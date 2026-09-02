"""产品化路由：Today、提醒、学生画像与校园通知 Inbox。"""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import state
from api.deps import require_user

router = APIRouter(tags=["产品"])


def _personal():
    if state._personal_service is None:
        raise HTTPException(503, "个人数据中心未初始化")
    return state._personal_service


def _radar():
    if state._campus_radar is None:
        raise HTTPException(503, "校园通知雷达未初始化")
    return state._campus_radar


class TodoUpdateBody(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=500)
    kind: Optional[Literal["todo", "ddl", "exam"]] = None
    due_at: Optional[str] = Field(default=None, max_length=32, pattern=r"^$|^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")


class ProfileBody(BaseModel):
    college: str = Field(default="", max_length=100)
    major: str = Field(default="", max_length=100)
    grade: str = Field(default="", max_length=32)
    education: Literal["", "本科生", "研究生"] = ""
    interests: List[Literal["保研", "奖学金", "竞赛", "就业", "考研", "出国"]] = Field(default_factory=list, max_length=6)


class InboxStatusBody(BaseModel):
    status: Literal["seen", "ignored", "interested"]


class InboxDeleteBody(BaseModel):
    # 空数组（或省略请求体）表示清空当前用户的 Inbox。
    event_ids: List[int] = Field(default_factory=list, max_length=100)


@router.get("/personal/today")
async def today(user=Depends(require_user)):
    return await _personal().overview(user.id)


@router.get("/personal/reminders")
async def reminders(user=Depends(require_user)):
    return await _personal().reminders(user.id)


@router.get("/personal/free-time")
async def free_time(when: str = "今天", user=Depends(require_user)):
    try:
        return await _personal().free_time(user.id, when)
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex


@router.patch("/personal/todo/{todo_id}")
async def update_todo(todo_id: int, body: TodoUpdateBody, user=Depends(require_user)):
    due_at = "" if "due_at" in body.model_fields_set and body.due_at is None else body.due_at
    todo = await _personal().update_todo(user.id, todo_id, content=body.content, kind=body.kind, due_at=due_at)
    if todo is None:
        raise HTTPException(404, "待办不存在、不属于当前用户，或未提供更新字段")
    return {"message": "已更新", "todo": todo}


@router.get("/student-profile")
async def get_profile(user=Depends(require_user)):
    return await _personal().store.get_profile(user.id)


@router.put("/student-profile")
async def save_profile(body: ProfileBody, user=Depends(require_user)):
    return await _personal().store.save_profile(user.id, body.model_dump())


@router.post("/inbox/refresh")
async def refresh_inbox(user=Depends(require_user)):
    # 拉取仅来自公开官方网页，不传递用户画像或任何认证信息到外部站点。
    return await _radar().refresh()


@router.get("/inbox")
async def inbox(status: Literal["active", "all", "new", "seen", "interested", "ignored"] = "active", user=Depends(require_user)):
    profile = await _personal().store.get_profile(user.id)
    events = await _radar().inbox(user.id, profile, status)
    profile_complete = bool(profile.get("education") or profile.get("interests"))
    return {
        "events": events,
        "profile_complete": profile_complete,
        "delivery_mode": "personalized" if profile_complete else "recent_public",
        "ttl_hours": _radar().inbox_ttl_hours,
        "total": len(events),
    }


@router.get("/inbox/briefing")
async def inbox_briefing(user=Depends(require_user)):
    """Personal attention center: scored CampusEvents, grouped from source notices."""
    profile = await _personal().store.get_profile(user.id)
    briefing = await _radar().inbox_briefing(user.id, profile)
    profile_complete = bool(profile.get("education") or profile.get("interests"))
    return {
        **briefing,
        "profile_complete": profile_complete,
        "delivery_mode": "personalized" if profile_complete else "recent_public",
        "ttl_hours": _radar().inbox_ttl_hours,
    }


@router.post("/inbox/{event_id}/status")
async def set_inbox_status(event_id: int, body: InboxStatusBody, user=Depends(require_user)):
    if not await _radar().set_status(user.id, event_id, body.status):
        raise HTTPException(404, "通知不在当前用户的收件箱中")
    return {"message": "已更新"}


@router.delete("/inbox")
async def delete_inbox(body: InboxDeleteBody | None = None, user=Depends(require_user)):
    deleted = await _radar().delete_inbox(user.id, body.event_ids if body else None)
    return {"message": "已删除", "deleted": deleted}


@router.post("/inbox/{event_id}/add-to-plan")
async def add_event_to_plan(event_id: int, user=Depends(require_user)):
    try:
        plan = await _radar().create_action_plan(user.id, event_id, _personal())
    except ValueError as ex:
        raise HTTPException(404, str(ex)) from ex
    return {"message": "已生成个人行动计划", "plan": plan}
