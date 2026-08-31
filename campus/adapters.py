"""公开校园通知的数据源 Adapter 边界。

Adapter 只负责“从哪里发现通知、怎样取得正文”；Radar 不再知道具体网站结构。
后续学院/学工/就业等公开站点只需增加一个 Adapter，不影响事件、画像或个人计划层。
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import httpx


@dataclass(frozen=True)
class NoticeLink:
    url: str
    title: str
    source_name: str
    source_category: str


@dataclass(frozen=True)
class NoticePage:
    link: NoticeLink
    body: str
    etag: Optional[str]
    last_modified: Optional[str]
    not_modified: bool = False


class PublicSourceAdapter:
    name: str
    category: str

    async def discover(self, client: httpx.AsyncClient) -> List[NoticeLink]:
        raise NotImplementedError

    async def fetch(self, client: httpx.AsyncClient, link: NoticeLink, *, etag: Optional[str] = None, last_modified: Optional[str] = None) -> NoticePage:
        raise NotImplementedError


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[tuple[str, str]] = []
        self._href = ""
        self._parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._parts = []

    def handle_data(self, data):
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, re.sub(r"\s+", " ", "".join(self._parts)).strip()))
            self._href, self._parts = "", []


def html_text(html: str) -> str:
    html = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


class HtmlNoticeAdapter(PublicSourceAdapter):
    """通用 HTML 列表 Adapter，适合多数不要求登录的院校公开通知页。"""

    def __init__(self, *, name: str, category: str, listing_url: str, max_links: int = 25):
        self.name, self.category, self.listing_url, self.max_links = name, category, listing_url, max_links

    async def discover(self, client: httpx.AsyncClient) -> List[NoticeLink]:
        response = await client.get(self.listing_url)
        response.raise_for_status()
        parser = _AnchorParser()
        parser.feed(response.text)
        host = urlparse(self.listing_url).netloc
        links: List[NoticeLink] = []
        seen = set()
        for href, title in parser.links:
            url = urljoin(self.listing_url, href)
            if (not title or len(title) < 8 or urlparse(url).netloc != host or url.rstrip("/") == self.listing_url.rstrip("/")
                    or title in {"更多", "首页", "详情", self.name} or url in seen):
                continue
            if any(value in url.lower() for value in ("javascript:", "login", "register")):
                continue
            seen.add(url)
            links.append(NoticeLink(url=url, title=title, source_name=self.name, source_category=self.category))
            if len(links) >= self.max_links:
                break
        return links

    async def fetch(self, client: httpx.AsyncClient, link: NoticeLink, *, etag: Optional[str] = None, last_modified: Optional[str] = None) -> NoticePage:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        response = await client.get(link.url, headers=headers)
        if response.status_code == 304:
            return NoticePage(link=link, body="", etag=etag, last_modified=last_modified, not_modified=True)
        response.raise_for_status()
        return NoticePage(link=link, body=html_text(response.text)[:12000], etag=response.headers.get("ETag"), last_modified=response.headers.get("Last-Modified"))


def default_public_adapters() -> List[PublicSourceAdapter]:
    return [
        HtmlNoticeAdapter(name="西电新闻网", category="school", listing_url="https://news.xidian.edu.cn/"),
        HtmlNoticeAdapter(name="本科生院", category="academic", listing_url="https://jwc.xidian.edu.cn/"),
        HtmlNoticeAdapter(name="西电就业信息网", category="employment", listing_url="https://job.xidian.edu.cn/"),
    ]
