"""规则兜底 + 可选 LLM 的 Campus Event 结构化提取，所有生成字段均受原文校验。

分类与行动步骤共用同一个"LLM 起草 → 对原文校验 → 不合规则丢弃/回退规则"的模式：
  - classify_category：五个稳定类别（action/opportunity/academic/campus_life/announcement），
    规则实现是唯一事实来源，LLM 只能在枚举内选择，选错回退规则结果；
  - plan_steps：LLM 起草行动步骤时必须为每步给出原文逐字依据（evidence），
    evidence 不在原文中的步骤直接丢弃 —— 没有"无证据步骤"入库的路径。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

CATEGORIES = ("action", "opportunity", "academic", "campus_life", "announcement")


def classify_category(title: str, summary: str, event_type: str = "", deadline: Optional[str] = None) -> str:
    """五类分类的规则实现（extractor 与 radar 共用，避免两套词表漂移）。"""
    text = f"{title} {summary} {event_type}"
    if deadline or any(word in text for word in ("报名", "申请", "提交", "缴费", "确认", "填报", "选课")):
        return "action"
    if any(word in text for word in ("科研", "竞赛", "比赛", "奖学金", "实习", "招聘", "创新创业", "项目申报")):
        return "opportunity"
    if any(word in text for word in ("考试", "课程", "培养方案", "成绩", "教学", "学位")):
        return "academic"
    if any(word in text for word in ("讲座", "社团", "活动", "文体", "生活", "宿舍")):
        return "campus_life"
    return "announcement"


def _date(text: str) -> Optional[str]:
    match = re.search(r"(20\d{2})[年./-]\s?(\d{1,2})[月./-]\s?(\d{1,2})", text)
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else None


def rule_extract(title: str, body: str) -> Dict[str, Any]:
    text = f"{title} {body}"
    deadline_match = re.search(
        r"(?:截止(?:时间)?|报名(?:截止)?|请于)\s*[:：]?\s*(20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}日?)", text
    )
    deadline = _date(deadline_match.group(1)) if deadline_match else None
    targets = [
        label
        for label, words in {
            "本科生": ("本科生", "本科"),
            "研究生": ("研究生", "硕士", "博士"),
            "毕业生": ("毕业生", "应届", "届毕业"),
        }.items()
        if any(word in text for word in words)
    ]
    actions = [word for word in ("报名", "申请", "提交", "填报", "参赛", "领取") if word in text]
    event_type = "竞赛" if any(word in text for word in ("竞赛", "大赛")) else "通知"
    return {
        "event_type": event_type,
        "summary": body[:280],
        "deadline": deadline,
        "targets": targets,
        "requirements": [],
        "materials": [],
        "actions": actions,
        "location": "",
        "category": classify_category(title, body, event_type, deadline),
        "extraction": "rule",
    }


def _strip_ws(value: str) -> str:
    """校验用的归一化：去掉全部空白，避免换行/空格差异误判"不在原文中"。"""
    return re.sub(r"\s+", "", value or "")


class CampusEventExtractor:
    def __init__(self, *, client: Any = None, model: str = "", gateway: Any = None):
        self.client, self.model, self.gateway = client, model, gateway

    @property
    def llm_ready(self) -> bool:
        return bool(self.client and self.model and self.gateway)

    async def extract(self, title: str, body: str) -> Dict[str, Any]:
        fallback = rule_extract(title, body)
        if not self.llm_ready:
            return fallback
        prompt = f"""从以下校园官方通知中提取 JSON。只输出 JSON；未知字段用空字符串或空数组。不得补充原文没有的条件、材料或步骤。
