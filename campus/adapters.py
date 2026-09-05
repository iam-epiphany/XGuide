"""公开校园通知的数据源 Adapter 边界。

Adapter 只负责“从哪里发现通知、怎样取得正文”；Radar 不再知道具体网站结构。
后续学院/学工/就业等公开站点只需增加一个 Adapter，不影响事件、画像或个人计划层。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
import re
from typing import Dict, List, Optional, Sequence
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

    async def fetch(
        self,
        client: httpx.AsyncClient,
        link: NoticeLink,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> NoticePage:
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


class _TableParser(HTMLParser):
    """只保留表格单元格文本，供赛程类公开页面做行级拆分。"""

    def __init__(self) -> None:
        super().__init__()
        self.tables: List[List[List[str]]] = []
        self._table_depth = 0
        self._rows: List[List[str]] = []
        self._cells: List[str] = []
        self._cell_parts: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
        elif self._table_depth and tag == "tr":
            self._cells = []
        elif self._table_depth and tag in {"td", "th"}:
            self._cell_parts = []
        elif self._cell_parts is not None and tag == "br":
            self._cell_parts.append(" ")

    def handle_data(self, data):
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag):
        if self._table_depth and tag in {"td", "th"} and self._cell_parts is not None:
            self._cells.append(re.sub(r"\s+", " ", "".join(self._cell_parts)).strip())
            self._cell_parts = None
        elif self._table_depth and tag == "tr" and self._cells:
            self._rows.append(self._cells)
            self._cells = []
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._rows:
                self.tables.append(self._rows)
                self._rows = []


def html_text(html: str) -> str:
    html = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


def decode_response_text(response: httpx.Response) -> str:
    """按响应头 / HTML meta 探测编码解码正文。

    httpx 默认按 utf-8 解码 text；不少高校老站点是 GB2312/GBK，
    乱码后标题匹配不到任何通知 → 整轮静默同步 0 条，且结果里连 failed 都不是。
    """
    if response.charset_encoding:
        return response.text
    match = re.search(rb'charset\s*=\s*["\']?([\w-]+)', response.content[:2048], re.I)
    if match:
        try:
            return response.content.decode(match.group(1).decode("ascii"), errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    return response.text  # 无任何编码声明 → 维持 httpx 默认（utf-8）


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
        parser.feed(decode_response_text(response))
        host = urlparse(self.listing_url).netloc
        links: List[NoticeLink] = []
        seen = set()
        for href, title, listing_text in parser.links:
            title = _notice_title(title)
            url = urljoin(self.listing_url, href)
            if (
                not title
                or len(title) < 8
                or urlparse(url).netloc != host
                or url.rstrip("/") == self.listing_url.rstrip("/")
                or title in {"更多", "首页", "详情", self.name}
                or url in seen
            ):
                continue
            if any(value in url.lower() for value in ("javascript:", "login", "register")):
                continue
            if self.include_url_patterns and not any(pattern.search(url) for pattern in self.include_url_patterns):
                continue
            if self.include_title_patterns and not any(
                pattern.search(title) for pattern in self.include_title_patterns
            ):
                continue
            seen.add(url)
            links.append(
                NoticeLink(
                    url=url,
                    title=title,
                    source_name=self.name,
                    source_category=self.category,
                    published_at=_listing_date(listing_text),
                )
            )
            if len(links) >= self.max_links:
                break
        return links

    async def fetch(
        self,
        client: httpx.AsyncClient,
        link: NoticeLink,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> NoticePage:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        response = await client.get(link.url, headers=headers)
        if response.status_code == 304:
            return NoticePage(link=link, body="", etag=etag, last_modified=last_modified, not_modified=True)
        response.raise_for_status()
        return NoticePage(
            link=link,
            body=html_text(decode_response_text(response))[:12000],
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )


class CPIPCCompetitionAdapter(PublicSourceAdapter):
    """将研创网年度赛程表拆为可推荐的单项主题赛事。

    研创网的公开赛程是一张表；若作为普通通知采集，会丢失“哪项比赛、哪天报名截止”
    的关系。这个 Adapter 在来源边界完成行级拆分，每行仍保留同一份官方赛程原文链接。
    """

    name = "中国研究生创新实践系列大赛"
    category = "competition"
    listing_url = "https://cpipc.acge.org.cn/pw/notice/list/1"
    _fallback_schedule_url = "https://cpipc.acge.org.cn/pw/notice/detail/2c9080179e403028019e484cf50210a6?page=0"

    def __init__(self, *, listing_url: Optional[str] = None, fallback_schedule_url: Optional[str] = None):
        self.listing_url = listing_url or self.listing_url
        self._fallback_schedule_url = fallback_schedule_url or self._fallback_schedule_url
        self._bodies: Dict[str, str] = {}

    async def discover(self, client: httpx.AsyncClient) -> List[NoticeLink]:
        schedule_url = await self._schedule_url(client)
        response = await client.get(schedule_url)
        response.raise_for_status()
        html = decode_response_text(response)
        year = self._schedule_year(html)
        published_at = self._published_at(html)
        rows = self._schedule_rows(html)
        links: List[NoticeLink] = []
        self._bodies = {}
        for position, (competition, registration, submission, final) in enumerate(rows, start=1):
            deadline = self._registration_deadline(registration, year)
            if deadline is None:
                # 没有确定报名截止日时仍可展示赛事信息，但不能冒充为可提醒的 DDL。
                deadline_text = "报名截止时间请以官方赛程为准"
            else:
                deadline_year, deadline_month, deadline_day = deadline.split("-")
                deadline_text = f"报名截止：{deadline_year}年{int(deadline_month)}月{int(deadline_day)}日"
            title = competition if competition.startswith("中国研究生") else f"中国研究生{competition}"
            source_url = f"{schedule_url}#competition-{position}"
            body = (
                f"{title}。参赛对象：研究生。报名时间：{registration or '以官方通知为准'}；"
                f"{deadline_text}；提交作品时间：{submission or '以官方通知为准'}；"
                f"决赛时间：{final or '以官方通知为准'}。"
            )
            self._bodies[source_url] = body
            links.append(NoticeLink(source_url, title, self.name, self.category, published_at))
        return links

    async def fetch(
        self,
        client: httpx.AsyncClient,
        link: NoticeLink,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> NoticePage:
        # discover 已抓取并解析该公开赛程页，避免为同一张表重复请求 19 次。
        body = self._bodies.get(link.url)
        if body is None:
            raise ValueError("研创网赛事条目不在本次发现结果中")
        return NoticePage(link, body, None, None)

    async def _schedule_url(self, client: httpx.AsyncClient) -> str:
        try:
            response = await client.get(self.listing_url)
            response.raise_for_status()
            parser = _AnchorParser()
            parser.feed(response.text)
            for href, title, text in parser.links:
                if "赛程" in f"{title}{text}" and "赛事" in f"{title}{text}":
                    # 研创网页面使用 <base> 与无斜杠的站点根相对 href；不能按列表页路径拼接。
                    origin = urlparse(self.listing_url)
                    return urljoin(f"{origin.scheme}://{origin.netloc}/", href.lstrip("/"))
        except httpx.HTTPError:
            # 赛程详情仍是公开页面，列表页短暂不可用时可用已知入口继续同步。
            pass
        return self._fallback_schedule_url

    @staticmethod
    def _schedule_year(html: str) -> int:
        match = re.search(r"(20\d{2})年度[^<]{0,50}赛程", html)
        return int(match.group(1)) if match else datetime.now().astimezone().year

    @staticmethod
    def _published_at(html: str) -> Optional[str]:
        match = re.search(r"发布时间\s*[：:]?\s*(20\d{2})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})", html)
        if not match:
            return None
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    @staticmethod
    def _schedule_rows(html: str) -> List[tuple[str, str, str, str]]:
        parser = _TableParser()
        parser.feed(html)
        for table in parser.tables:
            if not table or "主题赛事" not in " ".join(table[0]) or "报名时间" not in " ".join(table[0]):
                continue
            rows: List[tuple[str, str, str, str]] = []
            parent_competition = ""
            for cells in table[1:]:
                # 带 rowspan 的序号列会在下一行缺失；赛事、报名、提交、决赛固定为末四列。
                if len(cells) < 4:
                    continue
                competition, registration, submission, final = cells[-4:]
                competition = re.sub(r"\s+", "", competition)
                # 某些主题赛事以 rowspan 标出总名称，具体报名时间放在子赛道行。
                if not registration:
                    if any(word in competition for word in ("大赛", "竞赛", "挑战赛")):
                        parent_competition = competition
                    continue
                if parent_competition and len(cells) == 4:
                    competition = f"{parent_competition}·{competition}"
                if not any(word in competition for word in ("大赛", "竞赛", "挑战赛")):
                    continue
                rows.append((competition, registration, submission, final))
            return rows
        return []

    @staticmethod
    def _registration_deadline(registration: str, year: int) -> Optional[str]:
        dates = re.findall(r"(\d{1,2})月\s*(\d{1,2})日", registration)
        if not dates:
            return None
        # 多赛道说明会并列多个截止日；取报名文本中最晚日期，避免主赛道仍开放时过早停止推送。
        month, day = max((int(month), int(day)) for month, day in dates)
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None


def default_public_adapters() -> List[PublicSourceAdapter]:
    return [
        # 这些公开页面均提供服务端渲染的通知列表；不要从门户首页泛抓导航和专题链接。
        HtmlNoticeAdapter(
            name="西电新闻网",
            category="school",
            listing_url="https://news.xidian.edu.cn/",
            include_url_patterns=(r"/info/\d+/\d+\.htm$",),
            include_title_patterns=(r"通知|公告|公示|报名|申请|招聘|选课",),
        ),
        HtmlNoticeAdapter(
            name="本科生院",
            category="academic",
            listing_url="https://jwc.xidian.edu.cn/tzgg.htm",
            include_url_patterns=(r"/info/1012/\d+\.htm$",),
        ),
        HtmlNoticeAdapter(
            # 该站的分类页由前端异步加载，首页服务端已输出同一份通知公告列表。
            name="西电就业信息网",
            category="employment",
            listing_url="https://job.xidian.edu.cn/",
            include_url_patterns=(r"/news/view/aid/\d+/tag/tzgg$",),
        ),
        CPIPCCompetitionAdapter(),
    ]
