"""校园通知雷达：采集公开网页、去重保存、按稳定学生画像生成 Inbox。

该模块只访问无需登录的官方页面。采集失败不会影响个人日程；前端会明确
显示上次同步结果，通知始终保留来源链接以便回到官方原文核验。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from html.parser import HTMLParser
import hashlib
import json
import pathlib
import re
import sqlite3
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx

from personal.store import PersonalStore

SOURCES = [
    {"name": "西电新闻网", "category": "school", "url": "https://news.xidian.edu.cn/"},
    {"name": "本科生院", "category": "academic", "url": "https://jwc.xidian.edu.cn/"},
    {"name": "西电就业信息网", "category": "employment", "url": "https://job.xidian.edu.cn/"},
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campus_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL,
    source_category TEXT NOT NULL,
    source_url TEXT NOT NULL,
    published_at TEXT,
    deadline TEXT,
    targets_json TEXT NOT NULL DEFAULT '[]',
    actions_json TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campus_events_published ON campus_events(published_at DESC);
CREATE TABLE IF NOT EXISTS campus_inbox (
    user_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    relevance INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, event_id)
);
"""


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[tuple[str, str]] = []
        self._href = ""
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            title = re.sub(r"\s+", " ", "".join(self._chunks)).strip()
            self.links.append((self._href, title))
            self._href = ""
            self._chunks = []


