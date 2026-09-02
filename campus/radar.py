"""校园通知雷达：采集公开网页、去重保存、按稳定学生画像生成 Inbox。

该模块只访问无需登录的官方页面。采集失败不会影响个人日程；前端会明确
显示上次同步结果，通知始终保留来源链接以便回到官方原文核验。
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import hashlib
from html.parser import HTMLParser
import json
import re
from typing import Any, Dict, List, Optional

import httpx

from campus.adapters import HtmlNoticeAdapter, PublicSourceAdapter, default_public_adapters
from campus.extractor import CampusEventExtractor
from personal.store import PersonalStore

# 兼容既有脚本/测试的可读配置；实际同步由 adapters 调度。
SOURCES = [{"name": adapter.name, "category": adapter.category, "url": adapter.listing_url} for adapter in default_public_adapters()]

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
    requirements_json TEXT NOT NULL DEFAULT '[]',
    materials_json TEXT NOT NULL DEFAULT '[]',
    location TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '通知',
    content_hash TEXT NOT NULL DEFAULT '',
    etag TEXT,
    last_modified TEXT,
    fetched_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campus_events_published ON campus_events(published_at DESC);
CREATE TABLE IF NOT EXISTS campus_inbox (
    user_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    relevance INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    expires_at TEXT,
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
    def __init__(self, personal_store: PersonalStore, *, adapters: Optional[List[PublicSourceAdapter]] = None, extractor: Optional[CampusEventExtractor] = None, inbox_ttl_hours: int = 48):
        self.store = personal_store
        self.db_path = personal_store.db_path
        self.adapters = adapters or default_public_adapters()
        self.extractor = extractor or CampusEventExtractor()
        self.inbox_ttl_hours = max(1, min(int(inbox_ttl_hours), 24 * 30))
        with self.store._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(campus_events)")}
            for name, definition in {"requirements_json": "TEXT NOT NULL DEFAULT '[]'", "materials_json": "TEXT NOT NULL DEFAULT '[]'", "location": "TEXT NOT NULL DEFAULT ''", "event_type": "TEXT NOT NULL DEFAULT '通知'", "content_hash": "TEXT NOT NULL DEFAULT ''", "etag": "TEXT", "last_modified": "TEXT", "last_checked_at": "TEXT", "updated_at": "TEXT"}.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE campus_events ADD COLUMN {name} {definition}")
            inbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(campus_inbox)")}
            if "expires_at" not in inbox_columns:
                conn.execute("ALTER TABLE campus_inbox ADD COLUMN expires_at TEXT")

    async def refresh(self) -> Dict[str, Any]:
        results = await asyncio.gather(*(self._refresh_adapter(adapter) for adapter in self.adapters), return_exceptions=True)
        inserted, updated, unchanged, checked, failed, errors = 0, 0, 0, 0, 0, []
        for adapter, result in zip(self.adapters, results, strict=False):
            if isinstance(result, Exception):
                errors.append({"source": adapter.name, "message": str(result)[:180]})
                continue
            checked += result["checked"]
            inserted += result["new"]
            updated += result["updated"]
            unchanged += result["unchanged"]
            failed += result.get("failed", 0)
            if result.get("failed"):
                errors.append({"source": adapter.name, "message": f"{result['failed']} 条通知读取失败"})
        return {"sources": len(self.adapters), "checked": checked, "new_events": inserted, "updated_events": updated, "unchanged": unchanged, "failed": failed, "errors": errors}

    async def _refresh_adapter(self, adapter: PublicSourceAdapter) -> Dict[str, int]:
        counts = {"checked": 0, "new": 0, "updated": 0, "unchanged": 0, "failed": 0}
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": "XGuide Campus Radar/1.0 (+public information only)"}) as client:
            links = await adapter.discover(client)
            for link in links:
                counts["checked"] += 1
                prior = await self.store._run(self._event_sync, link.url)
                try:
                    page = await adapter.fetch(client, link, etag=prior.get("etag") if prior else None, last_modified=prior.get("last_modified") if prior else None)
                except (httpx.HTTPError, ValueError):
                    # 单页临时异常不应中止同一来源的其余通知同步。
                    counts["failed"] += 1
                    continue
                if page.not_modified:
                    await self.store._run(self._touch_checked_sync, link.url)
                    counts["unchanged"] += 1
                    continue
                content_hash = hashlib.sha256(page.body.encode("utf-8")).hexdigest()
                if prior and prior.get("content_hash") == content_hash:
                    await self.store._run(self._touch_checked_sync, link.url, page.etag, page.last_modified)
                    counts["unchanged"] += 1
                    continue
                fields = await self.extractor.extract(link.title, page.body)
                state = await self.store._run(self._upsert_event_sync, {"url": link.url, "title": link.title, "body": page.body, "source_name": link.source_name, "source_category": link.source_category, "published_at": link.published_at, "etag": page.etag, "last_modified": page.last_modified, "content_hash": content_hash, **fields})
                counts[state] += 1
        return counts

    def _event_sync(self, url: str) -> Optional[Dict[str, Any]]:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM campus_events WHERE source_url=?", (url,)).fetchone()
        return dict(row) if row else None

    def _touch_checked_sync(self, url: str, etag: Optional[str] = None, last_modified: Optional[str] = None) -> None:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with self.store._connect() as conn:
            conn.execute("UPDATE campus_events SET last_checked_at=?, etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified) WHERE source_url=?", (now, etag, last_modified, url))

    def _upsert_event_sync(self, event: Dict[str, Any]) -> str:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        existing = self._event_sync(event["url"])
        with self.store._connect() as conn:
            values = (hashlib.sha256(event["url"].encode()).hexdigest(), event["title"], event["summary"], event["body"], event["source_name"], event["source_category"], event["url"], event.get("published_at") or _date_in(event["body"]) or _date_in(event["title"]), event.get("deadline"), json.dumps(event.get("targets", []), ensure_ascii=False), json.dumps(event.get("actions", []), ensure_ascii=False), json.dumps(event.get("requirements", []), ensure_ascii=False), json.dumps(event.get("materials", []), ensure_ascii=False), event.get("location", ""), event.get("event_type", "通知"), event["content_hash"], event.get("etag"), event.get("last_modified"), now, now, now)
            conn.execute("""INSERT INTO campus_events (fingerprint,title,summary,body,source_name,source_category,source_url,published_at,deadline,targets_json,actions_json,requirements_json,materials_json,location,event_type,content_hash,etag,last_modified,fetched_at,last_checked_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET title=excluded.title,summary=excluded.summary,body=excluded.body,published_at=excluded.published_at,deadline=excluded.deadline,targets_json=excluded.targets_json,actions_json=excluded.actions_json,requirements_json=excluded.requirements_json,materials_json=excluded.materials_json,location=excluded.location,event_type=excluded.event_type,content_hash=excluded.content_hash,etag=excluded.etag,last_modified=excluded.last_modified,last_checked_at=excluded.last_checked_at,updated_at=excluded.updated_at""", values)
        return "updated" if existing else "new"

    async def _fetch_source(self, source: Dict[str, str]) -> Dict[str, Any]:
        """兼容旧调用方：由通用 Adapter 完成发现与正文读取。"""
        adapter = HtmlNoticeAdapter(name=source["name"], category=source["category"], listing_url=source["url"])
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": "XGuide Campus Radar/1.0 (+public information only)"}) as client:
            events = []
            for link in await adapter.discover(client):
                page = await adapter.fetch(client, link)
                fields = await self.extractor.extract(link.title, page.body)
                events.append({"fingerprint": hashlib.sha256(link.url.encode("utf-8")).hexdigest(), "title": link.title, "body": page.body, "source_name": link.source_name, "source_category": link.source_category, "source_url": link.url, "published_at": link.published_at or _date_in(page.body) or _date_in(link.title), **fields})
        return {"checked": len(events), "events": events}

    def _save_events_sync(self, events: List[Dict[str, Any]]) -> int:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        with self.store._connect() as conn:
            for event in events:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO campus_events
                    (fingerprint,title,summary,body,source_name,source_category,source_url,published_at,deadline,targets_json,actions_json,fetched_at,last_checked_at,updated_at,content_hash)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event["fingerprint"], event["title"], event["summary"], event["body"], event["source_name"], event["source_category"], event["source_url"], event["published_at"], event["deadline"], json.dumps(event["targets"], ensure_ascii=False), json.dumps(event["actions"], ensure_ascii=False), now, now, now, hashlib.sha256(event["body"].encode()).hexdigest()),
                )
                inserted += cur.rowcount
        return inserted

    async def inbox(self, user_id: str, profile: Dict[str, Any], status: str = "active") -> List[Dict[str, Any]]:
        return await self.store._run(self._inbox_sync, user_id, profile, status)

    def _inbox_sync(self, user_id: str, profile: Dict[str, Any], status: str) -> List[Dict[str, Any]]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM campus_events ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 160").fetchall()
            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            today = now[:10]
            expires_at = (datetime.now().astimezone() + timedelta(hours=self.inbox_ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
            # 未设置学历或关注方向时，不应让用户面对空收件箱。展示最近 24 小时
            # 实际同步检查到的公开通知；保存画像后才启用下面的个性化阈值筛选。
            profile_ready = bool(profile.get("education") or profile.get("interests"))
            recent_cutoff = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            for row in rows:
                event = dict(row)
                # 对具备明确日期的赛事/通知，仅在截止当天及之前进入 Inbox；
                # 旧的 inbox 记录也会在下面查询中排除，避免过期赛事再次被“推送”。
                if event.get("deadline") and event["deadline"] < today:
                    continue
                if profile_ready:
                    relevance, reason = self._relevance(event, profile)
                    if relevance < 2:
                        continue
                else:
                    last_seen = event.get("last_checked_at") or event.get("fetched_at") or ""
                    if last_seen < recent_cutoff:
                        continue
                    relevance, reason = 1, "尚未设置通知筛选条件，展示最近 24 小时同步的公开通知"
                if relevance < 1:
                    continue
                conn.execute("""INSERT INTO campus_inbox (user_id,event_id,relevance,reason,status,expires_at,updated_at) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,event_id) DO UPDATE SET relevance=excluded.relevance, reason=excluded.reason,
                    expires_at=COALESCE(campus_inbox.expires_at, excluded.expires_at)""",
                    (user_id, event["id"], relevance, reason, "new", expires_at, now))
            query = """SELECT e.*, i.relevance, i.reason, i.status, i.expires_at FROM campus_inbox i JOIN campus_events e ON e.id=i.event_id WHERE i.user_id=? AND i.status != 'deleted' AND (e.deadline IS NULL OR e.deadline >= ?) AND (i.expires_at IS NULL OR i.expires_at > ?)"""
            args: List[Any] = [user_id, today, now]
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
            event["requirements"] = json.loads(event.pop("requirements_json") or "[]")
            event["materials"] = json.loads(event.pop("materials_json") or "[]")
            event.pop("body", None)
        return result

    async def relevant_events(self, user_id: str, profile: Dict[str, Any], query: str = "", limit: int = 8) -> List[Dict[str, Any]]:
        events = await self.inbox(user_id, profile, "active")
        query = query.strip().lower()
        if query:
            events = [event for event in events if query in f"{event['title']} {event['summary']} {event.get('event_type', '')}".lower()]
        return events[:max(1, min(limit, 20))]

    async def inbox_briefing(self, user_id: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Return the Inbox as a personal decision brief instead of a notice feed.

        Source notices remain immutable records.  The aggregation below is deliberately
        deterministic and explainable: it only groups notices that share an explicit
        subject signal (and, where available, a deadline), then exposes the original
        notices as a timeline.  This is a safe first event layer before introducing a
        learned clustering model.
        """
        notices = await self.inbox(user_id, profile, "active")
        return self._build_briefing(notices, profile)

    @classmethod
    def _build_briefing(cls, notices: List[Dict[str, Any]], profile: Dict[str, Any]) -> Dict[str, Any]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for notice in notices:
            groups.setdefault(cls._cluster_key(notice), []).append(notice)

        events = []
        for members in groups.values():
            members.sort(key=lambda item: (item.get("published_at") or "", item.get("id", 0)), reverse=True)
            primary = dict(members[0])
            category = cls._category(primary)
            score, factors = cls._attention(primary, profile, category)
            timeline = [
                {"id": item["id"], "date": item.get("published_at") or "", "title": item["title"], "source_name": item["source_name"], "source_url": item["source_url"]}
                for item in reversed(members)
            ]
            primary.update({
                "category": category,
                "attention_score": score,
                "attention_factors": factors,
                "notification_count": len(members),
                "timeline": timeline,
                "member_ids": [item["id"] for item in members],
            })
            events.append(primary)

        events.sort(key=lambda item: (-item["attention_score"], item.get("deadline") or "9999-12-31", item.get("published_at") or ""))
        focus = [event for event in events if event["category"] == "action" and event["attention_score"] >= 45][:3]
        focus_ids = {event["id"] for event in focus}
        recommended = [event for event in events if event["id"] not in focus_ids and event["category"] != "announcement"][:6]
        category_order = ("action", "opportunity", "academic", "campus_life")
        categories = [
            {"key": key, "count": sum(1 for event in events if event["category"] == key), "events": [event for event in events if event["category"] == key][:4]}
            for key in category_order
        ]
        other = [event for event in events if event["category"] == "announcement"]
        return {"events": events, "today_focus": focus, "recommended": recommended, "categories": categories, "other": other}

    @staticmethod
    def _cluster_key(event: Dict[str, Any]) -> str:
        text = f"{event.get('title', '')} {event.get('summary', '')}"
        # These terms are event subjects, not publishing departments.  Keeping an
        # explicit subject phrase avoids merging every general notice from one source.
        subjects = ("创新创业", "奖学金", "国家奖学金", "选课", "培养方案", "科研项目", "项目申报", "实习", "招聘", "招聘会", "推免", "保研", "考研", "竞赛", "比赛", "讲座", "社团", "考试")
        subject = next((word for word in subjects if word in text), "")
        deadline = event.get("deadline") or ""
        if subject:
            return f"{subject}:{deadline}"
        normalized = re.sub(r"20\d{2}[年./-]?\d{0,2}[月./-]?\d{0,2}日?|[（(].*?[）)]|\s+", "", event.get("title", ""))
        return f"title:{normalized[:24]}"

    @staticmethod
    def _category(event: Dict[str, Any]) -> str:
        text = f"{event.get('title', '')} {event.get('summary', '')} {event.get('event_type', '')}"
        if event.get("deadline") or any(word in text for word in ("报名", "申请", "提交", "缴费", "确认", "填报", "选课")):
            return "action"
        if any(word in text for word in ("科研", "竞赛", "比赛", "奖学金", "实习", "招聘", "创新创业", "项目申报")):
            return "opportunity"
        if any(word in text for word in ("考试", "课程", "培养方案", "成绩", "教学", "学位")):
            return "academic"
        if any(word in text for word in ("讲座", "社团", "活动", "文体", "生活", "宿舍")):
            return "campus_life"
        return "announcement"

    @staticmethod
    def _attention(event: Dict[str, Any], profile: Dict[str, Any], category: str) -> tuple[int, Dict[str, int]]:
        match = min(55, max(0, int(event.get("relevance", 0))) * 11)
        urgency = 0
        deadline = event.get("deadline")
        if deadline:
            try:
                days = (date.fromisoformat(deadline) - datetime.now().astimezone().date()).days
                urgency = 40 if days <= 0 else 35 if days <= 1 else 28 if days <= 3 else 18 if days <= 7 else 8
            except ValueError:
                urgency = 0
        importance = {"action": 20, "opportunity": 14, "academic": 12, "campus_life": 7, "announcement": 3}[category]
        interest = min(10, 3 * len(profile.get("interests", []))) if event.get("relevance", 0) else 0
        factors = {"匹配度": match, "时间紧迫度": urgency, "事务重要性": importance, "兴趣关联": interest}
        return min(100, sum(factors.values())), factors

    @staticmethod
    def _relevance(event: Dict[str, Any], profile: Dict[str, Any]) -> tuple[int, str]:
        text = f"{event['title']} {event['summary']}"
        score, reasons = 0, []
        if event["source_category"] == "employment" and ("就业" in profile.get("interests", []) or profile.get("grade", "").endswith("届")):
            score += 3
            reasons.append("你关注就业")
        mapping = {"奖学金": ("奖学金", "评优", "资助"), "竞赛": ("竞赛", "比赛", "挑战杯"), "保研": ("推免", "保研"), "就业": ("就业", "招聘", "实习", "选调"), "考研": ("考研", "招生", "调剂"), "出国": ("出国", "留学", "交流")}
        for interest in profile.get("interests", []):
            if any(word in text for word in mapping.get(interest, (interest,))):
                score += 3
                reasons.append(f"你关注{interest}")
        targets = json.loads(event.get("targets_json") or "[]")
        if profile.get("education") and profile["education"] in targets:
            score += 2
            reasons.append(f"面向{profile['education']}")
        if profile.get("college") and profile["college"] in text:
            score += 4
            reasons.append("面向你的学院")
        if any(word in text for word in ("截止", "报名", "申请", "提交")):
            score += 1
        return score, "；".join(dict.fromkeys(reasons)) or "包含需要关注的报名或申请事项"

    async def set_status(self, user_id: str, event_id: int, status: str) -> bool:
        return await self.store._run(self._set_status_sync, user_id, event_id, status)

    async def delete_inbox(self, user_id: str, event_ids: Optional[List[int]] = None) -> int:
        """删除当前用户的 Inbox 条目；公共来源事件保留以供其他用户使用。"""
        return await self.store._run(self._delete_inbox_sync, user_id, event_ids)

    def _delete_inbox_sync(self, user_id: str, event_ids: Optional[List[int]]) -> int:
        with self.store._connect() as conn:
            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if event_ids:
                unique_ids = list(dict.fromkeys(event_ids))
                placeholders = ",".join("?" for _ in unique_ids)
                cur = conn.execute(f"UPDATE campus_inbox SET status='deleted', updated_at=? WHERE user_id=? AND event_id IN ({placeholders}) AND status != 'deleted'", [now, user_id, *unique_ids])
            else:
                cur = conn.execute("UPDATE campus_inbox SET status='deleted', updated_at=? WHERE user_id=? AND status != 'deleted'", (now, user_id))
        return cur.rowcount

    def _set_status_sync(self, user_id: str, event_id: int, status: str) -> bool:
        with self.store._connect() as conn:
            cur = conn.execute("UPDATE campus_inbox SET status=?, updated_at=? WHERE user_id=? AND event_id=?", (status, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"), user_id, event_id))
        return cur.rowcount > 0

    async def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        return await self.store._run(self._get_event_sync, event_id)

    def _get_event_sync(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM campus_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return None
        event = dict(row)
        for key in ("targets", "actions", "requirements", "materials"):
            event[key] = json.loads(event.pop(f"{key}_json") or "[]")
        return event

    async def create_action_plan(self, user_id: str, event_id: int, personal_service: Any) -> Dict[str, Any]:
        """仅把 Event 中已有的 actions/materials 转为个人事项，绝不补造步骤。"""
        event = await self.get_event(event_id)
        if not event:
            raise ValueError("通知不存在")
        import uuid
        plan_id = uuid.uuid4().hex[:12]
        steps: List[str] = []
        for material in event["materials"]:
            steps.append(material if material.startswith(("准备", "整理", "获取")) else f"准备{material}")
        steps.extend(event["actions"])
        steps = list(dict.fromkeys(step for step in steps if step))
        if not steps:
            steps = [event["title"]]
        items = []
        for index, step in enumerate(steps):
            is_final = index == len(steps) - 1
            item = await personal_service.add_todo(
                user_id, step, kind="ddl" if is_final and event.get("deadline") else "todo",
                due_at=event.get("deadline") if is_final else None,
                source_event_id=event_id, source_url=event["source_url"], source_deadline=event.get("deadline"), action_plan_id=plan_id,
            )
            items.append(item)
        await self.set_status(user_id, event_id, "interested")
        return {"plan_id": plan_id, "event": event, "items": items, "evidence_limited": not bool(event["materials"] or event["actions"])}
