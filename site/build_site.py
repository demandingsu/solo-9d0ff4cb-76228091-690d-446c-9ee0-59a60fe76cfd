"""生成离线示例公告站（纯静态 HTML）。

主栏目场景覆盖（查询时刻 2026-11-11T10:00+08:00）：
  001 12 路原始停运，查询时有效
  002 18 路已过停运结束时间（过期）
  003 25 路原始停运，006 更正、010 再更正（更正引用更正，区间被两次替换）
  004 33 路停运，007 撤销（整条链失效）
  005 12 路另一条独立停运（同线路独立判断），012 更正发布于查询时刻之后（不可见）
  008 5 路停运区间尚未开始
  009 40 路停运，011 更正同时引用了不存在的 N-2026-999（无法匹配的引用）
  013 50 路整条公告发布于查询时刻之后（时间门控）
  另有一个跨站详情链接，验证同站限制。

改版后栏目（v2/，供 /diagnose 采集质量诊断演示）：
  101~103 新模板（dl.notice-meta）：字段整体搬出 table.notice-fields，
          旧规则取不到 → 同一结构的成批模板变化
  104     同页两个“公告编号”行且值不同 → 同名字段值冲突（单页异常）
  105     停运公告缺线路与停运结束时间（单页异常）
  106     发布时间缺少时区（单页异常）
  107     更正公告缺引用编号（单页异常）
  109     列表有链接但详情页不存在 → 抓取失败，整站状态为未核全
  110/111 正常停运 + 更正（对照组，不应产生任何异常）
"""
from __future__ import annotations

import html
import shutil
from dataclasses import dataclass, field
from pathlib import Path

OUT_DIR = Path(__file__).parent / "html"
PAGE_SIZE = 5
TZ = "+08:00"


@dataclass
class Notice:
    notice_id: str
    kind: str  # suspension | correction | cancellation
    title: str
    route: str | None
    published_at: str
    suspend_start: str | None = None
    suspend_end: str | None = None
    references: list[str] = field(default_factory=list)
    offsite: bool = False  # 列表项指向外站
    # --- 诊断演示用（仅 v2 栏目） ---
    template: str = "table"  # table=旧版字段表 / meta=改版后 dl.notice-meta
    conflict_fields: dict[str, str] = field(default_factory=dict)  # 字段 -> 冲突的第二个值
    naive_fields: tuple[str, ...] = ()  # 渲染为不带时区的时间字段
    broken: bool = False  # 列表有链接但不生成详情页（抓取 404）


NOTICES: list[Notice] = [
    Notice("N-2026-001", "suspension", "12 路临时停运公告", "12 路",
           "2026-11-08T09:00:00+08:00", "2026-11-10T06:00:00+08:00",
           "2026-11-12T22:00:00+08:00"),
    Notice("N-2026-002", "suspension", "18 路临时停运公告", "18 路",
           "2026-11-03T14:00:00+08:00", "2026-11-05T06:00:00+08:00",
           "2026-11-08T20:00:00+08:00"),
    Notice("N-2026-003", "suspension", "25 路临时停运公告", "25 路",
           "2026-11-07T16:30:00+08:00", "2026-11-09T06:00:00+08:00",
           "2026-11-12T20:00:00+08:00"),
    Notice("N-2026-004", "suspension", "33 路临时停运公告", "33 路",
           "2026-11-08T11:00:00+08:00", "2026-11-10T05:30:00+08:00",
           "2026-11-20T22:00:00+08:00"),
    Notice("N-2026-005", "suspension", "12 路临时停运公告（施工绕行）", "12 路",
           "2026-11-09T17:20:00+08:00", "2026-11-11T05:00:00+08:00",
           "2026-11-13T21:00:00+08:00"),
    Notice("N-2026-006", "correction", "25 路停运时间更正公告", "25 路",
           "2026-11-09T15:00:00+08:00", "2026-11-10T00:00:00+08:00",
           "2026-11-15T22:00:00+08:00", references=["N-2026-003"]),
    Notice("N-2026-007", "cancellation", "33 路停运撤销公告", "33 路",
           "2026-11-09T10:00:00+08:00", references=["N-2026-004"]),
    Notice("N-2026-008", "suspension", "5 路临时停运公告", "5 路",
           "2026-11-09T08:00:00+08:00", "2026-11-12T06:00:00+08:00",
           "2026-11-14T20:00:00+08:00"),
    Notice("N-2026-009", "suspension", "40 路临时停运公告", "40 路",
           "2026-11-10T09:30:00+08:00", "2026-11-11T06:00:00+08:00",
           "2026-11-13T20:00:00+08:00"),
    Notice("N-2026-010", "correction", "25 路停运时间第二次更正公告", "25 路",
           "2026-11-10T08:00:00+08:00", "2026-11-10T00:00:00+08:00",
           "2026-11-20T22:00:00+08:00", references=["N-2026-006"]),
    Notice("N-2026-011", "correction", "40 路停运更正公告（含一处错误引用）", "40 路",
           "2026-11-10T18:00:00+08:00", "2026-11-11T06:00:00+08:00",
           "2026-11-16T20:00:00+08:00",
           references=["N-2026-009", "N-2026-999"]),
    Notice("N-2026-012", "correction", "12 路施工绕行安排更正（次日发布）", "12 路",
           "2026-11-12T09:00:00+08:00", "2026-11-11T05:00:00+08:00",
           "2026-11-14T21:00:00+08:00", references=["N-2026-005"]),
    Notice("N-2026-013", "suspension", "50 路临时停运公告（提前发布）", "50 路",
           "2026-11-14T09:00:00+08:00", "2026-11-15T06:00:00+08:00",
           "2026-11-18T20:00:00+08:00"),
    Notice("N-EXT-001", "suspension", "外站转载公告（跨站链接示例）", "99 路",
           "2026-11-08T09:00:00+08:00", offsite=True),
]