def _text(html: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _date_in(text: str) -> Optional[str]:
    match = re.search(r"(20\d{2})[年./-]\s?(\d{1,2})[月./-]\s?(\d{1,2})", text)
    if not match:
        return None
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _event_fields(title: str, body: str) -> Dict[str, Any]:
    text = f"{title} {body}"
    deadline = None
    match = re.search(r"(?:截止(?:时间)?|报名(?:截止)?|请于)\s*[:：]?\s*(20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}日?)", text)
    if match:
        deadline = _date_in(match.group(1))
    targets = []
    for label, words in {"本科生": ("本科生", "本科"), "研究生": ("研究生", "硕士", "博士"), "毕业生": ("毕业生", "应届", "届毕业")}.items():
        if any(word in text for word in words):
            targets.append(label)
    actions = []
    for word in ("报名", "申请", "提交", "填报", "参赛", "领取"):
        if word in text:
            actions.append(word)
    return {"deadline": deadline, "targets": targets, "actions": actions}


class CampusRadar:
    def __init__(self, personal_store: PersonalStore):
        self.store = personal_store
        self.db_path = personal_store.db_path
        with self.store._connect() as conn:
            conn.executescript(_SCHEMA)

    async def refresh(self) -> Dict[str, Any]:
        results = await asyncio.gather(*(self._fetch_source(source) for source in SOURCES), return_exceptions=True)
        inserted, checked, errors = 0, 0, []
        for source, result in zip(SOURCES, results):
            if isinstance(result, Exception):
                errors.append({"source": source["name"], "message": str(result)[:180]})
                continue
            checked += result["checked"]
            inserted += await self.store._run(self._save_events_sync, result["events"])
        return {"sources": len(SOURCES), "checked": checked, "new_events": inserted, "errors": errors}

    async def _fetch_source(self, source: Dict[str, str]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": "EchoGuide Campus Radar/1.0 (+public information only)"}) as client:
            page = (await client.get(source["url"])).text
            parser = _LinkParser()
            parser.feed(page)
            host = urlparse(source["url"]).netloc
            candidates = []
            for href, title in parser.links:
                url = urljoin(source["url"], href)
                if urlparse(url).netloc != host or url.rstrip("/") == source["url"].rstrip("/") or len(title) < 8 or title in {"更多", "首页", "详情", source["name"]}:
                    continue
                if any(skip in url.lower() for skip in ("javascript:", "login", "register")):
                    continue
                candidates.append((url, title))
            unique = list(dict.fromkeys(candidates))[:25]
            events = []
            for url, title in unique:
                try:
                    detail = (await client.get(url)).text
                    body = _text(detail)[:9000]
                except httpx.HTTPError:
                    body = ""
                fields = _event_fields(title, body)
                events.append({
                    "fingerprint": hashlib.sha256(url.encode("utf-8")).hexdigest(), "title": title,
                    "summary": body[:280], "body": body, "source_name": source["name"],
                    "source_category": source["category"], "source_url": url,
                    "published_at": _date_in(body) or _date_in(title), **fields,
                })
        return {"checked": len(events), "events": events}

    def _save_events_sync(self, events: List[Dict[str, Any]]) -> int:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        with self.store._connect() as conn:
            for event in events:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO campus_events
                    (fingerprint,title,summary,body,source_name,source_category,source_url,published_at,deadline,targets_json,actions_json,fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event["fingerprint"], event["title"], event["summary"], event["body"], event["source_name"], event["source_category"], event["source_url"], event["published_at"], event["deadline"], json.dumps(event["targets"], ensure_ascii=False), json.dumps(event["actions"], ensure_ascii=False), now),
                )
                inserted += cur.rowcount
        return inserted

    async def inbox(self, user_id: str, profile: Dict[str, Any], status: str = "active") -> List[Dict[str, Any]]:
        return await self.store._run(self._inbox_sync, user_id, profile, status)

    def _inbox_sync(self, user_id: str, profile: Dict[str, Any], status: str) -> List[Dict[str, Any]]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM campus_events ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 160").fetchall()
            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            for row in rows:
                event = dict(row)
                relevance, reason = self._relevance(event, profile)
                if relevance < 2:
                    continue
                conn.execute("""INSERT INTO campus_inbox (user_id,event_id,relevance,reason,status,updated_at) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(user_id,event_id) DO UPDATE SET relevance=excluded.relevance, reason=excluded.reason""",
                    (user_id, event["id"], relevance, reason, "new", now))
            query = """SELECT e.*, i.relevance, i.reason, i.status FROM campus_inbox i JOIN campus_events e ON e.id=i.event_id WHERE i.user_id=?"""
            args: List[Any] = [user_id]
            if status == "active":
                query += " AND i.status != 'ignored'"
            elif status != "all":
                query += " AND i.status = ?"
                args.append(status)
            query += " ORDER BY i.relevance DESC, COALESCE(e.deadline, '9999-12-31'), COALESCE(e.published_at, e.fetched_at) DESC"
            result = [dict(row) for row in conn.execute(query, args).fetchall()]
        for event in result:
            event["targets"] = json.loads(event.pop("targets_json") or "[]")
            event["actions"] = json.loads(event.pop("actions_json") or "[]")
            event.pop("body", None)
        return result

    @staticmethod
    def _relevance(event: Dict[str, Any], profile: Dict[str, Any]) -> tuple[int, str]:
        text = f"{event['title']} {event['summary']}"
        score, reasons = 0, []
        if event["source_category"] == "employment" and ("就业" in profile.get("interests", []) or profile.get("grade", "").endswith("届")):
            score += 3; reasons.append("你关注就业")
        mapping = {"奖学金": ("奖学金", "评优", "资助"), "竞赛": ("竞赛", "比赛", "挑战杯"), "保研": ("推免", "保研"), "就业": ("就业", "招聘", "实习", "选调"), "考研": ("考研", "招生", "调剂"), "出国": ("出国", "留学", "交流")}
        for interest in profile.get("interests", []):
            if any(word in text for word in mapping.get(interest, (interest,))):
                score += 3; reasons.append(f"你关注{interest}")
        targets = json.loads(event.get("targets_json") or "[]")
        if profile.get("education") and profile["education"] in targets:
            score += 2; reasons.append(f"面向{profile['education']}")
        if profile.get("college") and profile["college"] in text:
            score += 4; reasons.append("面向你的学院")
        if any(word in text for word in ("截止", "报名", "申请", "提交")):
            score += 1
        return score, "；".join(dict.fromkeys(reasons)) or "包含需要关注的报名或申请事项"

    async def set_status(self, user_id: str, event_id: int, status: str) -> bool:
        return await self.store._run(self._set_status_sync, user_id, event_id, status)

    def _set_status_sync(self, user_id: str, event_id: int, status: str) -> bool:
        with self.store._connect() as conn:
            cur = conn.execute("UPDATE campus_inbox SET status=?, updated_at=? WHERE user_id=? AND event_id=?", (status, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"), user_id, event_id))
        return cur.rowcount > 0

    async def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        return await self.store._run(self._get_event_sync, event_id)

    def _get_event_sync(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM campus_events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None
