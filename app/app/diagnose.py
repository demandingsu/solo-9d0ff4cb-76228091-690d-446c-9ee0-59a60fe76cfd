"""采集质量诊断：找出改版后无法可靠抽取的详情页。

与生产解析器（parser.parse_detail）的关系：诊断器复用同一套字段键映射
（FIELD_KEYS）与表格选择器，模拟线上抽取规则；但它**不抛出异常**，而是把
每个问题记录为字段级异常（PageIssue），并给出字段所在的 HTML 结构签名。

结构签名只由标签名、id、class、data-field 构成，不包含任何文案，
也不包含引用条数，因此同一模板渲染出的页面无论文字如何都会得到
相同签名，便于把“成批模板变化”与“单页异常”区分开。
"""
from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from .models import (
    GroupClassification,
    IssueGroup,
    IssueKind,
    NoticeKind,
    PageIssue,
)
from .parser import FIELD_KEYS, KIND_MAP, _parse_time

#: 需要带时区 ISO 8601 时间的字段
TIME_FIELDS = ("published_at", "suspend_start", "suspend_end")

#: 所有页面都必须可抽取的字段
UNIVERSAL_FIELDS = ("notice_id", "published_at")

#: 停运公告额外要求的字段
SUSPENSION_FIELDS = ("route", "suspend_start", "suspend_end")


@dataclass
class _Candidate:
    """页面上某个 canonical 字段的一次出现。"""

    value: str
    row_path: str        # 字段行的结构路径
    container_path: str  # 字段所在容器的结构路径


def _node_path(el: Tag) -> str:
    """元素的结构路径，如 html>body>article>table.notice-fields。

    只含标签/id/class/data-field，不含文本内容。
    """
    parts: list[str] = []
    node = el
    while isinstance(node, Tag) and node.name != "[document]":
        desc = node.name
        nid = node.get("id")
        if nid:
            desc += f"#{nid}"
        classes = node.get("class") or []
        if classes:
            desc += "." + ".".join(sorted(str(c) for c in classes))
        data_field = node.get("data-field")
        if data_field:
            desc += f"[data-field={data_field}]"
        parts.append(desc)
        node = node.parent
    return ">".join(reversed(parts))


def _canonical_field(key: str) -> str | None:
    """把 data-field 值（英文键或中文标签）映射到 canonical 字段名。"""
    key = key.strip()
    for canon, aliases in FIELD_KEYS.items():
        if key == canon or key in aliases:
            return canon
    return None


def _collect_candidates(soup: BeautifulSoup) -> dict[str, list[_Candidate]]:
    """按 canonical 字段收集 table.notice-fields 内所有同名行。

    同一字段出现多行（重复 data-field 或别名键混用）时全部保留，
    供冲突检测使用。
    """
    out: dict[str, list[_Candidate]] = {}
    for tr in soup.select("table.notice-fields tr[data-field]"):
        canonical = _canonical_field(tr.get("data-field", ""))
        if canonical is None:
            continue
        cell = tr.select_one("td")
        if cell is None:
            continue
        time_el = cell.select_one("time[datetime]")
        value = time_el["datetime"] if time_el else cell.get_text(strip=True)
        table = tr.find_parent("table")
        out.setdefault(canonical, []).append(
            _Candidate(
                value=value,
                row_path=_node_path(tr),
                container_path=_node_path(table) if table else _node_path(tr),
            )
        )
    return out


def _missing_signature(soup: BeautifulSoup, field: str) -> str:
    """字段缺失时，描述它在页面上的实际（或应有）结构位置。"""
    aliases = {field, *FIELD_KEYS.get(field, ())}

    # 1) data-field 还在，但搬出了 table.notice-fields（模板改版）
    moved = soup.find(attrs={"data-field": lambda v: v and v.strip() in aliases})
    if isinstance(moved, Tag):
        return "moved:" + _node_path(moved)

    # 2) 没有 data-field，但标签文案还在（th/dt），字段随新结构迁移
    for lab in soup.find_all(["th", "dt"]):
        if lab.get_text(strip=True) in FIELD_KEYS.get(field, ()):
            row = lab.find_parent(["tr", "div", "li"]) or lab
            return "label:" + _node_path(row)

    # 3) 页面上完全没有该字段：记录字段表本身是否还在
    table = soup.select_one("table.notice-fields")
    if isinstance(table, Tag):
        return "absent-in:" + _node_path(table)
    return "absent:no-field-table"


def _conflict_signature(candidates: list[_Candidate]) -> str:
    containers = sorted({c.container_path for c in candidates})
    return "conflict-in:" + " | ".join(containers)


