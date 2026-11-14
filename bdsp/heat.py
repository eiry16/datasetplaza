"""调用热度：只从审计账本数出来，不推断、不预估。

账本里五类事件回答的是**不同**问题，混成一个「访问量」会骗人：

    point  平台把问题指到了架上某条路牌 —— 人确实来了，但值没经过我们
    route  被检索列进候选 —— agent 可能根本没选它
    prove  出了可复验的证明 —— 准备取值，可能停在门禁
    gate   过了访问控制 —— 有权限，且确实要走这条数据
    fetch  真把值交付出去了 —— 唯一能算「被用过」的事件，还带计费

所以热度不是一个魔数，而是五个可核对的计数器加一个固定权重的合成分。
权重只写在这一处：改口径就改 ``WEIGHTS``，历史数据不用重算，
因为落库的是计数器本身，``score`` 每次读取时现算。

一条被推翻的旧取舍（2026-11-08）——原先写的是「没接入索引的目录条目
不参与热度」。这条是错的，错在把记账缺口当成了显示口径：

  * 货架条目唯一可能产生的事件就是「指路」，而当时 ``discover`` 根本
    没往账本写一行，于是它们的热度**结构性地恒为 0**，还被读成「没人要」；
  * 「未入索引」讲的是**我们能不能把值交出去**（``callable``），
    「热度」讲的是**有没有人找过它**（``counted``/``score``）。两件事，
    不能用一个 0 同时表达。

现在：指路进账本（``Platform.log_points``），货架条目按指路计热；
``pool`` 说明它在哪个池子里，分数不再被清零。两条边界照旧：
重建索引不清账本（``audit`` 是 ``CREATE TABLE IF NOT EXISTS``），
热度跨体检、跨刷新累积。

0.30 补记一条架构变更：0.27 把取数还给 agent 之后，平台不再产生
prove/gate/fetch；0.28 删快照时把 ``index.sqlite3``（连同 audit 表）一并删了，
于是后台热度读到一个不存在的账本，全表显示 0——**「没有账本」被画成了
「没人问」**，正是上面那段一直在避免的错误。

现行的账本是 ``runtime/ledger.jsonl``（``bdsp/ledger.py``），每次把接入入口
交给调用方追加一行 handoff。它一直完好（0.28 之后还在累积），只是热度没读它。
所以现在热度以交接账本为准：一次 handoff 折算成一次 ``point``（权重 1）。
sqlite 仅在交接账本不存在时作为旧部署的兼容回退，**两本不混算**——
``dp_usage`` 会同时写 jsonl 和 sqlite 的 point，混算等于把同一次交接数两遍。

历史五类计数（point/route/prove/gate/fetch）随 sqlite 一起没了，账本未进 git、
备份也删了，不可恢复；这里不伪造，只把现行口径接上并让 UI 分清
「没有账本」与「没人问」。
"""
from __future__ import annotations

import json

# 权重：一次 fetch 抵五条 route。理由不是精密标定，而是「走到越后面，
# 越说明这条数据真的被用上了」。数值固定写死，避免每次读数漂移。
# point 与 route 同为第一跳（把问题转手，值还没到手），所以都是 1：
# 指路看起来「更热」，只是因为货架条目没有后面几跳可走。
WEIGHTS = {"point": 1, "route": 1, "prove": 2, "gate": 3, "fetch": 5}

# 曝光（touch）：调用方连上来了、但没有提出任何数据需求（握手/枚举工具）。
# 它进账本、可以数，但**不进 WEIGHTS**——它是"有多少人抬头看了一眼牌子"，
# 不是"有多少人开口问"。两者分开，扫描器才刷不动排行榜。
EXPOSURE_ACTION = "touch"

# 卡片上「真调用」显示哪一个：只有 fetch 是把值交给了调用方。
SERVED_ACTION = "fetch"

# 这两类事件的命中对象写在 detail.candidates 里（一次事件打中多条）。
MULTI_TARGET = ("point", "route")

# 交接账本里算「被问过一次」的动作。与 ledger.BILLABLE_ACTIONS 对齐：
# 都是「调用方索取了入口」，浏览/枚举不在此列。
LEDGER_ACTIONS = frozenset({"handoff", "click_source", "request_credential"})

# card().basis 的三种账本状态：现行交接账本 / 旧 sqlite 回退 / 账本缺失。
LEDGER_STATE_CURRENT = "handoff_ledger"
LEDGER_STATE_FALLBACK = "audit_ledger"
LEDGER_STATE_MISSING = "no_ledger"

SORTS = ("heat", "name", "records", "added")


def blank() -> dict:
    return {"point": 0, "route": 0, "prove": 0, "gate": 0, "fetch": 0,
            "charged": 0.0, "last_at": ""}


def _candidates(detail_json: str) -> list[str]:
    """point / route 事件把命中的 dataset_id 写在 detail.candidates 里。"""
    try:
        detail = json.loads(detail_json or "{}")
    except ValueError:
        return []
    if not isinstance(detail, dict):
        return []
    return [c for c in (detail.get("candidates") or []) if isinstance(c, str) and c]


