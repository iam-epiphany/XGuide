"""Campus Event Store 的 Agent 工具：Inbox 与 Chat 读取同一份持久化事件。"""
from __future__ import annotations

from typing import Any, Dict


def _user_id(context: Dict[str, Any]) -> str:
    return (context.get("user_id") or "").strip() or "anonymous"


async def query_campus_events_handler(params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    if _user_id(context) == "anonymous":
        return {"available": False, "auth_required": True, "message": "请先登录后查看与你相关的校园通知。"}
    radar, personal = context.get("campus_radar"), context.get("personal_service")
    if not radar or not personal:
        return {"available": False, "message": "校园通知服务暂不可用。"}
    profile = await personal.store.get_profile(_user_id(context))
    events = await radar.relevant_events(_user_id(context), profile, str(params.get("query", "")), int(params.get("limit", 8) or 8))
    return {"available": True, "events": events, "total": len(events), "profile_complete": bool(profile.get("education") or profile.get("interests"))}


async def get_campus_event_handler(params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    radar = context.get("campus_radar")
    try:
        event_id = int(params.get("id"))
    except (TypeError, ValueError):
        return {"available": False, "message": "需要有效的通知 id。"}
    event = await radar.get_event(event_id) if radar else None
    if not event:
        return {"available": False, "message": "未找到该通知。"}
    # 原文保留在 Store，但面向 Agent 仅提供必要详情与来源，避免上下文膨胀。
    return {"available": True, "event": {key: event.get(key) for key in ("id", "title", "event_type", "summary", "targets", "deadline", "requirements", "materials", "actions", "location", "source_name", "source_url", "published_at", "updated_at", "body")}}


async def create_action_plan_handler(params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    if _user_id(context) == "anonymous":
        return {"available": False, "auth_required": True, "message": "请先登录后把通知加入个人计划。"}
    try:
        event_id = int(params.get("event_id"))
    except (TypeError, ValueError):
        return {"available": False, "message": "需要有效的通知 event_id。"}
    radar, personal = context.get("campus_radar"), context.get("personal_service")
    if not radar or not personal:
        return {"available": False, "message": "校园通知服务暂不可用。"}
    try:
        plan = await radar.create_action_plan(_user_id(context), event_id, personal)
    except ValueError as exc:
        return {"available": False, "message": str(exc)}
    return {"available": True, "message": "已依据官方通知生成个人行动计划", "plan": plan}