def _references_missing_signature(soup: BeautifulSoup) -> str:
    # 引用链接被搬到非常规结构（没有 class="reference"）
    moved = soup.select_one("a[data-ref-id]")
    if isinstance(moved, Tag):
        return "moved:" + _node_path(moved.parent if isinstance(moved.parent, Tag) else moved)
    section = soup.select_one("section.refs")
    if isinstance(section, Tag):
        return "absent-in:" + _node_path(section)
    return "absent:no-refs-section"


def _page_kind(
    candidates: dict[str, list[_Candidate]], kind_hint: str | None
) -> NoticeKind | None:
    """判定公告类型：详情页类型字段优先，其次列表页 data-kind 摘要。"""
    for cand in candidates.get("kind", []):
        kind = KIND_MAP.get(cand.value.strip())
        if kind is not None:
            return kind
    if kind_hint:
        return KIND_MAP.get(kind_hint.strip())
    return None


def diagnose_detail_page(
    html: str, url: str, kind_hint: str | None = None
) -> list[PageIssue]:
    """诊断单个详情页，返回字段级异常列表（无异常则空列表）。

    kind_hint 为列表页 data-kind 摘要，用于详情页类型字段缺失时
    判断应检查停运字段还是引用编号。
    """
    soup = BeautifulSoup(html, "html.parser")
    if soup.find() is None:
        raise ValueError("页面为空或不是有效 HTML")

    candidates = _collect_candidates(soup)
    kind = _page_kind(candidates, kind_hint)

    issues: list[PageIssue] = []

    def check_field(field: str) -> None:
        vals = [c for c in candidates.get(field, []) if c.value.strip()]
        distinct = sorted({c.value.strip() for c in vals})
        if not vals:
            issues.append(
                PageIssue(
                    url=url,
                    field=field,
                    issue=IssueKind.MISSING,
                    structure=_missing_signature(soup, field),
                    detail="字段缺失，当前抽取规则取不到值",
                )
            )
        elif len(distinct) > 1:
            issues.append(
                PageIssue(
                    url=url,
                    field=field,
                    issue=IssueKind.CONFLICT,
                    structure=_conflict_signature(vals),
                    detail="同页同名字段值冲突: " + " / ".join(distinct),
                )
            )
        elif field in TIME_FIELDS:
            try:
                _parse_time(distinct[0])
            except ValueError:
                issues.append(
                    PageIssue(
                        url=url,
                        field=field,
                        issue=IssueKind.INVALID,
                        structure="value:" + vals[0].row_path,
                        detail=f"时间无法解析或缺少时区: {distinct[0]!r}",
                    )
                )

    for field in UNIVERSAL_FIELDS:
        check_field(field)

    if kind is NoticeKind.SUSPENSION:
        for field in SUSPENSION_FIELDS:
            check_field(field)
    elif kind in (NoticeKind.CORRECTION, NoticeKind.CANCELLATION):
        references = [
            a["data-ref-id"].strip()
            for a in soup.select("a.reference[data-ref-id]")
            if a.get("data-ref-id", "").strip()
        ]
        if not references:
            issues.append(
                PageIssue(
                    url=url,
                    field="references",
                    issue=IssueKind.MISSING,
                    structure=_references_missing_signature(soup),
                    detail="更正/撤销公告缺少引用编号",
                )
            )

    return issues


def group_issues(
    issues: list[PageIssue], *, batch_threshold: int = 2, max_examples: int = 3
) -> list[IssueGroup]:
    """按 (字段, 异常形态, HTML 结构签名) 归组。

    组内异常页数达到 batch_threshold 视为成批模板变化，否则为单页异常。
    """
    buckets: dict[tuple[str, IssueKind, str], list[str]] = {}
    for issue in issues:
        key = (issue.field, issue.issue, issue.structure)
        buckets.setdefault(key, []).append(issue.url)

    groups: list[IssueGroup] = []
    for (field, issue_kind, structure), urls in buckets.items():
        unique_urls = list(dict.fromkeys(urls))  # 同一页面同组只计一次
        classification = (
            GroupClassification.TEMPLATE_CHANGE
            if len(unique_urls) >= batch_threshold
            else GroupClassification.SINGLE_PAGE
        )
        groups.append(
            IssueGroup(
                field=field,
                issue=issue_kind,
                structure=structure,
                classification=classification,
                page_count=len(unique_urls),
                example_urls=unique_urls[:max_examples],
            )
        )

    groups.sort(
        key=lambda g: (
            g.classification is not GroupClassification.TEMPLATE_CHANGE,
            g.field,
            g.issue.value,
            g.structure,
        )
    )
    return groups