def compute(con) -> dict[str, dict]:
    """扫一遍账本 → {dataset_id: 计数器}。只返回账本里出现过的数据集。"""
    out: dict[str, dict] = {}
    rows = con.execute(
        "SELECT action, dataset_id, charged, detail_json, ts FROM audit"
    ).fetchall()
    for action, dataset_id, charged, detail_json, ts in rows:
        if action in MULTI_TARGET:
            targets = _candidates(detail_json)
        else:
            targets = [dataset_id] if dataset_id else []
        for ds in targets:
            rec = out.setdefault(ds, blank())
            if action in WEIGHTS:
                rec[action] += 1
            rec["charged"] = round(rec["charged"] + float(charged or 0.0), 4)
            ts = ts or ""
            if ts > rec["last_at"]:
                rec["last_at"] = ts
    for rec in out.values():
        rec["score"] = sum(WEIGHTS[a] * rec[a] for a in WEIGHTS)
    return out


def compute_ledger(rows: list[dict] | None) -> dict[str, dict]:
    """交接账本 → 热度计数器（0.30 起的现行口径）。

    一条 handoff = 一次「把入口交给调用方」，等价旧口径的 ``point``，
    权重 1。账本里没有金额字段（只有布尔 billable），所以 ``charged``
    恒为 0——不把「是否计费」编造成金额。坏行、非索取入口的动作、
    没有 dataset_id 的行都跳过，坏行不吞掉整本账。
    """
    out: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict) or row.get("_corrupt"):
            continue
        action = str(row.get("action") or "handoff")
        if action not in LEDGER_ACTIONS:
            continue
        ds = str(row.get("dataset_id") or "").strip()
        if not ds:
            continue
        rec = out.setdefault(ds, blank())
        rec["point"] += 1
        at = str(row.get("at") or "")
        if at > rec["last_at"]:
            rec["last_at"] = at
    for rec in out.values():
        rec["score"] = sum(WEIGHTS[a] * rec[a] for a in WEIGHTS)
    return out


def score_of(rec: dict | None) -> int:
    if not rec:
        return 0
    return int(rec.get("score") or sum(WEIGHTS[a] * int(rec.get(a) or 0) for a in WEIGHTS))


def card(rec: dict | None, item: dict, ledger_state: str = "") -> dict:
    """统一成卡片字段：账本里没它也给全 0，前端不用判空。

    两个轴必须分开返回，前端才不会又把「不可调用」画成「没人要」：

        counted / score   有没有人找过它（账本里的事件数与加权和）
        callable / pool   我们能不能把值交出去（索引池 / 货架池）

    ``ledger_state`` 是第三个状态：**账本在不在**。没传时沿用旧语义
    （有事件=audit_ledger / 没事件=no_event），保证既有调用方与测试不变；
    传了就由调用方说明这一屏的数来自哪本账，缺失时前端显示「账本缺失」
    而不是一个会被读成「没人问」的 0。
    """
    indexed = item.get("kind") == "indexed"
    base = blank()
    base.update({k: v for k, v in (rec or {}).items() if k in base})
    has_events = bool(rec)
    basis = ledger_state or ("audit_ledger" if has_events else "no_event")
    return {
        "score": score_of(rec),
        "point": int(base["point"]),
        "route": int(base["route"]),
        "prove": int(base["prove"]),
        "gate": int(base["gate"]),
        "fetch": int(base["fetch"]),
        "served": int(base["fetch"]),
        "charged": round(float(base["charged"] or 0.0), 4),
        "last_at": base["last_at"],
        "counted": has_events,
        "callable": indexed,
        "pool": "indexed" if indexed else "catalog_only",
        "basis": basis,
        "ledger_state": basis,
    }


def sort_items(items: list[dict], key: str = "heat") -> list[dict]:
    """排序。heat 是纯分数降序，不再按「可调用/不可调用」分组。

    旧实现把没入索引的条目一律压到队尾，理由是「它们不在同一个池子里」。
    指路进账本之后这个前提不成立了：一个被问过三十次的货架条目，热度就是
    该排在一个从没被问过的索引数据集前面。谁被找过，谁靠前。
    """
    if key == "name":
        return sorted(items, key=lambda i: (i.get("name") or ""))
    if key == "records":
        return sorted(items, key=lambda i: ((i.get("declared_records") or 0),
                                            i.get("name") or ""), reverse=True)
    if key == "added":
        return sorted(items, key=lambda i: ((i.get("added_at") or ""),
                                            i.get("name") or ""), reverse=True)
    return sorted(items, key=lambda i: (
        -int((i.get("heat") or {}).get("score") or 0),
        -int((i.get("heat") or {}).get("fetch") or 0),
        -int((i.get("heat") or {}).get("point") or 0),
        i.get("name") or "",
    ))


def stats(items: list[dict]) -> dict:
    """给页面抬头用的几个数：多少条有记录、多少条真被找过、总热度分。"""
    def h(i):
        return i.get("heat") or {}

    return {
        "tracked": sum(1 for i in items if h(i).get("counted")),
        "called": sum(1 for i in items if int(h(i).get("score") or 0) > 0),
        "pointed": sum(1 for i in items if int(h(i).get("point") or 0) > 0),
        "served": sum(1 for i in items if int(h(i).get("fetch") or 0) > 0),
        "score": sum(int(h(i).get("score") or 0) for i in items),
        "total": len(items),
    }