字段：
- event_type, summary, location
- category：五选一 —— action（需要办理/报名/缴费/选课等有明确动作的事项）、opportunity（竞赛/奖学金/实习/招聘等机会）、academic（考试/课程/成绩/培养方案）、campus_life（讲座/社团/文体/生活）、announcement（以上都不是的一般性通知）
- deadline：报名/提交的截止日期（YYYY-MM-DD 或空）。只取截止日期，不要把活动举办日期或发布日期当作 deadline
- targets, requirements, materials, actions
标题：{title}\n原文：{body[:10000]}"""
        try:
            response = await self.gateway.call(
                client=self.client,
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                system="你是严谨的信息抽取器，只依据给定官方原文。",
                max_tokens=900,
                temperature=0,
                thinking={"type": "disabled"},
                span_name="campus_event_extraction",
            )
            raw = response.response.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
            result = json.loads(raw)
        except Exception:
            return fallback
        return self._validate(result, title, body, fallback)

    @staticmethod
    def _validate(data: Dict[str, Any], title: str, body: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
        source = f"{title} {body}"
        result = {**fallback, "extraction": "llm"}
        for name in ("event_type", "summary", "location"):
            value = str(data.get(name, "")).strip()
            if value and (name == "summary" or value in source):
                result[name] = value[:600]
        # category 只能在五类枚举内选择；LLM 给出其他值时回退规则分类
        proposed_category = str(data.get("category", "")).strip()
        if proposed_category in CATEGORIES:
            result["category"] = proposed_category
        proposed_deadline = str(data.get("deadline", "")).strip()
        source_dates = {
            _date(match.group(0)) for match in re.finditer(r"20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}", source)
        }
        if (
            proposed_deadline
            and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", proposed_deadline)
            and proposed_deadline in source_dates
        ):
            result["deadline"] = proposed_deadline
        for name in ("targets", "requirements", "materials", "actions"):
            values = data.get(name, [])
            if not isinstance(values, list):
                continue
            result[name] = [value for value in (str(v).strip() for v in values) if value and value in source][:12]
        return result

    # ── 行动计划起草（evidence 逐条校验，见 create_action_plan）──────────────

    async def plan_steps(self, title: str, body: str, existing: List[str]) -> List[Dict[str, str]]:
        """LLM 起草完成该通知事项的行动步骤，每步必须携带原文逐字依据。

        返回 [{"step": ..., "evidence": ...}]；LLM 不可用或全部步骤无有效依据时
        返回空列表（调用方保持纯证据行为，不会因此丢掉原有 materials/actions）。
        """
        if not self.llm_ready:
            return []
        source = f"{title} {body}"
        existing_hint = "；".join(existing) if existing else "（无）"
        prompt = f"""根据官方通知原文，为学生起草完成这件事的行动步骤。只输出 JSON：{{"steps": [{{"step": "步骤", "evidence": "原文依据"}}]}}。
要求：
- evidence 必须是原文中逐字出现的句子或短语，作为该步骤存在的依据；找不到原文依据的步骤不要输出
- 步骤按执行顺序排列，3-8 条，每条不超过 40 字
- 不得编造原文没有的材料、条件、时间或流程；已知既定步骤（{existing_hint}）不要重复
标题：{title}\n原文：{body[:10000]}"""
        try:
            response = await self.gateway.call(
                client=self.client,
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                system="你是严谨的校园事务助理，只依据给定官方原文起草步骤。",
                max_tokens=900,
                temperature=0,
                thinking={"type": "disabled"},
                span_name="campus_action_plan",
            )
            raw = response.response.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
            data = json.loads(raw)
        except Exception:
            return []
        return self._validate_steps(data, source, existing)

    @staticmethod
    def _validate_steps(data: Dict[str, Any], source: str, existing: List[str]) -> List[Dict[str, str]]:
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            return []
        source_ws = _strip_ws(source)
        seen = {_strip_ws(step) for step in existing if step}
        validated: List[Dict[str, str]] = []
        for item in steps[:16]:
            if not isinstance(item, dict):
                continue
            step = str(item.get("step", "")).strip()
            evidence = str(item.get("evidence", "")).strip()
            # 依据必须逐字（去空白后）出现在原文中，否则该步骤不可信，直接丢弃
            if not (4 <= len(step) <= 60) or len(evidence) < 4:
                continue
            if _strip_ws(evidence) not in source_ws:
                continue
            normalized = _strip_ws(step)
            if normalized in seen:
                continue
            seen.add(normalized)
            validated.append({"step": step, "evidence": evidence[:120]})
            if len(validated) >= 8:
                break
        return validated
