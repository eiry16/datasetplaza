# DatasetPlaza 对外契约（v0.44.2）

这份文件是平台对调用方（agent）的**可引用承诺**，也是判断一次改动是否算回归的依据。
工具 schema 会变，货架会涨，下面这些语义不该变。实现见 `bdsp/`。下文引用的
`reports/`、`scripts/` 指开发树，不随公开仓库分发；数字按原样保留以便复核口径。

---

## 一、边界：只指路，不代取

平台是**目录 + 判断层**，不是数据服务。它明确不做以下事情：

| 不做 | 原因 |
|---|---|
| 不代发取数请求、不落数据快照 | 源方的口径、配额与授权由源方负责 |
| 不持凭证、不代注册、不代付款 | `paid` / `authorized` 只给 `buy_url` 与 `next_step` |
| 不返回数值 | 线索**不是存在性证明**，也不含任何一格值 |
| 不解释许可条款、不替调用方判断合规性 | `license_summary` 未声明一律 `unknown` |
| 不把推断伪装成源方声明 | 见 §三 |

一句话：**「能取到什么、干不干净、准不准，由你按源方规范自行判断。」**

调用方拿到的交接单（`dp_usage`）只回答：这个源是什么形态、入口在哪、怎么鉴权、
日配额与分页上限、有哪些已知精度坑、源方有没有自己的 MCP（有就只给启动方式与
工具名，参数以源方 `tools/list` 为准）。平台不转录源方的 HTTP 参数。

---

## 二、错误码：什么时候必须说「不知道」

| 码 | 触发 | 调用方应该做什么 |
|---|---|---|
| `NO_MATCH` | `shelf_gap`：货架上**没有任何能力声明**能接住这个问题 | 不要拿字面相关的候选当答案；换问法或换源 |
| `ACCESS_RESTRICTED` | `access_gap`：路存在，但要注册 / 授权 / 付费 | 把 `buy_url` / `apply_at` **展示给用户点**，不要替用户付款或索要账号 |
| `OK` | 存在 `complete` 或经核实的强匹配 | 仍要自行到源方取值 |

`shelf_gap` 与 `access_gap` **互斥**：收费墙不是「数据不存在」。

### 「接得住」的判据（0.44.1 窄修）

`discover` 里 `substantive` 的判定是：

- `capability_match.quality == "strong"` → 接得住；
- 或 `verified_axes` 含 `topic` **且 `missing` 为空** → 接得住；
- 只有大类主题、但明确写着缺轴（`missing` 非空）的泛化卡 → **不算接得住**。

0.44.1 之前只有「主题命中即算」，被 2,504 张国家统计局泛化卡蹭了绿灯：
「2024 年电影票房」「城镇调查失业率」的 8 条候选全是 `partial`、无一 `complete`、
`missing` 还明写「缺产量与产值」，`tool_errors` 却判成了 `code=OK`。
加上「无具名缺口」这一条后两问回到 `NO_MATCH`；法国污水
（`verified=[geo,sector,topic]`、`missing=[]`）、CO₂、军工名录等真匹配不受影响。

**改动只影响标记与错误码，不删候选、不改排序**——77 题盲测重跑，
各组 `hit1`/`hit10`/`mrr` 逐项不变，只有 A 组一条 `OK` → `NO_MATCH`。

### 字面相关的候选也要说清楚

`literal_only` 标记、`fully_matched` 计数、`rejected`（首屏之外位次最高的 3 条
及剔除原因码）都是给 agent 的可解释性字段，不是第二排名。
「货架上真有这张表」与「这张表被声明能回答这个问题」是两件事：
0.44.2 之后 NBS 的 GDP 表只剩 `geo` 一轴进 `verified`，`shelf_gap` 会重新出现，
但候选里仍有真表——那是**证据不足**，不是无路。任何候选都不许因此被判 `strong`。

---

## 三、证据档位：谁说的，说到什么程度

能力卡每根轴都带 `axis_provenance`。档位决定它能不能进 `verified_axes`、
能不能解锁 `complete`：

| 档位 | 含义 | 进 `verified` | 解锁 `complete` |
|---|---|---|---|
| `declared` | 源方实测声明 | 是 | 是 |
| `source_mapped` | 从**源方自己的分类树路径**翻译来的（0.39 定义） | 否 | 否 |
| `inferred` | 我们按表名 / 词表推断的 | 否 | 否 |

