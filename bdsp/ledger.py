"""使用记账（handoff ledger）：谁在什么时候被指到哪个源。

为什么平台要记这个：平台不代取、不代收，那它对源方的价值就是**把需求送到
门口**。记下「某个源今天被指路多少次」，就是以后和源方谈分成/计费的唯一
凭据——没有这个，平台只是个免费黄页。

三条硬约束：

1. **只追加**。不更新、不删除；对账靠 ref 去重，出错就追加一条更正记录。
2. **不存用户数据**。不记 query 原文（可能含人名、地址、手机号），只记
   dataset_id / 级别 / 时间 / 调用方标识（agent 自报，可为空）。
3. **不存凭证**。档案里的 token 都是占位符，记账更不碰。

文件格式：JSONL，一行一条，便于 `grep` 和逐行导入。默认落在
``runtime/ledger.jsonl``（运行时数据，不进 git），可用 ``BDSP_LEDGER`` 覆盖。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()

#: 「点击/索取入口」类动作才计费；单纯浏览不计。
BILLABLE_ACTIONS = frozenset({"handoff", "click_source", "request_credential"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_path() -> Path:
    env = os.environ.get("BDSP_LEDGER", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "runtime" / "ledger.jsonl"


def _ref(at: str, dataset_id: str) -> str:
    raw = f"{at}|{dataset_id}|{uuid.uuid4().hex}".encode()
    return "use_" + hashlib.sha256(raw).hexdigest()[:20]


def record(dataset_id: str, *, level: str = "", action: str = "handoff",
           dataset_name: str = "", actor: str = "", conversation_id: str = "",
           endpoint: str = "", buy_url: str = "", profile_basis: str = "",
           price_model: str = "", path: Path | None = None) -> dict:
    """记一次使用交接，返回可展示给调用方的凭据。

    ``billable`` 只在「源方要收费/要授权」且动作是索取入口时为 True：
    免费开放源也记（用于排序与冷热统计），但不产生任何应付金额。
    """
    at = _now()
    ref = _ref(at, dataset_id)
    paywalled = level in ("paid", "authorized", "registered")
    billable = bool(paywalled and action in BILLABLE_ACTIONS)
    row = {
        "ref": ref,
        "at": at,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name[:120],
        "level": level,
        "action": action,
        "billable": billable,
        # 计费依据写死在记录里，免得以后对账时各说各话。
        "bill_basis": ("handoff_to_source" if billable else "usage_only"),
        "actor": (actor or "")[:80],
        "conversation_id": (conversation_id or "")[:80],
        "endpoint_host": _host(endpoint),
        "buy_url": buy_url[:300],
        "profile_basis": profile_basis,
        "price_model": price_model,
    }
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _LOCK:
        # 一行一次 write，避免多进程交错写坏行（O_APPEND 下小写入是原子的）。
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    return {
        "ref": ref,
        "at": at,
        "billable": billable,
        "bill_basis": row["bill_basis"],
        "note": ("已记账：若用户最终在源方付费，这条即分成依据；金额由源方结算，"
                 "平台不经手" if billable else "使用统计（不计费）"),
    }


def _host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(str(url or "")).netloc[:120]
    except ValueError:
        return ""


def read(path: Path | None = None) -> list[dict]:
    p = Path(path) if path else default_path()
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # 坏行不吞掉整个账本：跳过并计数（调用方能看到 skipped>0）。
            out.append({"_corrupt": True})
    return out


def summary(path: Path | None = None, dataset_id: str = "") -> dict:
    """按源聚合：总次数、计费次数、最近一次。给源方对账用。"""
    rows = [r for r in read(path) if not r.get("_corrupt")]
    if dataset_id:
        rows = [r for r in rows if r.get("dataset_id") == dataset_id]
    agg: dict[str, dict] = {}
    for r in rows:
        k = str(r.get("dataset_id") or "")
        a = agg.setdefault(k, {"dataset_id": k, "name": r.get("dataset_name", ""),
                               "handoffs": 0, "billable": 0,
                               "last_at": "", "levels": set()})
        a["handoffs"] += 1
        a["billable"] += 1 if r.get("billable") else 0
        if str(r.get("at") or "") > a["last_at"]:
            a["last_at"] = str(r.get("at") or "")
        if r.get("level"):
            a["levels"].add(str(r["level"]))
    items = []
    for a in agg.values():
        a["levels"] = sorted(a["levels"])
        items.append(a)
    items.sort(key=lambda x: (-x["handoffs"], x["dataset_id"]))
    return {"total_handoffs": len(rows), "results": items,
            "billable_total": sum(i["billable"] for i in items)}
