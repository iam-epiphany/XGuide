"""公开校园通知的数据源 Adapter 边界。

Adapter 只负责“从哪里发现通知、怎样取得正文”；Radar 不再知道具体网站结构。
后续学院/学工/就业等公开站点只需增加一个 Adapter，不影响事件、画像或个人计划层。
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import List, Optional, Sequence
from urllib.parse import urljoin, urlparse

import httpx


@dataclass(frozen=True)
class NoticeLink:
    url: str
    title: str
    source_name: str
    source_category: str
    # 列表页通常比正文更稳定地提供发布时间；保留它避免把正文中的历史日期误作发布时间。
    published_at: Optional[str] = None


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
        self.links: List[tuple[str, str, str]] = []
        self._href = ""
        self._title = ""
        self._parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attributes = dict(attrs)
            self._href = attributes.get("href") or ""
            self._title = attributes.get("title") or ""
            self._parts = []

    def handle_data(self, data):
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            text = re.sub(r"\s+", " ", "".join(self._parts)).strip()
            self.links.append((self._href, self._title.strip() or text, text))
            self._href, self._title, self._parts = "", "", []


def html_text(html: str) -> str:
    html = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


def _listing_date(text: str) -> Optional[str]:
    """从列表项的日期文本规范化为 ISO 日期。"""
    match = re.search(r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})", text)
    if not match:
        return None
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _notice_title(title: str) -> str:
    """移除列表项前置日期，避免它被当作通知标题的一部分。"""
    return re.sub(r"^\s*20\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*(?:日)?\s*", "", title).strip()


class HtmlNoticeAdapter(PublicSourceAdapter):
    """通用 HTML 列表 Adapter，适合多数不要求登录的院校公开通知页。"""

    def __init__(
        self,
        *,
        name: str,
        category: str,
        listing_url: str,
        max_links: int = 25,
        include_url_patterns: Sequence[str] = (),
        include_title_patterns: Sequence[str] = (),
    ):
        self.name, self.category, self.listing_url, self.max_links = name, category, listing_url, max_links
        self.include_url_patterns = tuple(re.compile(pattern, re.I) for pattern in include_url_patterns)
        self.include_title_patterns = tuple(re.compile(pattern, re.I) for pattern in include_title_patterns)

    async def discover(self, client: httpx.AsyncClient) -> List[NoticeLink]:
        response = await client.get(self.listing_url)
        response.raise_for_status()
        parser = _AnchorParser()
        parser.feed(response.text)
        host = urlparse(self.listing_url).netloc
        links: List[NoticeLink] = []
        seen = set()
        for href, title, listing_text in parser.links:
            title = _notice_title(title)
            url = urljoin(self.listing_url, href)
            if (not title or len(title) < 8 or urlparse(url).netloc != host or url.rstrip("/") == self.listing_url.rstrip("/")
                    or title in {"更多", "首页", "详情", self.name} or url in seen):
                continue
            if any(value in url.lower() for value in ("javascript:", "login", "register")):
                continue
            if self.include_url_patterns and not any(pattern.search(url) for pattern in self.include_url_patterns):
                continue
            if self.include_title_patterns and not any(pattern.search(title) for pattern in self.include_title_patterns):
                continue
            seen.add(url)
            links.append(NoticeLink(
                url=url,
                title=title,
                source_name=self.name,
                source_category=self.category,
                published_at=_listing_date(listing_text),
            ))
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
        # 这些公开页面均提供服务端渲染的通知列表；不要从门户首页泛抓导航和专题链接。
        HtmlNoticeAdapter(
            name="西电新闻网", category="school", listing_url="https://news.xidian.edu.cn/",
            include_url_patterns=(r"/info/\d+/\d+\.htm$",),
            include_title_patterns=(r"通知|公告|公示|报名|申请|招聘|选课",),
        ),
        HtmlNoticeAdapter(
            name="本科生院", category="academic", listing_url="https://jwc.xidian.edu.cn/tzgg.htm",
            include_url_patterns=(r"/info/1012/\d+\.htm$",),
        ),
        HtmlNoticeAdapter(
            # 该站的分类页由前端异步加载，首页服务端已输出同一份通知公告列表。
            name="西电就业信息网", category="employment", listing_url="https://job.xidian.edu.cn/",
            include_url_patterns=(r"/news/view/aid/\d+/tag/tzgg$",),
        ),
    ]
