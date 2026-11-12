"""FastAPI 入口：POST /query 采集并判定临时停运；POST /diagnose 采集质量诊断。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

from .crawler import Crawler
from .diagnose import diagnose_detail_page, group_issues
from .engine import reconstruct
from .models import (
    CrawlFailure,
    DiagnoseRequest,
    DiagnoseResponse,
    GroupClassification,
    PageIssue,
    QueryRequest,
    QueryResponse,
    SiteStatus,
)

logger = logging.getLogger("bus_notice_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": "bus-suspension-api/1.0"},
    ) as client:
        app.state.http_client = client
        yield


app = FastAPI(
    title="公交临时停运公告采集 API",
    version="1.1.0",
    description=(
        "从运营方分页发布的线路公告中，还原更正/撤销公告链，"
        "返回查询时刻仍有效的临时停运区间；并提供采集质量诊断接口，"
        "找出改版后无法可靠抽取的详情页。"
    ),
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest) -> QueryResponse:
    crawler = Crawler(app.state.http_client)
    crawl = await crawler.crawl(req.index_url)

    result = reconstruct(crawl.announcements, req.query_time)

    if crawl.errors:
        logger.warning("抓取过程出现 %d 个错误: %s", len(crawl.errors), crawl.errors[:3])

    if not crawl.list_pages:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "公告首页无法抓取或无任何分页内容",
                "errors": crawl.errors,
            },
        )

    return QueryResponse(
        index_url=req.index_url,
        query_time=req.query_time,
        list_pages_crawled=crawl.list_pages,
        detail_pages_crawled=crawl.detail_pages,
        announcements_parsed=len(crawl.announcements),
        active_suspensions=result.active,
        inactive_chains=result.inactive,
        unmatched_references=result.unmatched,
        skipped_offsite_links=crawl.skipped_offsite,
    )


@app.post("/diagnose", response_model=DiagnoseResponse)
async def diagnose(req: DiagnoseRequest) -> DiagnoseResponse:
    """采集质量诊断：沿同站分页抓取详情页，逐页检查关键字段是否可可靠抽取。

    检查项：公告编号、带时区的发布时间、停运公告的线路与起止时间、
    更正/撤销公告的引用编号。字段缺失或同页同名字段值冲突时记录异常，
    按字段所在的 HTML 结构归组（忽略文案与引用条数），区分成批模板变化
    与单页异常。抓取/解析失败的页面保留在 failures 中，整站状态标为
    incomplete（未核全），不得报为正常。
    """
    crawler = Crawler(app.state.http_client)
    raw = await crawler.crawl_raw(req.index_url)

    failures = [
        CrawlFailure(url=f.url, stage=f.stage, error=f.error) for f in raw.failures
    ]

    if not raw.list_pages:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "公告首页无法抓取或无任何分页内容",
                "failures": [f.model_dump() for f in failures],
            },
        )

    page_issues: list[PageIssue] = []
    pages_checked = 0
    for page in raw.pages:
        try:
            found = diagnose_detail_page(page.html, page.url, page.kind_hint)
        except Exception as exc:  # 解析失败：保留已完成诊断，记录失败地址
            logger.warning("诊断解析失败 %s: %s", page.url, exc)
            failures.append(
                CrawlFailure(url=page.url, stage="parse", error=f"{type(exc).__name__}: {exc}")
            )
            continue
        pages_checked += 1
        page_issues.extend(found)

    groups = group_issues(
        page_issues,
        batch_threshold=req.batch_threshold,
        max_examples=req.max_examples,
    )

    if failures:
        logger.warning("诊断未核全：%d 个页面抓取/解析失败", len(failures))

    return DiagnoseResponse(
        index_url=req.index_url,
        status=SiteStatus.INCOMPLETE if failures else SiteStatus.COMPLETE,
        list_pages_crawled=raw.list_pages,
        detail_pages_crawled=len(raw.pages),
        pages_checked=pages_checked,
        pages_with_issues=len({i.url for i in page_issues}),
        issue_count=len(page_issues),
        template_change_groups=sum(
            1 for g in groups if g.classification is GroupClassification.TEMPLATE_CHANGE
        ),
        single_page_groups=sum(
            1 for g in groups if g.classification is GroupClassification.SINGLE_PAGE
        ),
        groups=groups,
        page_issues=page_issues,
        failures=failures,
        skipped_offsite_links=raw.skipped_offsite,
    )
