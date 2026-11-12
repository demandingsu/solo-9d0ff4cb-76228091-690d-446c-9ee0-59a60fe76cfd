# 公交临时停运公告采集 API

调度员出车前，需要从运营方**分页发布**的线路公告中，查出“查询时刻仍有效”的临时停运。
本服务用 FastAPI + BeautifulSoup 抓取同站分页与详情页，按公告编号引用关系与发布时间
还原**更正/撤销公告链**，返回有效停运区间、来源链接及无法匹配的引用。

运营方改版详情页后，线路资料员可用 **POST /diagnose** 做采集质量诊断：沿同站分页抓取
详情页，逐页检查关键字段能否被当前规则可靠抽取，按字段所在的 HTML 结构归组异常，
区分**成批模板变化**与**单页异常**，输出各组异常页数量与示例链接，据此修复采集规则。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 公告按编号唯一标识 | `Announcement.notice_id` |
| 更正可引用另一条更正 | 引用图上从根（无有效出边引用的公告）沿后继遍历，支持任意深度 |
| 链顺序 | 引用关系定链，同一节点多个后继按发布时间排序 |
| 更正替换原停运信息 | 按发布时间依次应用，链上最后一条可见公告的起止/线路为当前生效信息 |
| 撤销使整条链失效 | 链上出现查询时刻已发布的撤销公告 → `inactive_chains[].reason=cancelled` |
| 未来公告不可见 | 发布时间晚于查询时刻的公告不参与链还原与判定 |
| 同线路独立公告分别判断 | 每条根公告独立成链；示例中 12 路有两条同时有效的独立停运 |
| 无法匹配的引用 | 被引用编号在整站不存在时收集到 `unmatched_references`，不影响其余判定 |
| 同站限制 | 只沿协议+主机+端口一致的链接爬行，跨站链接跳过并在 `skipped_offsite_links` 返回 |

停运区间按半开 `[suspend_start, suspend_end)` 判定：查询时刻等于结束时刻即视为已失效。
所有时间均要求带时区（ISO 8601 偏移），比较基于同一时间线瞬间。

## 采集质量诊断（POST /diagnose）

入参 `{"index_url": "...", "batch_threshold": 2, "max_examples": 3}`（后两者可选）。
沿同站分页抓取详情页，逐页检查：

| 检查项 | 适用页面 |
| --- | --- |
| 公告编号 `notice_id` | 全部 |
| 带时区的发布时间 `published_at` | 全部（无时区/无法解析记为 `invalid`） |
| 线路 `route`、停运起止 `suspend_start`/`suspend_end` | 停运公告 |
| 引用编号 `references` | 更正/撤销公告（至少一条） |

公告类型优先取详情页类型字段，取不到时回退列表页 `data-kind` 摘要。

**异常形态**：`missing`（字段缺失）、`conflict`（同页同名字段值冲突）、
`invalid`（存在但无法可靠解析，如时间缺时区）。每个异常都在 `page_issues[]`
中列出**字段和页面地址**。

**结构归组**：异常按 (字段, 异常形态, HTML 结构签名) 归组到 `groups[]`。
签名只含标签/id/class/data-field（如 `label:html>body>article>dl.notice-meta>div`），
**忽略文案与引用条数**——同一模板渲染的页面无论文字如何都落入同组。
组内异常页数 ≥ `batch_threshold` 判为 `template_change`（成批模板变化，
修一次解析规则即可），否则为 `single_page`（单页异常，逐页核对数据）。
每组返回 `page_count` 与 `example_urls` 示例链接。

**未核全保护**：抓取或解析失败的页面记入 `failures[]`（含地址与阶段），
已完成的诊断结果保留，同时整站 `status=incomplete`（未核全），不会报为正常；
全部页面核全时 `status=complete`。

## 目录结构

