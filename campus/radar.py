"""校园通知雷达：采集公开网页、去重保存、按稳定学生画像生成 Inbox。

该模块只访问无需登录的官方页面。采集失败不会影响个人日程；前端会明确
显示上次同步结果，通知始终保留来源链接以便回到官方原文核验。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import os
import random
import re
from typing import Any, Dict, List, Optional

import httpx

from campus.adapters import PublicSourceAdapter, default_public_adapters
from campus import semantic
from campus.extractor import CampusEventExtractor, classify_category
from campus.robots import RobotsCache
from personal.store import PersonalStore

logger = logging.getLogger(__name__)

RADAR_USER_AGENT = "XGuide Campus Radar/1.0 (+public information only)"
# 详情页逐条间隔（秒）+ 随机抖动：对源站友好，降低被反爬封禁的概率
DETAIL_FETCH_INTERVAL = float(os.getenv("ECHOGUIDE_RADAR_FETCH_INTERVAL", "1.2"))

# 兼容既有脚本/测试的可读配置；实际同步由 adapters 调度。
SOURCES = [
    {"name": adapter.name, "category": adapter.category, "url": adapter.listing_url}
    for adapter in default_public_adapters()
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
    requirements_json TEXT NOT NULL DEFAULT '[]',
    materials_json TEXT NOT NULL DEFAULT '[]',
    location TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '通知',
    category TEXT NOT NULL DEFAULT '',
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


def _date_in(text: str) -> Optional[str]:
    match = re.search(r"(20\d{2})[年./-]\s?(\d{1,2})[月./-]\s?(\d{1,2})", text)
    if not match:
        return None
    month, day = int(match.group(2)), int(match.group(3))
    # "2024年13月32日" 这类伪日期直接拒绝入库（deadline 的字符串比较行为才可预期）
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{match.group(1)}-{month:02d}-{day:02d}"


class CampusRadar:
    def __init__(
        self,
        personal_store: PersonalStore,
        *,
        adapters: Optional[List[PublicSourceAdapter]] = None,
        extractor: Optional[CampusEventExtractor] = None,
        inbox_ttl_hours: int = 48,
        robots: Optional[RobotsCache] = None,
    ):
        self.store = personal_store
        self.db_path = personal_store.db_path
        self.adapters = adapters or default_public_adapters()
        self.extractor = extractor or CampusEventExtractor()
        self.inbox_ttl_hours = max(1, min(int(inbox_ttl_hours), 24 * 30))
        # robots.txt 缓存（per-origin 一次）与刷新并发防护；可注入以便测试替身
        self._robots = robots or RobotsCache()
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_at: Optional[datetime] = None
        self._last_refresh_result: Optional[Dict[str, Any]] = None
        with self.store._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(campus_events)")}
            for name, definition in {
                "requirements_json": "TEXT NOT NULL DEFAULT '[]'",
                "materials_json": "TEXT NOT NULL DEFAULT '[]'",
                "location": "TEXT NOT NULL DEFAULT ''",
                "event_type": "TEXT NOT NULL DEFAULT '通知'",
                "category": "TEXT NOT NULL DEFAULT ''",
                "content_hash": "TEXT NOT NULL DEFAULT ''",
                "etag": "TEXT",
                "last_modified": "TEXT",
                "last_checked_at": "TEXT",
                "updated_at": "TEXT",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE campus_events ADD COLUMN {name} {definition}")
            inbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(campus_inbox)")}
            if "expires_at" not in inbox_columns:
                conn.execute("ALTER TABLE campus_inbox ADD COLUMN expires_at TEXT")

    async def refresh(self, force: bool = False) -> Dict[str, Any]:
        """全量同步公开通知。

        进程内互斥：已有一轮 refresh 在跑时，后续调用不再重复抓取（手动端点
        与每日定时任务叠加、或用户连点时），直接返回进行中/上次结果。
        冷却判定不在本方法内——由 /inbox/refresh 端点决定是否复用缓存结果，
        供测试/内部调用的直接 refresh() 始终真实执行。
        """
        if self._refresh_lock.locked():
            base = self._last_refresh_result or self._empty_result()
            return {**base, "in_progress": True}
        async with self._refresh_lock:
            result = await self._refresh_all()
            self._last_refresh_at = datetime.now().astimezone()
            self._last_refresh_result = result
            return result

    async def _refresh_all(self) -> Dict[str, Any]:
        results = await asyncio.gather(
            *(self._refresh_adapter(adapter) for adapter in self.adapters), return_exceptions=True
        )
        inserted, updated, unchanged, checked, failed, skipped, errors = 0, 0, 0, 0, 0, 0, []
        for adapter, result in zip(self.adapters, results, strict=False):
            if isinstance(result, Exception):
                errors.append({"source": adapter.name, "message": str(result)[:180]})
                continue
            checked += result["checked"]
            inserted += result["new"]
            updated += result["updated"]
            unchanged += result["unchanged"]
            failed += result.get("failed", 0)
            skipped += result.get("skipped_by_robots", 0)
            if result.get("failed"):
                errors.append({"source": adapter.name, "message": f"{result['failed']} 条通知读取失败"})
        return {
            "sources": len(self.adapters),
            "checked": checked,
            "new_events": inserted,
            "updated_events": updated,
            "unchanged": unchanged,
            "failed": failed,
            "skipped_by_robots": skipped,
            "errors": errors,
        }

    def _empty_result(self) -> Dict[str, Any]:
        return {
            "sources": len(self.adapters),
            "checked": 0,
            "new_events": 0,
            "updated_events": 0,
            "unchanged": 0,
            "failed": 0,
            "skipped_by_robots": 0,
            "errors": [],
        }

    def cached_result(self, max_age_seconds: float = 600) -> Optional[Dict[str, Any]]:
        """冷却期内的上次同步结果；无结果或已超冷却期返回 None。

        /inbox/refresh 端点用它把冷却期内的重复触发合并为一次真实抓取，
        避免高频手动刷新放大对源站的请求量。
        """
        if self._last_refresh_result is None or self._last_refresh_at is None:
            return None
        elapsed = (datetime.now().astimezone() - self._last_refresh_at).total_seconds()
        if elapsed > max_age_seconds:
            return None
        return {**self._last_refresh_result, "cached": True}

    async def _refresh_adapter(self, adapter: PublicSourceAdapter) -> Dict[str, int]:
        counts = {"checked": 0, "new": 0, "updated": 0, "unchanged": 0, "failed": 0, "skipped_by_robots": 0}
        async with httpx.AsyncClient(
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": RADAR_USER_AGENT},
        ) as client:
            # robots.txt：列表页被禁止时整源跳过（仅公开来源采集，遵守声明是前提）
            listing_url = getattr(adapter, "listing_url", None)
            if listing_url and not await self._robots.allowed(client, listing_url, RADAR_USER_AGENT):
                logger.info("来源 %s 的 robots.txt 禁止采集列表页，本轮跳过", adapter.name)
                counts["skipped_by_robots"] += 1
                return counts
            links = await adapter.discover(client)
            for link in links:
                counts["checked"] += 1
                # 逐条限速 + 抖动：详情页一次请求一次等待，避免突发并发打源站
                await asyncio.sleep(DETAIL_FETCH_INTERVAL + random.random())
                # 单页异常（网络 / 解析 / 存储）都不应中止同一来源的其余通知同步
                try:
                    if not await self._robots.allowed(client, link.url, RADAR_USER_AGENT):
                        counts["skipped_by_robots"] += 1
                        continue
                    prior = await self.store._run(self._event_sync, link.url)
                    page = await adapter.fetch(
                        client,
                        link,
                        etag=prior.get("etag") if prior else None,
                        last_modified=prior.get("last_modified") if prior else None,
                    )
                except (httpx.HTTPError, ValueError):
                    counts["failed"] += 1
                    continue
                except Exception as ex:  # SQLite 等其他异常同样只算单条失败
                    counts["failed"] += 1
                    logger.warning("通知读取/入库异常 %s: %s", link.url, ex)
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
                state = await self.store._run(
                    self._upsert_event_sync,
                    {
                        "url": link.url,
                        "title": link.title,
                        "body": page.body,
                        "source_name": link.source_name,
                        "source_category": link.source_category,
                        "published_at": link.published_at,
                        "etag": page.etag,
                        "last_modified": page.last_modified,
                        "content_hash": content_hash,
                        **fields,
                    },
                )
                counts[state] += 1
        return counts

    def _event_sync(self, url: str) -> Optional[Dict[str, Any]]:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM campus_events WHERE source_url=?", (url,)).fetchone()
        return dict(row) if row else None

    def _touch_checked_sync(self, url: str, etag: Optional[str] = None, last_modified: Optional[str] = None) -> None:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE campus_events SET last_checked_at=?, etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified) WHERE source_url=?",
                (now, etag, last_modified, url),
            )

    def _upsert_event_sync(self, event: Dict[str, Any]) -> str:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        existing = self._event_sync(event["url"])
        with self.store._connect() as conn:
            values = (
                hashlib.sha256(event["url"].encode()).hexdigest(),
                event["title"],
                event["summary"],
                event["body"],
                event["source_name"],
                event["source_category"],
                event["url"],
                # published_at 只信列表页给出的日期；正文/标题里的日期兜底会把
                # 历史日期（如往年同活动的举办时间）误作发布时间（见 adapters.NoteLink 注释）
                event.get("published_at"),
                event.get("deadline"),
                json.dumps(event.get("targets", []), ensure_ascii=False),
                json.dumps(event.get("actions", []), ensure_ascii=False),
                json.dumps(event.get("requirements", []), ensure_ascii=False),
                json.dumps(event.get("materials", []), ensure_ascii=False),
                event.get("location", ""),
                event.get("event_type", "通知"),
                event.get("category", ""),
                event["content_hash"],
                event.get("etag"),
                event.get("last_modified"),
                now,
                now,
                now,
            )
            conn.execute(
                """INSERT INTO campus_events (fingerprint,title,summary,body,source_name,source_category,source_url,published_at,deadline,targets_json,actions_json,requirements_json,materials_json,location,event_type,category,content_hash,etag,last_modified,fetched_at,last_checked_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET title=excluded.title,summary=excluded.summary,body=excluded.body,published_at=excluded.published_at,deadline=excluded.deadline,targets_json=excluded.targets_json,actions_json=excluded.actions_json,requirements_json=excluded.requirements_json,materials_json=excluded.materials_json,location=excluded.location,event_type=excluded.event_type,category=excluded.category,content_hash=excluded.content_hash,etag=excluded.etag,last_modified=excluded.last_modified,last_checked_at=excluded.last_checked_at,updated_at=excluded.updated_at""",
                values,
            )
        return "updated" if existing else "new"

    def _save_events_sync(self, events: List[Dict[str, Any]]) -> int:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        with self.store._connect() as conn:
            for event in events:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO campus_events
                    (fingerprint,title,summary,body,source_name,source_category,source_url,published_at,deadline,targets_json,actions_json,fetched_at,last_checked_at,updated_at,content_hash)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event["fingerprint"],
                        event["title"],
                        event["summary"],
                        event["body"],
                        event["source_name"],
                        event["source_category"],
                        event["source_url"],
                        event["published_at"],
                        event["deadline"],
                        json.dumps(event["targets"], ensure_ascii=False),
                        json.dumps(event["actions"], ensure_ascii=False),
                        now,
                        now,
                        now,
                        hashlib.sha256(event["body"].encode()).hexdigest(),
                    ),
                )
                inserted += cur.rowcount
        return inserted

    async def inbox(self, user_id: str, profile: Dict[str, Any], status: str = "active") -> List[Dict[str, Any]]:
        return await self.store._run(self._inbox_sync, user_id, profile, status)

    def _inbox_sync(self, user_id: str, profile: Dict[str, Any], status: str) -> List[Dict[str, Any]]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM campus_events ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 160"
            ).fetchall()
            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            today = now[:10]
            expires_at = (datetime.now().astimezone() + timedelta(hours=self.inbox_ttl_hours)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            # 未设置学历或关注方向时，不应让用户面对空收件箱。展示最近 24 小时
            # 实际同步检查到的公开通知；保存画像后才启用下面的个性化阈值筛选。
            profile_ready = bool(profile.get("education") or profile.get("interests"))
            recent_cutoff = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            # 语义加成：本地嵌入计算画像与通知的相似度；模型不可用时返回空 dict，
            # 打分完全退化为既有关键词规则（见 campus/semantic.py 的设计说明）。
            try:
                boosts = semantic.relevance_boost([dict(row) for row in rows], profile)
            except Exception as ex:  # 语义层任何异常都不能阻断 Inbox 生成
                logger.warning("语义相关度加成失败（退化为纯关键词）: %s", ex)
                boosts = {}
            for row in rows:
                event = dict(row)
                # 对具备明确日期的赛事/通知，仅在截止当天及之前进入 Inbox；
                # 旧的 inbox 记录也会在下面查询中排除，避免过期赛事再次被“推送”。
                if event.get("deadline") and event["deadline"] < today:
                    continue
                if profile_ready:
                    relevance, reason = self._relevance(event, profile)
                    sim, bonus, why = boosts.get(event["id"], (0.0, 0, ""))
                    if bonus:
                        relevance += bonus
                        reason = f"{reason}；{why}"
                    if relevance < 2:
                        continue
                else:
                    last_seen = event.get("last_checked_at") or event.get("fetched_at") or ""
                    if last_seen < recent_cutoff:
                        continue
                    relevance, reason = 1, "尚未设置通知筛选条件，展示最近 24 小时同步的公开通知"
                if relevance < 1:
                    continue
                conn.execute(
                    """INSERT INTO campus_inbox (user_id,event_id,relevance,reason,status,expires_at,updated_at) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,event_id) DO UPDATE SET relevance=excluded.relevance, reason=excluded.reason,
                    expires_at=COALESCE(campus_inbox.expires_at, excluded.expires_at)""",
                    (user_id, event["id"], relevance, reason, "new", expires_at, now),
                )
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

    async def relevant_events(
        self, user_id: str, profile: Dict[str, Any], query: str = "", limit: int = 8
    ) -> List[Dict[str, Any]]:
        events = await self.inbox(user_id, profile, "active")
        if not query.strip():
            return events[: max(1, min(limit, 20))]
        # 优先语义检索（同义词/换一种说法也能召回）；嵌入模型不可用时
        # 回退到原有的子串过滤，行为不变。
        ranked = semantic.rank_by_query(events, query)
        if ranked is None:
            needle = query.strip().lower()
            ranked = [
                event
                for event in events
                if needle in f"{event['title']} {event['summary']} {event.get('event_type', '')}".lower()
            ]
        return ranked[: max(1, min(limit, 20))]

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
        # 关键词分组之外的补充合并：语义高度相似（同一事件的系列通知）并为一组；
        # 嵌入模型不可用时原样返回，行为与纯关键词版本一致。
        groups = semantic.merge_similar_groups(groups)

        events = []
        for members in groups.values():
            members.sort(key=lambda item: (item.get("published_at") or "", item.get("id", 0)), reverse=True)
            primary = dict(members[0])
            # 优先使用抽取阶段入库的 category（LLM 判定或规则判定），
            # 旧数据列为空时回退到规则分类。
            category = primary.get("category") or cls._category(primary)
            score, factors = cls._attention(primary, profile, category)
            timeline = [
                {
                    "id": item["id"],
                    "date": item.get("published_at") or "",
                    "title": item["title"],
                    "source_name": item["source_name"],
                    "source_url": item["source_url"],
                }
                for item in reversed(members)
            ]
            primary.update(
                {
                    "category": category,
                    "attention_score": score,
                    "attention_factors": factors,
                    "notification_count": len(members),
                    "timeline": timeline,
                    "member_ids": [item["id"] for item in members],
                }
            )
            events.append(primary)

        events.sort(
            key=lambda item: (
                -item["attention_score"],
                item.get("deadline") or "9999-12-31",
                item.get("published_at") or "",
            )
        )
        focus = [event for event in events if event["category"] == "action" and event["attention_score"] >= 45][:3]
        focus_ids = {event["id"] for event in focus}
        recommended = [
            event for event in events if event["id"] not in focus_ids and event["category"] != "announcement"
        ][:6]
        category_order = ("action", "opportunity", "academic", "campus_life")
        categories = [
            {
                "key": key,
                "count": sum(1 for event in events if event["category"] == key),
                "events": [event for event in events if event["category"] == key][:4],
            }
            for key in category_order
        ]
        other = [event for event in events if event["category"] == "announcement"]
        return {
            "events": events,
            "today_focus": focus,
            "recommended": recommended,
            "categories": categories,
            "other": other,
        }

    @staticmethod
    def _cluster_key(event: Dict[str, Any]) -> str:
        text = f"{event.get('title', '')} {event.get('summary', '')}"
        # These terms are event subjects, not publishing departments.  Keeping an
        # explicit subject phrase avoids merging every general notice from one source.
        # 按长度降序：更具体的词形必须先命中（"国家奖学金"不能被"奖学金"抢走分组）
        subjects = sorted(
            (
                "创新创业",
                "国家奖学金",
                "奖学金",
                "培养方案",
                "科研项目",
                "项目申报",
                "招聘会",
                "选课",
                "实习",
                "招聘",
                "推免",
                "保研",
                "考研",
                "竞赛",
                "比赛",
                "讲座",
                "社团",
                "考试",
            ),
            key=len,
            reverse=True,
        )
        subject = next((word for word in subjects if word in text), "")
        deadline = event.get("deadline") or ""
        if subject:
            return f"{subject}:{deadline}"
        normalized = re.sub(r"20\d{2}[年./-]?\d{0,2}[月./-]?\d{0,2}日?|[（(].*?[）)]|\s+", "", event.get("title", ""))
        return f"title:{normalized[:24]}"

    @staticmethod
    def _category(event: Dict[str, Any]) -> str:
        """规则分类兜底；抽取阶段 LLM 已产出的 category 优先（见 _build_briefing）。"""
        return classify_category(
            event.get("title", ""), event.get("summary", ""), event.get("event_type", ""), event.get("deadline")
        )

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
        # 兴趣关联按"实际命中的兴趣数"计分：按兴趣条数计分会让配置了多个兴趣的
        # 用户对任何相关事件一律拿满分，排序失真
        text = f"{event.get('title', '')} {event.get('summary', '')}"
        matched = sum(1 for interest in profile.get("interests", []) if interest and str(interest) in text)
        interest = min(10, 3 * matched)
        factors = {"匹配度": match, "时间紧迫度": urgency, "事务重要性": importance, "兴趣关联": interest}
        return min(100, sum(factors.values())), factors

    @staticmethod
    def _relevance(event: Dict[str, Any], profile: Dict[str, Any]) -> tuple[int, str]:
        text = f"{event['title']} {event['summary']}"
        score, reasons = 0, []
        if event["source_category"] == "employment" and (
            "就业" in profile.get("interests", []) or profile.get("grade", "").endswith("届")
        ):
            score += 3
            reasons.append("你关注就业")
        mapping = {
            "奖学金": ("奖学金", "评优", "资助"),
            "竞赛": ("竞赛", "比赛", "挑战杯"),
            "保研": ("推免", "保研"),
            "就业": ("就业", "招聘", "实习", "选调"),
            "考研": ("考研", "招生", "调剂"),
            "出国": ("出国", "留学", "交流"),
        }
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
                cur = conn.execute(
                    f"UPDATE campus_inbox SET status='deleted', updated_at=? WHERE user_id=? AND event_id IN ({placeholders}) AND status != 'deleted'",
                    [now, user_id, *unique_ids],
                )
            else:
                cur = conn.execute(
                    "UPDATE campus_inbox SET status='deleted', updated_at=? WHERE user_id=? AND status != 'deleted'",
                    (now, user_id),
                )
        return cur.rowcount

    def _set_status_sync(self, user_id: str, event_id: int, status: str) -> bool:
        with self.store._connect() as conn:
            cur = conn.execute(
                "UPDATE campus_inbox SET status=?, updated_at=? WHERE user_id=? AND event_id=?",
                (status, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"), user_id, event_id),
            )
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
        """由通知生成个人行动计划。

        主体仍是 Event 中已有的 materials/actions（可追溯，绝不凭空补造）。
        在此之上，若 extractor 配置了 LLM，则允许它基于原文起草补充步骤，
        但每一步必须携带原文逐字依据（evidence），依据不在原文中的步骤
        直接丢弃 —— 见 CampusEventExtractor.plan_steps 的校验说明。
        """
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
        # LLM 补充步骤（可选增强）：drafted 为空 = LLM 不可用或没有通过校验的步骤
        drafted: List[Dict[str, str]] = []
        evidence_map: Dict[str, str] = {}
        if getattr(self.extractor, "llm_ready", False) and hasattr(self.extractor, "plan_steps"):
            source_ws_steps = list(steps)
            try:
                drafted = await self.extractor.plan_steps(event["title"], event["body"], source_ws_steps)
            except Exception as ex:
                logger.warning("行动步骤起草失败（保持纯证据计划）: %s", ex)
                drafted = []
            for item in drafted:
                step = item["step"]
                steps.append(step)
                evidence_map[step] = item["evidence"]
        items = []
        for index, step in enumerate(steps):
            is_final = index == len(steps) - 1
            item = await personal_service.add_todo(
                user_id,
                step,
                kind="ddl" if is_final and event.get("deadline") else "todo",
                due_at=event.get("deadline") if is_final else None,
                source_event_id=event_id,
                source_url=event["source_url"],
                source_deadline=event.get("deadline"),
                action_plan_id=plan_id,
                evidence=evidence_map.get(step),
            )
            items.append(item)
        await self.set_status(user_id, event_id, "interested")
        return {
            "plan_id": plan_id,
            "event": event,
            "items": items,
            "evidence_limited": not bool(event["materials"] or event["actions"] or drafted),
            "llm_steps": len(drafted),
        }
