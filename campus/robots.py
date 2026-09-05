"""robots.txt 缓存与检查：对公开来源保持礼貌采集。

采集只针对无需登录的公开页面；遵守 robots.txt 既是对源站的基本礼貌，
也降低被反爬封禁导致同步不可用的概率。每个 origin 只抓取一次 robots.txt，
结果按 origin 缓存：404/读取失败视为全允许（无声明 = 未禁止）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

# 哨兵：origin 尚未加载（区别于 None=已加载且全允许）
_UNLOADED = object()


class RobotsCache:
    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout
        # origin → 解析结果；None 表示 robots.txt 不存在/不可达（全允许）
        self._cache: Dict[str, Optional[RobotFileParser]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, origin: str) -> asyncio.Lock:
        lock = self._locks.get(origin)
        if lock is None:
            lock = self._locks.setdefault(origin, asyncio.Lock())
        return lock

    async def allowed(self, client: httpx.AsyncClient, url: str, user_agent: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if self._cache.get(origin, _UNLOADED) is _UNLOADED:
            async with self._lock_for(origin):
                if self._cache.get(origin, _UNLOADED) is _UNLOADED:
                    self._cache[origin] = await self._load(client, origin, user_agent)
        parser = self._cache.get(origin)
        if parser is None:
            return True
        try:
            return parser.can_fetch(user_agent, url)
        except Exception:  # 解析异常不阻断采集（保守放行，与"无声明"语义一致）
            return True

    async def _load(self, client: httpx.AsyncClient, origin: str, user_agent: str) -> Optional[RobotFileParser]:
        url = f"{origin}/robots.txt"
        try:
            response = await client.get(url, timeout=self._timeout)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
            return parser
        except Exception as ex:
            logger.debug("robots.txt 读取失败（按全允许处理）%s: %s", url, ex)
            return None