```
app/                 FastAPI 采集服务
  app/models.py      Pydantic 模型（查询/诊断入参出参、公告实体）
  app/parser.py      BeautifulSoup 解析：分页 rel=next、详情链接、详情字段
  app/crawler.py     同站分页 + 详情抓取（httpx 异步，限量/去重/错误收集）
  app/engine.py      公告链还原与有效性判定（纯函数，无 IO）
  app/diagnose.py    采集质量诊断：字段检查、结构签名、异常归组（纯函数，无 IO）
  app/main.py        POST /query、POST /diagnose、GET /health
  Dockerfile
site/                本地示例公告站（数据驱动生成纯静态 HTML）
  build_site.py      生成 html/（主栏目 3 列表页 + 13 详情；v2 改版栏目 2 列表页 + 9 详情）
  Dockerfile         python -m http.server 托管静态站
tests/               38 个单元/集成测试（pytest）
demo/                本地与 Compose 演示脚本
docker-compose.yml   api(8000) + notice-site(8080)
```

## 运行方式

### 方式一：Docker Compose（推荐，完整离线演示）

```bash
python3 site/build_site.py          # 生成示例站静态文件
docker compose up -d --build

curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"index_url":"http://notice-site:8080/index.html",
       "query_time":"2026-11-11T10:00:00+08:00"}'

# 采集质量诊断：改版后栏目
curl -s -X POST http://127.0.0.1:8000/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"index_url":"http://notice-site:8080/v2/index.html"}'
```

容器内 API 通过 Compose 网络访问 `notice-site`；从宿主机调用时 `index_url`
也可换成 `http://127.0.0.1:8080/index.html`（两个地址同构，同站判定均成立）。
交互式文档：http://127.0.0.1:8000/docs

### 方式二：本地 venv（无 Docker 环境）

```bash
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py        # 若 ensurepip 不可用，先 bootstrap pip
.venv/bin/pip install -r app/requirements.txt
./demo/run_local_demo.sh           # 自动建站、起两个服务并发起查询
```

### 测试

```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest -q      # 需先在 127.0.0.1:8080 启动示例站
```

## 示例站点场景（查询时刻 2026-11-11 10:00 +08:00）

| 公告 | 场景 | 判定结果 |
| --- | --- | --- |
| N-2026-001 | 12 路原始停运 | 有效 |
| N-2026-002 | 18 路停运已结束 | `expired` |
| 003 → 006 → 010 | 25 路停运被两次更正（更正引用更正） | 有效，区间为 010 替换后的 11-10 ~ 11-20 |
| 004 → 007 | 33 路停运被撤销 | `cancelled`，整条链失效 |
| N-2026-005 | 12 路另一条独立停运；012 更正次日才发布 | 有效，链上只有 005 |
| N-2026-008 | 5 路停运尚未开始 | `not_yet_effective` |
| 009 → 011 | 40 路更正，且 011 误引用不存在的 N-2026-999 | 有效，同时报告悬空引用 |
| N-2026-013 | 50 路整条公告查询时刻之后才发布 | `not_yet_effective` |
| N-EXT-001 | 列表中的跨站链接 | 跳过，出现在 `skipped_offsite_links` |

## 改版后栏目场景（v2/，供 /diagnose 演示）

| 公告 | 场景 | 诊断结果 |
| --- | --- | --- |
| N-2026-101/102/103 | 新模板 `dl.notice-meta`，字段整体搬出 `table.notice-fields` | 5 个字段 × 3 页，`template_change` 成批组 |
| N-2026-104 | 同页两行“公告编号”值不同 | `notice_id` 冲突，`single_page` |
| N-2026-105 | 停运公告缺线路、缺停运结束时间 | `route`/`suspend_end` 缺失，`single_page` |
| N-2026-106 | 发布时间 `<time>` 缺时区偏移 | `published_at` 无效，`single_page` |
| N-2026-107 | 更正公告无任何引用编号 | `references` 缺失，`single_page` |
| N-2026-109 | 列表有链接但详情页不存在（404） | `failures[]` 记录地址，`status=incomplete` 未核全 |
| N-2026-110/111 | 结构完整的停运 + 更正（对照组） | 无异常 |

## 接入真实站点

真实页面结构不同时，只需替换 `app/parser.py` 中的 CSS 选择器与字段键映射
（`FIELD_KEYS` / `KIND_MAP`），引擎、爬虫与 API 契约无需改动。解析器约定详情页
使用 `<time datetime>` 携带带时区的 ISO 8601 时间、引用使用
`<a class="reference" data-ref-id="编号">`，避免依赖文案措辞。