# 改版后栏目（v2/）：运营方改版详情页后的新公告，覆盖诊断接口的各类场景。
V2_NOTICES: list[Notice] = [
    # 新模板：字段从 table.notice-fields 搬到 dl.notice-meta（成批模板变化）
    Notice("N-2026-101", "suspension", "7 路临时停运公告", "7 路",
           "2026-11-10T09:00:00+08:00", "2026-11-12T06:00:00+08:00",
           "2026-11-15T20:00:00+08:00", template="meta"),
    Notice("N-2026-102", "suspension", "9 路临时停运公告", "9 路",
           "2026-11-10T09:30:00+08:00", "2026-11-12T06:00:00+08:00",
           "2026-11-14T20:00:00+08:00", template="meta"),
    Notice("N-2026-103", "suspension", "21 路临时停运公告", "21 路",
           "2026-11-10T10:00:00+08:00", "2026-11-13T06:00:00+08:00",
           "2026-11-16T20:00:00+08:00", template="meta"),
    # 旧模板单页异常
    Notice("N-2026-104", "suspension", "8 路临时停运公告", "8 路",
           "2026-11-10T10:30:00+08:00", "2026-11-11T06:00:00+08:00",
           "2026-11-13T20:00:00+08:00",
           conflict_fields={"notice_id": "N-2026-104B"}),
    Notice("N-2026-105", "suspension", "16 路临时停运公告", None,
           "2026-11-10T11:00:00+08:00", "2026-11-11T06:00:00+08:00",
           None),
    Notice("N-2026-106", "suspension", "19 路临时停运公告", "19 路",
           "2026-11-10T08:30:00+08:00", "2026-11-11T06:00:00+08:00",
           "2026-11-12T20:00:00+08:00", naive_fields=("published_at",)),
    Notice("N-2026-107", "correction", "8 路停运时间更正公告", "8 路",
           "2026-11-10T12:00:00+08:00", "2026-11-11T08:00:00+08:00",
           "2026-11-14T20:00:00+08:00"),
    Notice("N-2026-109", "suspension", "23 路临时停运公告", "23 路",
           "2026-11-10T13:00:00+08:00", "2026-11-12T06:00:00+08:00",
           "2026-11-13T20:00:00+08:00", broken=True),
    # 对照组：结构完整，不应产生异常
    Notice("N-2026-110", "suspension", "27 路临时停运公告", "27 路",
           "2026-11-10T14:00:00+08:00", "2026-11-11T06:00:00+08:00",
           "2026-11-13T20:00:00+08:00"),
    Notice("N-2026-111", "correction", "27 路停运时间更正公告", "27 路",
           "2026-11-10T16:00:00+08:00", "2026-11-11T08:00:00+08:00",
           "2026-11-14T20:00:00+08:00", references=["N-2026-110"]),
]

KIND_CN = {"suspension": "停运", "correction": "更正", "cancellation": "撤销"}


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def detail_path(n: Notice) -> str:
    return f"notices/{n.notice_id}.html"


def _refs_html(n: Notice) -> str:
    if not n.references:
        return ""
    links = "、".join(
        f'<a class="reference" data-ref-id="{_esc(r)}" href="{_esc(r)}.html">'
        f"{_esc(r)}</a>"
        for r in n.references
    )
    return f"\n    <section class=refs><h2>引用公告</h2><p>{links}</p></section>"


def render_detail_table(n: Notice) -> str:
    """旧版模板：table.notice-fields + data-field 行。"""
    rows: list[str] = []

    def row(field_name: str, label: str, value: str | None, is_time: bool = False) -> None:
        if value is None:
            return
        if is_time:
            shown = value.replace(TZ, "") if field_name in n.naive_fields else value
            cell = f'<time datetime="{_esc(shown)}">{_esc(shown.replace("T", " "))}</time>'
        else:
            cell = _esc(value)
        rows.append(
            f'      <tr data-field="{field_name}"><th>{label}</th><td>{cell}</td></tr>'
        )
        # 同名字段第二行（值冲突场景）
        conflict = n.conflict_fields.get(field_name)
        if conflict is not None:
            rows.append(
                f'      <tr data-field="{field_name}"><th>{label}</th>'
                f"<td>{_esc(conflict)}</td></tr>"
            )

    row("notice_id", "公告编号", n.notice_id)
    row("kind", "类型", KIND_CN[n.kind])
    row("route", "线路", n.route)
    row("suspend_start", "停运开始时间", n.suspend_start, is_time=True)
    row("suspend_end", "停运结束时间", n.suspend_end, is_time=True)
    row("published_at", "发布时间", n.published_at, is_time=True)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{_esc(n.title)}</title></head>
