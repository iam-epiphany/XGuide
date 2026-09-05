"""个人数据中心路由：课表导入/查询、待办 CRUD、当日汇总。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from api import state
from api.deps import require_user

router = APIRouter(prefix="/personal", tags=["个人数据"])


class ScheduleImportBody(BaseModel):
    """课表导入请求体：courses（JSON 课表）与 ics_text（ICS 文本）二选一。"""

    user_id: str = Field(default="anonymous", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    courses: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=200)
    ics_text: Optional[str] = Field(default=None, max_length=500_000)


def _require_personal_service():
    if state._personal_service is None:
        raise HTTPException(503, "个人数据中心未初始化")
    return state._personal_service


@router.post("/schedule/import")
async def import_schedule(body: ScheduleImportBody, user=Depends(require_user)):
    """
    导入课程表（整表替换）。支持两种格式：
      1. JSON 课表：{"user_id": "...", "courses": [{"course", "day_of_week", "start_time", "end_time", "location", "weeks"}]}
      2. ICS 文本：{"user_id": "...", "ics_text": "BEGIN:VCALENDAR..."}（教务系统导出）
    返回导入的课程数量。
    """
    personal = _require_personal_service()
    if body.courses is not None:
        count = await personal.import_courses(user.id, body.courses)
    elif body.ics_text:
        from personal.ics_parser import parse_ics
        from personal.time_context import SEMESTER_START, SEMESTER_WEEKS

        courses = parse_ics(body.ics_text, SEMESTER_START, SEMESTER_WEEKS)
        count = await personal.import_courses(user.id, [c.to_dict() for c in courses])
    else:
        raise HTTPException(400, "请提供 courses（JSON 课表）或 ics_text（ICS 文本）")
    return {"message": f"课表导入成功，共 {count} 门课程", "courses": count}


@router.post("/schedule/import/file")
async def import_schedule_file(
    file: UploadFile = File(...),
    user_id: str = Form("anonymous"),
    user=Depends(require_user),
):
    """
    上传 .ics（教务系统导出）或 .json 课表文件导入。
    文件大小限制 5MB。
    """
    personal = _require_personal_service()
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 5MB 限制")
    text = content.decode("utf-8", errors="ignore")
    filename = (file.filename or "").lower()

    from personal.ics_parser import parse_ics
    from personal.time_context import SEMESTER_START, SEMESTER_WEEKS

    if filename.endswith(".json"):
        try:
            docs = json.loads(text)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}") from e
        if isinstance(docs, dict):
            docs = docs.get("courses", [])
        if not isinstance(docs, list):
            raise HTTPException(
                400, "JSON 课表应为数组: [{course, day_of_week, start_time, end_time, location, weeks}]"
            )
        count = await personal.import_courses(user.id, docs)
    elif filename.endswith(".ics"):
        courses = parse_ics(text, SEMESTER_START, SEMESTER_WEEKS)
        count = await personal.import_courses(user.id, [c.to_dict() for c in courses])
    else:
        raise HTTPException(400, "仅支持 .ics 或 .json 文件")
    return {"message": f"文件 {file.filename} 导入成功，共 {count} 门课程", "courses": count}


class ScheduleTextBody(BaseModel):
    """粘贴文本导入课表：教务系统网页整段复制即可。"""

    text: str = Field(min_length=1, max_length=20_000)


@router.post("/schedule/import/text")
async def import_schedule_text(body: ScheduleTextBody, user=Depends(require_user)):
    """LLM 解析粘贴的课表文本并整表导入；每行过校验，不合法行跳过。

    LLM 不可用或解析不出课程时回退规则解析；两者都失败返回 400。
    """
    parser = state._schedule_parser
    if parser is None:
        raise HTTPException(503, "课表解析服务未初始化")
    parsed = await parser.parse(body.text)
    courses = parsed.get("courses", [])
    if not courses:
        raise HTTPException(400, parsed.get("message") or "未能从文本中解析出任何课程，请检查内容后重试")
    count = await _require_personal_service().import_courses(user.id, courses)
    return {
        "message": f"已导入 {count} 门课程" + (f"，跳过 {parsed['skipped']} 行无法识别的内容" if parsed.get("skipped") else ""),
        "courses": count,
        "skipped": parsed.get("skipped", 0),
        "parser": parsed.get("parser", "llm"),
    }


@router.get("/schedule")
async def get_schedule(user=Depends(require_user)):
    """查看用户完整课表，并附带当前教学周的课程子集。"""
    personal = _require_personal_service()
    weekly = await personal.weekly_overview(user.id)
    # 不能只返回当前教学周：开学前、假期或仅导入后续周课程时，空周视图会让前端
    # 误以为上传的数据丢失。完整课表始终由 SQLite 返回，周视图作为附加信息。
    courses = await personal.store.get_schedule(user.id)
    return {
        "user_id": user.id,
        "week_num": weekly["week_num"],
        "monday": weekly["monday"],
        "in_semester": weekly["in_semester"],
        "courses": courses,
        "weekly_courses": weekly["courses"],
        "total": len(courses),
    }


@router.delete("/schedule")
async def clear_schedule(user=Depends(require_user)):
    """清空用户课表（重新导入前使用）。"""
    personal = _require_personal_service()
    await personal.store.clear_schedule(user.id)
    return {"message": "课表已清空"}


class TodoBody(BaseModel):
    user_id: str = Field(default="anonymous", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    content: str = Field(min_length=1, max_length=500)
    kind: Literal["todo", "ddl", "exam"] = "todo"
    due_at: Optional[str] = Field(default=None, max_length=32, pattern=r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")


@router.post("/todo")
async def add_todo(body: TodoBody, user=Depends(require_user)):
    """新增待办 / DDL / 考试安排。"""
    personal = _require_personal_service()
    if not body.content.strip():
        raise HTTPException(400, "content 不能为空")
    todo = await personal.add_todo(
        user.id,
        body.content.strip(),
        kind=body.kind,  # Literal["todo","ddl","exam"] 已校验，无需再判
        due_at=body.due_at,
    )
    return {"message": "已记录", "todo": todo}


@router.get("/todo")
async def list_todos(status: str = "open", user=Depends(require_user)):
    """查看待办清单（open/done/all）。"""
    personal = _require_personal_service()
    todos = await personal.list_todos(user.id, status=status)
    return {"user_id": user.id, "status": status, "todos": todos, "total": len(todos)}


@router.post("/todo/{todo_id}/complete")
async def complete_todo(todo_id: int, done: bool = True, user=Depends(require_user)):
    """标记完成 / 恢复待办。"""
    personal = _require_personal_service()
    todo = await personal.complete_todo(user.id, todo_id, done=done)
    if todo is None:
        raise HTTPException(404, f"待办 {todo_id} 不存在或不属于该用户")
    return {"message": "已标记完成" if done else "已恢复未完成", "todo": todo}


@router.delete("/todo/{todo_id}")
async def delete_todo(todo_id: int, user=Depends(require_user)):
    """删除待办。"""
    personal = _require_personal_service()
    ok = await personal.delete_todo(user.id, todo_id)
    if not ok:
        raise HTTPException(404, f"待办 {todo_id} 不存在或不属于该用户")
    return {"message": "已删除"}


@router.get("/overview")
async def personal_overview(user=Depends(require_user)):
    """当日汇总：课程 + 待办 + 未来 7 天 DDL/考试倒计时（对话工具与前端共用）。"""
    personal = _require_personal_service()
    return await personal.overview(user.id)
