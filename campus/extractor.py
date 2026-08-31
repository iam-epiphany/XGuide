"""规则兜底 + 可选 LLM 的 Campus Event 结构化提取，所有生成字段均受原文校验。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


def _date(text: str) -> Optional[str]:
    match = re.search(r"(20\d{2})[年./-]\s?(\d{1,2})[月./-]\s?(\d{1,2})", text)
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else None


def rule_extract(title: str, body: str) -> Dict[str, Any]:
    text = f"{title} {body}"
    deadline_match = re.search(r"(?:截止(?:时间)?|报名(?:截止)?|请于)\s*[:：]?\s*(20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}日?)", text)
    targets = [label for label, words in {"本科生": ("本科生", "本科"), "研究生": ("研究生", "硕士", "博士"), "毕业生": ("毕业生", "应届", "届毕业")}.items() if any(word in text for word in words)]
    actions = [word for word in ("报名", "申请", "提交", "填报", "参赛", "领取") if word in text]
    return {"event_type": "通知", "summary": body[:280], "deadline": _date(deadline_match.group(1)) if deadline_match else None, "targets": targets, "requirements": [], "materials": [], "actions": actions, "location": "", "extraction": "rule"}


class CampusEventExtractor:
    def __init__(self, *, client: Any = None, model: str = "", gateway: Any = None):
        self.client, self.model, self.gateway = client, model, gateway

    async def extract(self, title: str, body: str) -> Dict[str, Any]:
        fallback = rule_extract(title, body)
        if not (self.client and self.model and self.gateway):
            return fallback
        prompt = f"""从以下校园官方通知中提取 JSON。只输出 JSON；未知字段用空字符串或空数组。不得补充原文没有的条件、材料或步骤。
字段：event_type, summary, targets, deadline(YYYY-MM-DD 或空), requirements, materials, actions, location。
标题：{title}\n原文：{body[:10000]}"""
        try:
            response = await self.gateway.call(client=self.client, model=self.model, messages=[{"role": "user", "content": prompt}], system="你是严谨的信息抽取器，只依据给定官方原文。", max_tokens=900, temperature=0, thinking={"type": "disabled"}, span_name="campus_event_extraction")
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
        proposed_deadline = str(data.get("deadline", "")).strip()
        source_dates = {_date(match.group(0)) for match in re.finditer(r"20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}", source)}
        if proposed_deadline and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", proposed_deadline) and proposed_deadline in source_dates:
            result["deadline"] = proposed_deadline
        for name in ("targets", "requirements", "materials", "actions"):
            values = data.get(name, [])
            if not isinstance(values, list):
                continue
            result[name] = [value for value in (str(v).strip() for v in values) if value and value in source][:12]
        return result
