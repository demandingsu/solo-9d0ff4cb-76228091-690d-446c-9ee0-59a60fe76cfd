"""采集质量诊断：字段级异常检测、结构归组与 /diagnose 端到端。

集成测试前置：示例站点已在 127.0.0.1:8080 提供服务（见 README / demo 脚本）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.diagnose import diagnose_detail_page, group_issues
from app.main import app
from app.models import PageIssue, IssueKind

HTML_DIR = Path(__file__).parent.parent / "site" / "html"
SITE = os.environ.get("TEST_SITE_URL", "http://127.0.0.1:8080/index.html")
V2_SITE = os.environ.get("TEST_V2_SITE_URL", "http://127.0.0.1:8080/v2/index.html")

pytestmark = pytest.mark.asyncio


def _v2(notice_id: str) -> str:
    return (HTML_DIR / "v2" / "notices" / f"{notice_id}.html").read_text(encoding="utf-8")


def _url(notice_id: str) -> str:
    return f"http://site:8080/v2/notices/{notice_id}.html"


# ---------------------------------------------------------------------------
# 单页字段诊断
# ---------------------------------------------------------------------------


async def test_meta_template_all_fields_missing():
    """新模板（dl.notice-meta）：旧规则取不到任何字段，结构签名指向新容器。"""
    issues = diagnose_detail_page(_v2("N-2026-101"), _url("N-2026-101"), "suspension")
    by_field = {i.field: i for i in issues}
    assert set(by_field) == {
        "notice_id",
        "published_at",
        "route",
        "suspend_start",
        "suspend_end",
    }
    assert all(i.issue is IssueKind.MISSING for i in issues)
    assert all("dl.notice-meta" in i.structure for i in issues)
    assert all(i.url == _url("N-2026-101") for i in issues)


async def test_meta_template_without_kind_hint_checks_universal_only():
    """列表页无 data-kind 摘要时，无法判定类型，只检查通用字段。"""
    issues = diagnose_detail_page(_v2("N-2026-101"), _url("N-2026-101"))
    assert {i.field for i in issues} == {"notice_id", "published_at"}


async def test_conflicting_notice_id_detected():
    issues = diagnose_detail_page(_v2("N-2026-104"), _url("N-2026-104"), "suspension")
    assert len(issues) == 1
    issue = issues[0]
    assert issue.field == "notice_id"
    assert issue.issue is IssueKind.CONFLICT
    assert "N-2026-104" in issue.detail and "N-2026-104B" in issue.detail
    assert "table.notice-fields" in issue.structure


async def test_missing_route_and_suspend_end():
    issues = diagnose_detail_page(_v2("N-2026-105"), _url("N-2026-105"), "suspension")
    assert {(i.field, i.issue) for i in issues} == {
        ("route", IssueKind.MISSING),
        ("suspend_end", IssueKind.MISSING),
    }


async def test_naive_published_time_is_invalid():
    issues = diagnose_detail_page(_v2("N-2026-106"), _url("N-2026-106"), "suspension")
    assert len(issues) == 1
    assert issues[0].field == "published_at"
    assert issues[0].issue is IssueKind.INVALID
    assert "时区" in issues[0].detail


async def test_correction_without_references():
    issues = diagnose_detail_page(_v2("N-2026-107"), _url("N-2026-107"), "correction")
    assert len(issues) == 1
    assert issues[0].field == "references"
    assert issues[0].issue is IssueKind.MISSING


async def test_healthy_pages_have_no_issues():
    for nid, hint in [("N-2026-110", "suspension"), ("N-2026-111", "correction")]:
        assert diagnose_detail_page(_v2(nid), _url(nid), hint) == []


async def test_main_site_pages_have_no_issues():
    """主栏目全部为旧模板健康页，诊断应为空。"""
    for f in sorted((HTML_DIR / "notices").glob("*.html")):
        assert diagnose_detail_page(f.read_text(encoding="utf-8"), f"http://site/{f.name}") == []


async def test_empty_html_raises_parse_failure():
    with pytest.raises(ValueError):
        diagnose_detail_page("", "http://site/x.html")


# ---------------------------------------------------------------------------
# 结构签名与归组
# ---------------------------------------------------------------------------


def _meta_page(dt_label: str, route_text: str) -> str:
    """同一新模板结构、不同文案的详情页。"""
    return f"""<html><body><article><dl class="notice-meta">
      <div><dt>公告编号</dt><dd>N-X</dd></div>
      <div><dt>{dt_label}</dt><dd>{route_text}</dd></div>
    </dl></article></body></html>"""


async def test_structure_signature_ignores_wording():
    """结构相同、文案不同的页面得到相同签名，归入同组。"""
    a = diagnose_detail_page(_meta_page("线路", "7 路"), "http://site/a.html", "suspension")
    b = diagnose_detail_page(_meta_page("线路名称", "9 路"), "http://site/b.html", "suspension")
    sig_a = {i.field: i.structure for i in a}
    sig_b = {i.field: i.structure for i in b}
    assert sig_a == sig_b
    assert "7 路" not in sig_a["route"] and "线路" not in sig_a["route"]

    groups = group_issues(a + b)
    route_group = next(g for g in groups if g.field == "route")
    assert route_group.page_count == 2
    assert route_group.classification.value == "template_change"


async def test_reference_count_ignored():
    """更正公告引用条数多少不影响判定：有引用即正常，且不进入签名。"""
    one_ref = """<html><body><article><table class="notice-fields">
      <tr data-field="notice_id"><th>公告编号</th><td>N-1</td></tr>
      <tr data-field="kind"><th>类型</th><td>更正</td></tr>
      <tr data-field="published_at"><th>发布时间</th><td><time datetime="2026-11-10T09:00:00+08:00">t</time></td></tr>
    </table><section class="refs"><p>
      <a class="reference" data-ref-id="N-0" href="N-0.html">N-0</a>
    </p></section></article></body></html>"""
    three_refs = one_ref.replace(
        '<a class="reference" data-ref-id="N-0" href="N-0.html">N-0</a>',
        '<a class="reference" data-ref-id="N-0" href="N-0.html">N-0</a>'
        '<a class="reference" data-ref-id="N-2" href="N-2.html">N-2</a>'
        '<a class="reference" data-ref-id="N-3" href="N-3.html">N-3</a>',
    )
    assert diagnose_detail_page(one_ref, "http://site/1.html", "correction") == []
    assert diagnose_detail_page(three_refs, "http://site/3.html", "correction") == []


async def test_group_classification_and_examples():
    def issue(url: str) -> PageIssue:
        return PageIssue(
            url=url, field="route", issue=IssueKind.MISSING,
            structure="absent-in:html>body>table", detail="x",
        )

    issues = [issue(f"http://site/{i}.html") for i in range(4)]
    issues.append(issue("http://site/0.html"))  # 同页重复只计一次
    groups = group_issues(issues, batch_threshold=2, max_examples=2)
    assert len(groups) == 1
    g = groups[0]
    assert g.page_count == 4
    assert g.classification.value == "template_change"
    assert len(g.example_urls) == 2  # max_examples 截断

    single = group_issues([issue("http://site/only.html")])
    assert single[0].classification.value == "single_page"
    assert single[0].page_count == 1


# ---------------------------------------------------------------------------
# /diagnose 端到端
# ---------------------------------------------------------------------------


async def _post(url: str, payload: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            return await ac.post(url, json=payload)


async def test_diagnose_v2_site_end_to_end():
    r = await _post("/diagnose", {"index_url": V2_SITE})
    assert r.status_code == 200, r.text
    data = r.json()

    # N-2026-109 详情页 404：保留已完成诊断，整站标为未核全
    assert data["status"] == "incomplete"
    assert len(data["failures"]) == 1
    assert data["failures"][0]["url"].endswith("/v2/notices/N-2026-109.html")
    assert data["failures"][0]["stage"] == "detail_page"

    assert len(data["list_pages_crawled"]) == 2
    assert data["detail_pages_crawled"] == 9
    assert data["pages_checked"] == 9
    assert data["pages_with_issues"] == 7
    assert data["issue_count"] == 20

    # 成批模板变化：新模板 3 页 × 5 个字段；单页异常 5 组
    assert data["template_change_groups"] == 5
    assert data["single_page_groups"] == 5

    batch = [g for g in data["groups"] if g["classification"] == "template_change"]
    assert {g["field"] for g in batch} == {
        "notice_id", "published_at", "route", "suspend_start", "suspend_end",
    }
    assert all(g["page_count"] == 3 for g in batch)
    assert all(len(g["example_urls"]) == 3 for g in batch)
    assert all("dl.notice-meta" in g["structure"] for g in batch)

    single = [g for g in data["groups"] if g["classification"] == "single_page"]
    assert all(g["page_count"] == 1 for g in single)
    by_field = {(g["field"], g["issue"]) for g in single}
    assert ("notice_id", "conflict") in by_field
    assert ("published_at", "invalid") in by_field
    assert ("references", "missing") in by_field
    assert ("route", "missing") in by_field
    assert ("suspend_end", "missing") in by_field

    # 逐页明细：字段 + 页面地址；健康页不出现
    issue_urls = {i["url"] for i in data["page_issues"]}
    assert not any(u.endswith("N-2026-110.html") for u in issue_urls)
    assert not any(u.endswith("N-2026-111.html") for u in issue_urls)
    conflict = next(i for i in data["page_issues"] if i["issue"] == "conflict")
    assert conflict["field"] == "notice_id"
    assert conflict["url"].endswith("N-2026-104.html")


async def test_diagnose_main_site_is_complete():
    r = await _post("/diagnose", {"index_url": SITE})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "complete"
    assert data["failures"] == []
    assert data["issue_count"] == 0
    assert data["groups"] == []
    assert data["pages_checked"] == 13
    assert len(data["skipped_offsite_links"]) == 1


async def test_diagnose_bad_index_url_returns_502():
    r = await _post("/diagnose", {"index_url": "http://127.0.0.1:9/never.html"})
    assert r.status_code == 502


async def test_diagnose_rejects_non_http_url():
    r = await _post("/diagnose", {"index_url": "ftp://x/index.html"})
    assert r.status_code == 422
