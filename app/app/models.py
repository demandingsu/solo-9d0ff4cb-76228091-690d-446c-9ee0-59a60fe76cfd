"""领域数据模型。

所有时间在系统内部统一为时区感知的 ``datetime``（UTC 或带原始偏移量均可，
比较时 Python 会自动换算）。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class NoticeKind(str, Enum):
    """公告类型。"""

    SUSPENSION = "suspension"          # 原始停运公告
    CORRECTION = "correction"          # 更正公告
    CANCELLATION = "cancellation"      # 撤销公告


class Announcement(BaseModel):
    """从详情页解析出的单条公告。"""

    notice_id: str = Field(..., description="公告编号，如 2026-001")
    route: str | None = Field(None, description="线路，如 12 路；更正/撤销页可能缺失")
    kind: NoticeKind
    suspend_start: datetime | None = None
    suspend_end: datetime | None = None
    published_at: datetime
    references: list[str] = Field(default_factory=list, description="更正/撤销引用的公告编号")
    url: str
    title: str | None = None

    @field_validator("published_at")
    @classmethod
    def _published_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("published_at 必须带时区信息")
        return v


class QueryRequest(BaseModel):
    """查询入参。"""

    index_url: str = Field(..., description="公告列表首页 URL（同站分页的起点）")
    query_time: datetime = Field(..., description="带时区的查询时刻，ISO 8601")

    @field_validator("index_url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("index_url 必须是 http/https URL")
        return v

    @field_validator("query_time")
    @classmethod
    def _query_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("query_time 必须带时区，例如 2026-11-11T10:00:00+08:00")
        return v


class ActiveSuspension(BaseModel):
    """查询时刻仍然有效的停运区间。"""

    route: str
    suspend_start: datetime
    suspend_end: datetime
    source_chain: list[str] = Field(..., description="形成该结论的公告链编号，根公告在前")
    sources: list[str] = Field(..., description="链上各公告详情页链接")
    published_at: datetime = Field(..., description="链上最新生效公告的发布时间")


class InactiveChain(BaseModel):
    """未产生有效停运的公告链，用于解释判定过程。"""

    root_id: str
    route: str | None
    reason: str = Field(..., description="cancelled / not_yet_effective / expired / no_interval")
    chain: list[str]
    sources: list[str]
    latest_published_at: datetime | None = None


class UnmatchedReference(BaseModel):
    """指向不存在公告的引用。"""

    notice_id: str = Field(..., description="发出引用的公告编号")
    missing_reference: str
    url: str
    kind: NoticeKind


class QueryResponse(BaseModel):
    """查询结果。"""

    index_url: str
    query_time: datetime
    list_pages_crawled: list[str]
    detail_pages_crawled: int
    announcements_parsed: int
    active_suspensions: list[ActiveSuspension]
    inactive_chains: list[InactiveChain]
    unmatched_references: list[UnmatchedReference]
    skipped_offsite_links: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 采集质量诊断（/diagnose）
# ---------------------------------------------------------------------------


class IssueKind(str, Enum):
    """单页字段异常形态。"""

    MISSING = "missing"      # 字段缺失（含带时区时间等效缺失）
    CONFLICT = "conflict"    # 同页同名字段出现多个不同的值
    INVALID = "invalid"      # 字段存在但无法可靠解析（如时间缺时区）


class GroupClassification(str, Enum):
    """异常组归类：成批模板变化 / 单页异常。"""

    TEMPLATE_CHANGE = "template_change"
    SINGLE_PAGE = "single_page"


class SiteStatus(str, Enum):
    """整站核查状态。"""

    COMPLETE = "complete"        # 全部页面均已抓取并完成诊断（核全）
    INCOMPLETE = "incomplete"    # 存在抓取/解析失败，仅覆盖部分页面（未核全）


class DiagnoseRequest(BaseModel):
    """诊断入参。"""

    index_url: str = Field(..., description="公告列表首页 URL（同站分页的起点）")
    batch_threshold: int = Field(
        2, ge=1, le=100,
        description="同一结构组内异常页数达到该值即视为成批模板变化，否则为单页异常",
    )
    max_examples: int = Field(3, ge=1, le=20, description="每组返回的示例链接上限")

    @field_validator("index_url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("index_url 必须是 http/https URL")
        return v


class PageIssue(BaseModel):
    """单个详情页上的字段级异常。"""

    url: str = Field(..., description="异常详情页地址")
    field: str = Field(
        ...,
        description="字段名：notice_id/published_at/route/suspend_start/suspend_end/references",
    )
    issue: IssueKind
    structure: str = Field(
        ..., description="字段所在的 HTML 结构签名（只含标签/类名/data 属性，忽略文案与引用条数）"
    )
    detail: str = Field(..., description="异常说明，如冲突的具体值")


class IssueGroup(BaseModel):
    """按字段 + 异常形态 + HTML 结构归组后的异常组。"""

    field: str
    issue: IssueKind
    structure: str
    classification: GroupClassification
    page_count: int = Field(..., description="组内异常页数量")
    example_urls: list[str] = Field(..., description="组内示例链接（最多 max_examples 条）")


class CrawlFailure(BaseModel):
    """抓取或解析失败的页面。"""

    url: str
    stage: str = Field(..., description="list_page / detail_page / parse")
    error: str


class DiagnoseResponse(BaseModel):
    """采集质量诊断结果。"""

    index_url: str
    status: SiteStatus = Field(
        ...,
        description="complete=全部页面核全；incomplete=存在抓取/解析失败，未核全（不得视为正常）",
    )
    list_pages_crawled: list[str]
    detail_pages_crawled: int
    pages_checked: int = Field(..., description="成功完成诊断的详情页数")
    pages_with_issues: int
    issue_count: int
    template_change_groups: int = Field(..., description="判定为成批模板变化的异常组数")
    single_page_groups: int = Field(..., description="判定为单页异常的异常组数")
    groups: list[IssueGroup]
    page_issues: list[PageIssue] = Field(..., description="逐页字段异常明细（字段 + 页面地址）")
    failures: list[CrawlFailure] = Field(
        default_factory=list, description="抓取/解析失败的页面地址；非空时 status 必为 incomplete"
    )
    skipped_offsite_links: list[str] = Field(default_factory=list)
