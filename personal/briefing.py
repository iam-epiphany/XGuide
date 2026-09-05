"""LLM 简报服务 —— Today 晨间简报 / Inbox 摘要叙述 / 空档利用建议。

与 extractor 同一套安全模式：LLM 只做生成，输入数据决定内容边界；
free_time_advice 的结构化建议逐条校验（todo 必须真实存在、时段必须落在
真实空档内），不合法的建议直接丢弃。

缓存：结果按 (user, kind, day) 存 SQLite，并记录输入数据的规范化指纹
（fingerprint）。数据没变 → 直接复用，一人一天最多几次 LLM 调用；
数据变了（加了待办、来了新通知）→ 自动重新生成。LLM 未配置时所有
方法返回 {"available": False}，端点与前端据此优雅降级。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_BRIEFING_SYSTEM = (
    "你是校园个人助理。只依据给定数据写内容，绝不编造数据中没有的课程、待办、"
    "时间、通知或截止日期；数据为空的部分不要提及。"
)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _json_loads(raw: str) -> Any:
    text = raw.strip().removeprefix("```json").removesuffix("```").strip()
    match = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
    if not match:
        raise ValueError("LLM 输出中没有 JSON")
    return json.loads(match.group(0))


class BriefingService:
    def __init__(self, *, client: Any = None, model: str = "", gateway: Any = None, store: Any = None):
        self.client, self.model, self.gateway, self.store = client, model, gateway, store

    @property
    def llm_ready(self) -> bool:
        return bool(self.client and self.model and self.gateway)

    # ── 内部：带缓存的生成 ────────────────────────────────────────────────────

    async def _generate(
        self,
        user_id: str,
        kind: str,
        day: str,
        payload: Any,
        prompt: str,
        *,
        system: str = _BRIEFING_SYSTEM,
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        if not self.llm_ready:
            return {"available": False, "reason": "LLM 未配置"}
        fingerprint = _fingerprint(payload)
        if self.store is not None:
            cached = await self.store.get_llm_briefing(user_id, kind, day, fingerprint)
            if cached:
                return {"available": True, "text": cached, "cached": True}
        try:
            response = await self.gateway.call(
                client=self.client,
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking={"type": "disabled"},
                span_name=f"briefing_{kind}",
            )
            text = response.response.content[0].text.strip()
        except Exception as ex:
            logger.warning("简报生成失败（%s/%s）: %s", kind, day, ex)
            return {"available": False, "reason": "生成失败，请稍后重试"}
        if not text:
            return {"available": False, "reason": "生成结果为空"}
        if self.store is not None:
            await self.store.put_llm_briefing(user_id, kind, day, fingerprint, text, self.model)
        return {"available": True, "text": text, "cached": False}

    # ── Today 晨间简报 ────────────────────────────────────────────────────────

    async def today_briefing(self, user_id: str, overview: Dict[str, Any], focus: List[Dict[str, Any]]) -> Dict[str, Any]:
        day = str(overview.get("date") or datetime.now().astimezone().date().isoformat())
        courses = [
            f"{c['start_time']}-{c['end_time']} {c['course']}（{c.get('location') or '地点未填'}）"
            for c in (overview.get("courses") or [])[:15]
        ]
        todos = [str(t.get("content")) for t in (overview.get("todos") or [])[:15]]
        upcoming = [
            f"{i['content']}（{i['due_at']}，{i['status']}）" for i in (overview.get("upcoming") or [])[:10]
        ]
        reminders = [f"{i['label']} · {i['content']}" for i in (overview.get("reminders") or [])[:5]]
        focus = [f"{f['title']}（截止 {f['deadline']}）" if f.get("deadline") else f["title"] for f in focus[:5]]
        payload = {"day": day, "courses": courses, "todos": todos, "upcoming": upcoming, "reminders": reminders, "focus": focus}
        if not any([courses, todos, upcoming, focus]):
            return {"available": True, "text": "今天没有课程、待办和临近的截止事项，安排一段自己喜欢的活动吧。", "cached": False}
        prompt = f"""今天是 {day}（{overview.get('weekday', '')}，第 {overview.get('week_num', 0)} 教学周）。
今日课程：{"；".join(courses) or "无"}
今天待办：{"；".join(todos) or "无"}
未来 7 天 DDL/考试：{"；".join(upcoming) or "无"}
需要留意：{"；".join(reminders) or "无"}
Inbox 今日关注：{"；".join(focus) or "无"}

