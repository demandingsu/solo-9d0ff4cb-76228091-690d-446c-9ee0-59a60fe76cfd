"""同站分页 + 详情抓取。

约束：
* 只沿同站链接爬行（协议/主机/端口一致），跨站分页或详情链接跳过并记录；
* 分页跟随 ``rel=next``，带最大页数与死循环保护；
* 详情 URL 去重，抓取失败不中断整体查询，作为错误收集返回。

``crawl`` 面向 /query：抓回详情页即解析为公告实体；
``crawl_raw`` 面向 /diagnose：保留详情页原始 HTML 与列表页 data-kind 摘要，
供采集质量诊断逐页检查。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .models import Announcement
from .parser import is_same_site, parse_detail, parse_detail_links, parse_next_page


@dataclass
class CrawlResult:
    announcements: list[Announcement] = field(default_factory=list)
    list_pages: list[str] = field(default_factory=list)
    detail_pages: int = 0
    skipped_offsite: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RawDetailPage:
    """抓取成功、尚未解析的详情页。"""

    url: str
    html: str
    kind_hint: str | None = None  # 列表页 data-kind 摘要


@dataclass
class RawFailure:
    """抓取失败的页面（列表页或详情页）。"""

    url: str
    stage: str  # "list_page" | "detail_page"
    error: str


@dataclass
class RawCrawlResult:
    pages: list[RawDetailPage] = field(default_factory=list)
    list_pages: list[str] = field(default_factory=list)
    skipped_offsite: list[str] = field(default_factory=list)
    failures: list[RawFailure] = field(default_factory=list)


class Crawler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_list_pages: int = 50,
        max_detail_pages: int = 500,
    ) -> None:
        self._client = client
        self._max_list_pages = max_list_pages
        self._max_detail_pages = max_detail_pages

    async def _get(self, url: str) -> str | None:
        try:
            resp = await self._client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return f"__ERROR__:{type(exc).__name__}:{exc}"
        return resp.text

    async def crawl_raw(self, index_url: str) -> RawCrawlResult:
        """抓列表分页与详情页原始 HTML，失败记入 failures 不中断。"""
        result = RawCrawlResult()
        seen_pages: set[str] = set()
        detail_urls: list[tuple[str, str | None]] = []
        seen_details: set[str] = set()

        # 1) 抓列表分页（只走同站 rel=next）
        page_url: str | None = index_url
        while page_url and page_url not in seen_pages and len(seen_pages) < self._max_list_pages:
            seen_pages.add(page_url)
            html = await self._get(page_url)
            if html is None or html.startswith("__ERROR__:"):
                result.failures.append(RawFailure(url=page_url, stage="list_page", error=str(html)))
                break
            result.list_pages.append(page_url)

            for detail_url, _id, kind_hint in parse_detail_links(html, page_url):
                if not is_same_site(detail_url, index_url):
                    result.skipped_offsite.append(detail_url)
                    continue
                if detail_url not in seen_details:
                    seen_details.add(detail_url)
                    detail_urls.append((detail_url, kind_hint))

            nxt = parse_next_page(html, page_url)
            if nxt and not is_same_site(nxt, index_url):
                result.skipped_offsite.append(nxt)
                nxt = None
            page_url = nxt

        # 2) 抓详情（同站已保证），限量保护
        for detail_url, kind_hint in detail_urls[: self._max_detail_pages]:
            html = await self._get(detail_url)
            if html is None or html.startswith("__ERROR__:"):
                result.failures.append(
                    RawFailure(url=detail_url, stage="detail_page", error=str(html))
                )
                continue
            result.pages.append(RawDetailPage(url=detail_url, html=html, kind_hint=kind_hint))

        return result

    async def crawl(self, index_url: str) -> CrawlResult:
        result = CrawlResult()
        raw = await self.crawl_raw(index_url)
        result.list_pages = raw.list_pages
        result.skipped_offsite = raw.skipped_offsite
        for failure in raw.failures:
            label = "列表页" if failure.stage == "list_page" else "详情页"
            result.errors.append(f"{label}抓取失败 {failure.url}: {failure.error}")

        for page in raw.pages:
            result.detail_pages += 1
            try:
                result.announcements.append(parse_detail(page.html, page.url))
            except ValueError as exc:
                result.errors.append(f"详情页解析失败 {page.url}: {exc}")

        return result
