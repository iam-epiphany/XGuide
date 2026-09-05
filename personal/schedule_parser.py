"""课表文本解析 —— LLM 主路径 + 规则兜底，逐行校验后才允许入库。

用户把教务系统网页上的课表整段复制粘贴进来即可导入，不再依赖 .ics/JSON 文件。
LLM 输出的每一行都过 _valid_course 校验（星期 0-6、时间 HH:MM、start<end、
周次 1-30），不合法的行丢弃并计入 skipped；LLM 不可用或全军覆没时退回
规则解析（识别"课程名 周X HH:MM-HH:MM 地点"形态的行）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from personal.service import compress_weeks

logger = logging.getLogger(__name__)

_WEEKDAY_NUM = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _valid_course(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """校验并归一化一行课表；不合法返回 None（调用方计入 skipped）。"""
    if not isinstance(row, dict):
        return None
    course = str(row.get("course", "")).strip()
    if not (1 <= len(course) <= 40):
        return None
    try:
        day = int(row.get("day_of_week"))
    except (TypeError, ValueError):
        return None
    if not 0 <= day <= 6:
        return None
    start, end = str(row.get("start_time", "")).strip(), str(row.get("end_time", "")).strip()
    if not (_TIME_RE.match(start) and _TIME_RE.match(end) and start < end):
        return None
    location = str(row.get("location", "") or "").strip()[:40]
    weeks = row.get("weeks", [])
    if isinstance(weeks, str):
        weeks = [int(w) for w in re.findall(r"\d+", weeks)]
    clean_weeks: List[int] = []
    for w in weeks if isinstance(weeks, list) else []:
        try:
            w = int(w)
        except (TypeError, ValueError):
            continue
        if 1 <= w <= 30:
            clean_weeks.append(w)
    return {
        "course": course,
        "day_of_week": day,
        "start_time": start,
        "end_time": end,
        "location": location,
        "weeks": sorted(set(clean_weeks)),
    }


def rule_parse(text: str) -> Dict[str, Any]:
    """规则兜底：逐行识别「课程名 … 周X … HH:MM-HH:MM … 地点」形态。"""
    courses: List[Dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        day_match = re.search(r"[周星期]([一二三四五六日天])", line)
        if not day_match:
            continue
        times = re.findall(r"(\d{1,2})[:：](\d{2})", line)
        if len(times) < 2:
            continue
        start = f"{int(times[0][0]):02d}:{times[0][1]}"
        end = f"{int(times[-1][0]):02d}:{times[-1][1]}"
        name = re.sub(r"[周星期][一二三四五六日天]|\d{1,2}[:：]\d{2}|[-–—~至到]", " ", line)
        name = re.sub(r"\s+", " ", name).strip(" -–—,，;；、()（）") or "未命名课程"
        row = _valid_course(
            {"course": name[:40], "day_of_week": _WEEKDAY_NUM[day_match.group(1)], "start_time": start, "end_time": end}
        )
        if row:
            courses.append(row)
    non_empty = sum(1 for line in text.splitlines() if line.strip())
    return {
        "courses": courses,
        "skipped": max(0, non_empty - len(courses)),
        "parser": "rule",
    }


class ScheduleTextParser:
    def __init__(self, *, client: Any = None, model: str = "", gateway: Any = None):
        self.client, self.model, self.gateway = client, model, gateway

    @property
    def llm_ready(self) -> bool:
        return bool(self.client and self.model and self.gateway)

    async def parse(self, text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"courses": [], "skipped": 0, "parser": "none", "message": "内容为空"}
        if not self.llm_ready:
            result = rule_parse(text)
            if not result["courses"]:
                result["message"] = "未能从文本中识别出课程；请粘贴包含「周X、时间、课程名」的课表内容"
            return result
        prompt = f"""从学生提供的课表文本中解析全部课程。只输出 JSON：{{"courses": [{{"course": "课程名", "day_of_week": 0-6（0=周一）, "start_time": "HH:MM", "end_time": "HH:MM", "location": "地点或空", "weeks": [1, 2] 或 []}}]}}。
要求：
- 时间统一 24 小时制 HH:MM；"第X-Y节"若无法确定具体时间则丢弃该行，不要猜
- 单双周/周次信息用 weeks 数组（1-30 的整数）表达，全周上则留空数组
- 解析不了或含糊不清的行直接丢弃；不编造课程、地点或时间
课表文本：
{text[:12000]}"""
        try:
            response = await self.gateway.call(
                client=self.client,
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                system="你是严谨的课表解析器，只依据给定文本，输出 JSON。",
                max_tokens=8000,  # 80 行课表 × ~50 token/行：上限过低会让 JSON 截断报废
                temperature=0,
                thinking={"type": "disabled"},
                span_name="schedule_text_parsing",
            )
            raw = response.response.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
            data = json.loads(raw)
            rows = data.get("courses", []) if isinstance(data, dict) else []
        except json.JSONDecodeError:
            # 输出被 max_tokens 截断：回退到最后一个完整对象，闭合成合法 JSON，
            # 救回已生成的课程行（截断发生在第一个对象内部时无法抢救，回退规则）
            try:
                courses_idx = raw.find('"courses"')
                repaired = None
                for end in range(raw.rfind("}"), courses_idx, -1):
                    candidate = raw[: end + 1].rstrip().rstrip(",") + "]}"
                    try:
                        repaired = json.loads(candidate)
                        break
                    except json.JSONDecodeError:
                        continue
                rows = (repaired or {}).get("courses", []) if isinstance(repaired, dict) else []
                if not rows:
                    raise ValueError("无可抢救的完整课程行")
            except Exception:
                logger.warning("课表 LLM 输出截断且修复失败（回退规则解析）")
                return rule_parse(text)
        except Exception as ex:
            logger.warning("课表 LLM 解析失败（回退规则解析）: %s", ex)
            return rule_parse(text)
        courses, skipped = [], 0
        for row in rows[:80]:
            clean = _valid_course(row)
            if clean:
                courses.append(clean)
            else:
                skipped += 1
        if not courses:
            return rule_parse(text)
        # weeks 压缩为教学周表达式（与 import_courses 的存储格式一致）
        for course in courses:
            course["weeks"] = compress_weeks(course["weeks"]) if course["weeks"] else []
        return {"courses": courses, "skipped": skipped, "parser": "llm"}