请写一段 80-140 字的中文晨间简报：先概括今天的节奏（课程密度与空闲情况），再点出最紧迫的 1-2 件事，最后给一句可执行的建议。直接输出正文，不要标题。"""
        return await self._generate(user_id, "today", day, payload, prompt)

    # ── Inbox 摘要叙述 ────────────────────────────────────────────────────────

    async def inbox_narrative(self, user_id: str, briefing: Dict[str, Any]) -> Dict[str, Any]:
        day = datetime.now().astimezone().date().isoformat()
        focus = [
            {"title": e.get("title", ""), "deadline": e.get("deadline") or ""}
            for e in (briefing.get("today_focus") or [])[:5]
        ]
        recommended = [
            {"title": e.get("title", ""), "category": e.get("category", ""), "deadline": e.get("deadline") or ""}
            for e in (briefing.get("recommended") or [])[:6]
        ]
        if not focus and not recommended:
            return {"available": False, "reason": "收件箱暂无内容"}
        payload = {"focus": focus, "recommended": recommended}
        prompt = f"""校园通知收件箱的今日聚合结果（按关注度排序）：
今日关注：{json.dumps(focus, ensure_ascii=False)}
其他推荐：{json.dumps(recommended, ensure_ascii=False)}

请写一段 60-120 字的中文摘要：点出最需要注意的事件与它的截止时间，其余推荐一笔带过。直接输出正文，不要标题。"""
        return await self._generate(user_id, "inbox", day, payload, prompt)

    # ── 空档利用建议 ──────────────────────────────────────────────────────────

    async def free_time_advice(
        self,
        user_id: str,
        day: str,
        free_periods: List[Dict[str, str]],
        todos: List[Dict[str, Any]],
        upcoming: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        candidates = [
            {"id": t["id"], "content": t["content"], "kind": t.get("kind", "todo"), "due_at": t.get("due_at") or ""}
            for t in todos[:12]
        ]
        candidates += [
            {"id": i["id"], "content": i["content"], "kind": i.get("kind", "ddl"), "due_at": i.get("due_at") or ""}
            for i in upcoming[:6]
        ]
        if not free_periods or not candidates:
            return {"available": False, "reason": "没有可安排的空档或待办"}
        payload = {"day": day, "free_periods": free_periods, "candidates": candidates}
        prompt = f"""{day} 的空闲时段与待办如下。请把最值得推进的待办安排进空闲时段，只输出 JSON：
{{"suggestions": [{{"todo_id": 待办id, "start": "HH:MM", "end": "HH:MM", "why": "一句安排理由"}}]}}
要求：时段必须完全落在某个空闲时段内；最多 3 条；deadline 临近或已过期的优先；没有合适安排的待办不要硬排。
空闲时段：{json.dumps(free_periods, ensure_ascii=False)}
待办/DDL：{json.dumps(candidates, ensure_ascii=False)}"""
        result = await self._generate(
            user_id, "free_advice", day, payload, prompt, max_tokens=400, temperature=0
        )
        if not result.get("available"):
            return result
        try:
            data = _json_loads(result["text"])
        except (ValueError, json.JSONDecodeError) as ex:
            logger.warning("空档建议 JSON 解析失败: %s", ex)
            return {"available": False, "reason": "生成结果无法解析"}
        suggestions = self._validate_advice(data, todos + upcoming, free_periods)
        if not suggestions:
            return {"available": False, "reason": "没有生成有效的安排建议"}
        return {"available": True, "suggestions": suggestions, "cached": result.get("cached", False)}

    @staticmethod
    def _validate_advice(
        data: Any, todos: List[Dict[str, Any]], free_periods: List[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        """逐条校验：todo 必须真实存在，时段必须完全落在真实空档内。"""
        if not isinstance(data, dict):
            return []
        by_id = {t["id"]: t for t in todos}
        periods = [(p["start_time"], p["end_time"]) for p in free_periods]
        seen_ids: set = set()
        suggestions: List[Dict[str, Any]] = []
        for item in (data.get("suggestions") or [])[:8]:
            if not isinstance(item, dict):
                continue
            try:
                todo_id = int(item.get("todo_id"))
            except (TypeError, ValueError):
                continue
            if todo_id not in by_id or todo_id in seen_ids:
                continue
            start, end = str(item.get("start", "")).strip(), str(item.get("end", "")).strip()
            if not (_TIME_RE.match(start) and _TIME_RE.match(end) and start < end):
                continue
            if not any(p_start <= start and end <= p_end for p_start, p_end in periods):
                continue
            todo = by_id[todo_id]
            seen_ids.add(todo_id)
            suggestions.append(
                {
                    "todo_id": todo_id,
                    "content": todo["content"],
                    "due_at": todo.get("due_at") or "",
                    "start": start,
                    "end": end,
                    "why": str(item.get("why", "")).strip()[:100],
                }
            )
            if len(suggestions) >= 3:
                break
        return suggestions