名称词表命中**不能**说成 `source_mapped`——那是另一种伪造证据。

### 0.44.2 档位归位（一次真实的诚实性修复）

根因：`harvest_nbs.cmd_apply` 把从表名 / 分类树命中的 `topics` 覆盖进卡片后，
`capability.enrich_axes` 见「已有主题」就兜底盖章 `topics="declared"`，
于是 1,752 张卡从「我们按名称猜的」升成「发布方实测声明」，进 `verified`、
解锁 `complete`、每次白拿 +4.0 主题分。

按定位器逐条重算来源，精确分档、不一刀切：

| 来源 | 张数 | 归位后 |
|---|---|---|
| 表名命中词表 | 904 | `inferred` |
| 源方分类树路径 | 848 | `source_mapped` |
| `taxonomy.classify` 补轴卡 | 752 | 本就 `inferred`，连 `topics_reason` 原样不动 |

结果：NBS 的 `declared` **归零**，全货架 `declared` 2,531 → 779。
采集端 `_topic_of` 现在返回来源、`make_item` 显式写 `axis_provenance`，防复发；
存量由 `scripts/relabel_nbs_topics.py` 幂等原子写回，并加了
`test_nbs_cards_never_claim_declared_topics` 守卫。

---

## 四、可复核的评测基线

对外发布时，最容易被复用的可能不是工具，而是这几份带标注、带反例的语料：

| 基线 | 文件 | 内容 |
|---|---|---|
| 77 题口语盲测 | `reports/blind_eval_040.json` | 6 组（A / A2 / C / E / D_baseline / D_expanded），逐题 `code` / `rank` / top1 / 候选 |
| 改档位前基线 | `reports/blind_eval_040.before0442.json` | 0.44.2 之前逐题快照 |
| 0.39 60 问召回 | `reports/routing_efficiency.md` | 首条 44/60、前十 58/60、零丢失、11,053ms → 453ms |
| 0.40 全量 | `reports/version_0_44.md` | 615 项 / 6,179.6s |

0.44.2 的逐题核对结论：**77 题里 76 题逐字段完全不变**，唯一变化是
A13「香港各区空气污染指数历史记录」——top1 从 NBS「废水中主要污染物排放」
（rank 6 / `NO_MATCH`）变成香港环保署「过去空气污染指数记录」
（rank 1 / `ACCESS_RESTRICTED`）。A 组 `hit1` 4 → 5、`mrr` 0.3118 → 0.3535，
其余五组逐项不变。泛化卡不再靠虚假主题分把真答案挤下去。

`ACCESS_RESTRICTED` 不是「答出来了」：它诚实说明命中了需授权的源，平台不代销、不代取。

---

## 五、已知边界（不掩饰）

1. **元数据会过期**。源站改版、关闭、开始收费、API 迁移都会让判断失效。
   活例子：README 一度写「583 个用例 / 12 个工具」，当时实际是 652 / 13
   （现为 663 / 13）；
   IMF 老域名 `dataservices.imf.org` 的 502 曾被记成「本机代理问题」，
   而第三方资料显示该 API 已退役、新址是 `api.imf.org/external/sdmx/3.0`。
   **调用方须在源方复核访问层级与端点**，不要把目录判断当成实时承诺。
2. **目录不是全世界的目录**。37,847 条 / 6 片，偏中国公共统计；
   覆盖面本身不构成「这个数据不存在」的证明。
3. **不含数据**。Python 包只发 `bdsp/` 模块；313 MB `listings.json` 与
   `shelf`/`archive` 需从仓库或 Release 快照获取（`--data`）。
4. **单人维护**。没有 SLA；公共网关默认匿名、按 IP 限流、只追加一条留言。

---

## 六、版本与回归口径

改动若触及 `discover` 的 `substantive`、`tool_errors.classify`、
`capability.match` 的证据档位或 `axis_provenance` 写入路径，
**必须**同时跑：① 定向回归；② 77 题盲测逐题 diff（不只是组级指标）；
③ `test_nbs_cards_never_claim_declared_topics` 防复发守卫。
只改错误码、不动排序的窄修，判据是盲测 `hit1`/`hit10`/`mrr` 逐项不变。