<body>
  <article>
    <h1 class="notice-title">{_esc(n.title)}</h1>
    <table class="notice-fields">
{chr(10).join(rows)}
    </table>{_refs_html(n)}
    <p><a href="../index.html">返回公告列表</a></p>
  </article>
</body>
</html>
"""


def render_detail_meta(n: Notice) -> str:
    """改版后新模板：字段搬进 dl.notice-meta，无 data-field 行。"""
    items: list[str] = []

    def item(label: str, value: str | None, is_time: bool = False) -> None:
        if value is None:
            return
        if is_time:
            cell = f'<time datetime="{_esc(value)}">{_esc(value.replace("T", " "))}</time>'
        else:
            cell = _esc(value)
        items.append(f"      <div><dt>{label}</dt><dd>{cell}</dd></div>")

    item("公告编号", n.notice_id)
    item("类型", KIND_CN[n.kind])
    item("线路", n.route)
    item("停运开始时间", n.suspend_start, is_time=True)
    item("停运结束时间", n.suspend_end, is_time=True)
    item("发布时间", n.published_at, is_time=True)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{_esc(n.title)}</title></head>
<body>
  <article>
    <h1 class="notice-title">{_esc(n.title)}</h1>
    <dl class="notice-meta">
{chr(10).join(items)}
    </dl>{_refs_html(n)}
    <p><a href="../index.html">返回公告列表</a></p>
  </article>
</body>
</html>
"""


def render_detail(n: Notice) -> str:
    if n.template == "meta":
        return render_detail_meta(n)
    return render_detail_table(n)


def render_list(
    page_notices: list[Notice],
    page_idx: int,
    total_pages: int,
    *,
    heading: str = "线路公告列表",
    footer: str = "",
) -> str:
    items: list[str] = []
    for n in page_notices:
        if n.offsite:
            href = "http://external.example.org/notice/N-EXT-001.html"
        else:
            href = detail_path(n)
        items.append(
            f'    <article class="notice" data-id="{_esc(n.notice_id)}" '
            f'data-kind="{n.kind}">\n'
            f'      <a class="detail-link" href="{_esc(href)}">{_esc(n.title)}</a>\n'
            f'      <span class="published">{_esc(n.published_at)}</span>\n'
            f"    </article>"
        )

    pager_parts: list[str] = []
    if page_idx > 0:
        prev_file = "index.html" if page_idx == 1 else f"page{page_idx}.html"
        pager_parts.append(f'<a rel="prev" href="{prev_file}">上一页</a>')
    if page_idx < total_pages - 1:
        next_file = "page2.html" if page_idx == 0 else f"page{page_idx + 2}.html"
        pager_parts.append(f'<a rel="next" href="{next_file}">下一页</a>')
    pager = f'  <nav class="pager">{" ".join(pager_parts)}</nav>' if pager_parts else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{_esc(heading)}（第 {page_idx + 1} 页）</title></head>
<body>
  <h1>{_esc(heading)}</h1>
{chr(10).join(items)}
{pager}
{footer}
</body>
</html>
"""


def _write_section(base: Path, notices: list[Notice], heading: str, footer: str = "") -> int:
    """写一个栏目：详情页（broken 除外）+ 分页列表，返回列表页数。"""
    (base / "notices").mkdir(parents=True, exist_ok=True)
    real = [n for n in notices if not n.offsite and not n.broken]
    for n in real:
        (base / detail_path(n)).write_text(render_detail(n), encoding="utf-8")

    pages: list[list[Notice]] = [
        notices[i : i + PAGE_SIZE] for i in range(0, len(notices), PAGE_SIZE)
    ]
    for idx, page_notices in enumerate(pages):
        name = "index.html" if idx == 0 else f"page{idx + 1}.html"
        (base / name).write_text(
            render_list(page_notices, idx, len(pages), heading=heading, footer=footer),
            encoding="utf-8",
        )
    return len(pages)


def build() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    main_pages = _write_section(
        OUT_DIR,
        NOTICES,
        "线路公告列表",
        footer='  <p><a href="v2/index.html">运营方改版后公告栏目（采集质量诊断示例）</a></p>',
    )
    v2_pages = _write_section(OUT_DIR / "v2", V2_NOTICES, "线路公告列表（改版后栏目）")

    real_main = len([n for n in NOTICES if not n.offsite])
    real_v2 = len([n for n in V2_NOTICES if not n.broken])
    print(
        f"示例站点已生成: {OUT_DIR}"
        f"（主栏目 {real_main} 条公告 / {main_pages} 个列表页，"
        f"改版后栏目 {real_v2} 条公告 / {v2_pages} 个列表页）"
    )


if __name__ == "__main__":
    build()
