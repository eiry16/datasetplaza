"""货架存储分离（0.37）：路由读薄索引，详情按需取。

## 为什么

0.36 接完香港与 World Bank，货架 35,418 条、`listings.json` 240MB，一次
`load()` 5 秒上下。热路径（`dp_discover`）只需要「每条的名字、坐标、闸门、
货架层」，却被 240MB 的档案绑住——进程每次启动都付一次 5 秒，测试套件
跑不完，`dp_discover` 的召回加速也被这笔固定开销吃掉。

拆两半：

- ``shelf.jsonl``：薄索引。每行一条，只放路由与展示要用的字段（走短键
  压缩，35k 条约十几 MB）。`load_shelf()` 在这里读。
- ``archive/<shard>.jsonl`` + ``.idx``：完整档案，按平台分批存，配每行的
  字节偏移。`hydrate()` 按 id 直接 seek 读那一行，不再整份解析。

档案是**派生数据**：`listings.json` 仍是唯一真相（人可以改、脚本可以写），
薄索引与档案都由它重建（`scripts/build_shelf.py`）。所以两者都在
`.gitignore` 里——它们随时可以生成，不该进版本库。

## 边界

- 档案过期（`listings.json` 变新）时，`load_shelf()` 会**自动重建**一次。
  重建要读全量，约 5 秒；这是上架/改档之后的一次性代价，不是热路径。
- `hydrate()` 拿不到（id 不存在、分片缺失）就返回 None，调用方自己退到
  薄记录。宁可少字段，不能编字段。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "store-1"

DEFAULT_SOURCE = ROOT / "listings" / "listings.json"
SHELF = "shelf.jsonl"
ARCHIVE_DIR = "archive"

# ── 短键映射 ────────────────────────────────────────────────────────────
# 键名本身就占体积：35k 条 × 20 个键 × 平均 10 字符 ≈ 7MB，比不少字段的值
# 还大。落盘用短键，读回来立刻展开——展开是 35k × 20 次 dict 赋值，几毫秒。
_KEYS: dict[str, str] = {
    "dataset_id": "i", "name": "n", "publisher": "p", "publisher_level": "pl",
    "domain": "dm", "category": "c", "modality": "m", "kind": "k",
    "content_status": "cs", "access_level": "al", "source_url": "su",
    "data_url": "du", "declared_records": "dr",
    "published": "pb", "onboard_batch": "ob", "onboard_group": "og",
    "fields_count": "fc", "notes": "nt", "portal": "po", "hk": "hk",
    "capability": "cp", "harvest": "h",
    # 0.41：表内指标名。定位器整份留档案，薄索引只留名字——召回靠词面，
    # 名字就够；整份进薄索引会把 9,500 张表 × 8 列的坐标一起搬进来。
    "locator_terms": "lt",
}
_LONG = {v: k for k, v in _KEYS.items()}

# 能力卡**要**进薄索引：打分靠它，而随机读档案太贵（实测一条 16ms——
# Windows 上对大分片做 400 次 seek 要 6.5s，比整份薄索引还慢）。所以薄
# 索引留坐标轴，去掉的是**推断说明与中文标签**：`basis_note` 一条几百字、
# `sector_names`/`entity_labels` 都能按码表现查，加进来比坐标轴本身还大。
#
# 另外留两个派生短值（排序的硬输入，代价只有一个短串）：
#   t = shelf_tier(item)（content / gated / registry_only）
#   u = [form, gate]（要不要过闸门、怎么取）
TIER_KEY = "t"
USAGE_KEY = "u"

_CAP_KEYS = ("what", "asks", "entities", "measures", "sectors", "topics",
             "subtopics", "geo", "geo_label", "granularity", "role",
             "axis_provenance", "basis",
             # 0.39：名字推断的度量要能被审计；薄索引是白名单，不加就丢。
             "measures_basis", "measures_from_name", "axis_version")

NOTES_LIMIT = 60    # 薄索引里的说明只留开头：它参与字面匹配，但不是主证据


def slim(item: dict) -> dict:
    """一条完整条目 → 薄记录（短键）。字段是白名单，不是黑名单——新字段
    默认不进薄索引，要进得显式加到 ``_KEYS``，免得档案里的重家伙自动漏进来。
    """
    rec: dict = {}
    for long_key, short in _KEYS.items():
        if long_key == "fields_count":
            n = len(item.get("fields") or [])
            if n:
                rec[short] = n
            continue
        if long_key == "notes":
            txt = str(item.get("notes") or "").strip()
            if txt:
                rec[short] = txt[:NOTES_LIMIT]
            continue
        if long_key == "capability":
            cap = item.get("capability") or {}
            keep = {k: cap[k] for k in _CAP_KEYS if cap.get(k)}
            if keep:
                rec[short] = keep
            continue
        if long_key == "locator_terms":
            loc = item.get("locator") or {}
            names = [str(x.get("name") or "") for x in (loc.get("indicators") or [])
                     if isinstance(x, dict) and x.get("name")]
            if names:
                rec[short] = names[:40]
            continue
        if long_key == "harvest":
            hv = item.get("harvest") or {}
            if hv.get("status"):
                rec[short] = hv["status"]
            continue
        if long_key == "portal":
            po = item.get("portal") or {}
            if po:
                rec[short] = {k: po[k] for k in ("division_area", "shape", "topic")
                              if po.get(k) not in (None, "")}
            continue
        if long_key == "hk":
            hk = item.get("hk") or {}
            if hk:
                rec[short] = {k: hk[k] for k in ("organization", "license",
                                                 "update_frequency", "isopen")
                              if hk.get(k) not in (None, "")}
            continue
        v = item.get(long_key)
        if v in (None, "", [], {}, 0) and long_key != "published":
            continue
        if long_key == "data_url" and v == item.get("source_url"):
            continue   # 同一条 URL 存两遍是白占地方，档案里两列都在
        rec[short] = v

    from .listings import shelf_tier
    rec[TIER_KEY] = shelf_tier(item)
    use = item.get("usage") or {}
    pair = [use.get("form") or "", use.get("gate") or ""]
    if pair[0] or pair[1]:
        rec[USAGE_KEY] = pair
    return rec


def expand(rec: dict) -> dict:
    """薄记录 → 可路由的条目（长键）。缺的键不补——补就是编。"""
    out: dict = {}
    for short, v in rec.items():
        if short == TIER_KEY:
            out["shelf_tier"] = v
            continue
        if short == "h" and isinstance(v, str):
            out["harvest"] = {"status": v}
            continue
        if short == USAGE_KEY:
            form, gate = (list(v) + ["", ""])[:2]
            use = {}
            if form:
                use["form"] = form
            if gate:
                use["gate"] = gate
            out["usage"] = use
            continue
        key = _LONG.get(short)
        if key is None:          # 未来版本多出来的键，原样保留
            out[short] = v
            continue
        out[key] = v
    return out


# ── 分片 ────────────────────────────────────────────────────────────────
def shard_of(item: dict) -> str:
    """按上线批次分片。同批条目同源、同模板，重建与排查都是按批做的。"""
    b = str(item.get("onboard_batch") or "").strip()
    if not b:
        return "core"
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in b)


def paths(root: str | Path | None = None) -> dict:
    r = Path(root) if root else ROOT / "listings"
    return {"dir": r, "shelf": r / SHELF, "archive": r / ARCHIVE_DIR,
            "source": DEFAULT_SOURCE}


def is_fresh(root: str | Path | None = None, source: str | Path | None = None) -> bool:
    p = paths(root)
    src = Path(source) if source else p["source"]
    try:
        return p["shelf"].stat().st_mtime_ns >= src.stat().st_mtime_ns
    except OSError:
        return False


def build(items: list[dict], root: str | Path | None = None,
          source: str | Path | None = None) -> dict:
    """从完整条目重建薄索引 + 档案。"""
    p = paths(root)
    arch = p["archive"]
    arch.mkdir(parents=True, exist_ok=True)

    # 先按分片分组，逐片写 jsonl 并记字节偏移
    shards: dict[str, list[dict]] = {}
    for it in items:
        shards.setdefault(shard_of(it), []).append(it)

    meta_shards = {}
    for name, rows in shards.items():
        f = arch / (name + ".jsonl")
        idx: list[str] = []
        off = 0
        with f.open("w", encoding="utf-8", newline="\n") as fh:
            for it in rows:
                line = json.dumps({k: v for k, v in it.items()
                                   if not k.startswith("_")},
                                  ensure_ascii=False) + "\n"
                b = line.encode("utf-8")
                fh.write(line)
                idx.append("%s\t%d\t%d" % (it.get("dataset_id") or "", off, len(b)))
                off += len(b)
        (arch / (name + ".idx")).write_text("\n".join(idx) + "\n", encoding="utf-8")
        meta_shards[name] = {"rows": len(rows), "bytes": off}

    with p["shelf"].open("w", encoding="utf-8", newline="\n") as fh:
        for it in items:
            fh.write(json.dumps(slim(it), ensure_ascii=False) + "\n")

    from . import listings as listingmod   # 构建期算一次，别让翻目录时现算
    meta = {
        "schema": "dataset-plaza/shelf/%s" % VERSION,
        "stats": listingmod.stats(items),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(items),
        "shards": meta_shards,
        "shelf_bytes": p["shelf"].stat().st_size,
    }
    (arch / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    _SHELF_CACHE.clear()
    _IDX_CACHE.clear()
    return meta


# ── 读薄索引 ────────────────────────────────────────────────────────────
_SHELF_CACHE: dict = {}


def load_shelf(root: str | Path | None = None, source: str | Path | None = None,
               auto_build: bool = True, visible_only: bool = True) -> list[dict]:
    """路由用的货架。首次调用读 `shelf.jsonl`（十几 MB，约 0.2s）；之后按
    mtime 命中进程内缓存，等同于零成本。

    档案缺失或落后于 `listings.json` 时自动重建一次——被上架脚本改过的
    货架不会因为忘了跑构建而看起来「少了几千条」。
    """
    p = paths(root)
    key = str(p["shelf"])
    if not is_fresh(root, source):
        if not auto_build:
            return []
        from . import listings as listingmod
        full = listingmod.load(source)
        if not full:
            return []
        build(full, root, source)
    try:
        mt = p["shelf"].stat().st_mtime_ns
    except OSError:
        return []
    hit = _SHELF_CACHE.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    out: list[dict] = []
    with p["shelf"].open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = expand(json.loads(line))
            # 下架开关在薄索引里也必须生效：档案里留着它（后台能再上架），
            # 但 agent 侧一条都不能漏。
            if visible_only and rec.get("published") is False:
                continue
            out.append(rec)
    _SHELF_CACHE[key] = (mt, out)
    return out


# ── 按 id 取档案 ────────────────────────────────────────────────────────
_IDX_CACHE: dict = {}


def _index(root: str | Path | None = None) -> dict:
    """id → (分片, 偏移, 长度)。35k 条按 tab 分隔读，比 JSON 快一个量级。"""
    p = paths(root)
    arch = p["archive"]
    key = str(arch)
    try:
        mt = (arch / "meta.json").stat().st_mtime_ns
    except OSError:
        return {}
    hit = _IDX_CACHE.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    table: dict[str, tuple[str, int, int]] = {}
    for f in arch.glob("*.idx"):
        shard = f.stem
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                ds_id, off, ln = line.rstrip("\n").split("\t")
                if ds_id:
                    table[ds_id] = (shard, int(off), int(ln))
    _IDX_CACHE[key] = (mt, table)
    return table


def hydrate(ids, root: str | Path | None = None) -> dict[str, dict]:
    """按 id 读完整档案。未命中的 id 不出现在返回里——调用方自己决定退路。"""
    if isinstance(ids, str):
        ids = [ids]
    table = _index(root)
    arch = paths(root)["archive"]
    want: dict[str, list[tuple[str, int, int]]] = {}
    for ds_id in ids:
        pos = table.get(ds_id)
        if pos:
            want.setdefault(pos[0], []).append((ds_id, pos[1], pos[2]))
    out: dict[str, dict] = {}
    for shard, rows in want.items():
        f = arch / (shard + ".jsonl")
        try:
            fh = f.open("rb")
        except OSError:
            continue
        with fh:
            for ds_id, off, ln in rows:
                fh.seek(off)
                raw = fh.read(ln)
                try:
                    out[ds_id] = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
    return out


def shelf_stats(root: str | Path | None = None) -> dict:
    """货架统计（构建期算好存在 meta 里）。3.5 万条现算要三十多秒，
    翻一次目录就付一次，不值得。"""
    try:
        meta = json.loads((paths(root)["archive"] / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return meta.get("stats") or {}


def stats(root: str | Path | None = None) -> dict:
    p = paths(root)
    try:
        meta = json.loads((p["archive"] / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"built": False}
    meta["built"] = True
    meta["fresh"] = is_fresh(root)
    return meta
