"""平台原生元数据的读取与挂载（categories / distributions）。

为什么单独一层：这些字段**不是我们判定的**，是平台自己声明的，由
``scripts/enrich_eu_metadata.py`` 增量抓取后落在 ``listings/hub_meta.json``，
按 ``dataset_id`` 与目录条目对齐。它有三个用途，都只做「转述」不做「推断」：

1. 平台页按平台自己的主题分组（否则只能按我们的门类分组 = 伪造原生分组）
2. 形态轴用 distribution 的 format 兜底（补上名称看不出的 50%）
3. 单张卡上告诉 agent「你拿到手是 CSV 还是 PDF」，省掉一次试错

设计约束：``listings.json`` 是采集脚本的产物，本模块**只读不写**它；元数据
以 ``_hub_meta`` 键挂在内存里的条目上，``save()`` 会剥掉这个键再落盘。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_META = ROOT / "listings" / "hub_meta.json"

_CACHE: dict[str, dict] = {}


def load(path: str | Path | None = None) -> dict:
    """读取元数据快照。文件缺失或损坏时返回空结构——降级，不抛异常。"""
    p = Path(path or DEFAULT_META)
    key = str(p)
    if key in _CACHE:
        return _CACHE[key]
    empty = {"schema": "", "generated_at": "", "stats": {}, "items": {}, "unresolved": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _CACHE[key] = empty
        return empty
    if not isinstance(data, dict):
        _CACHE[key] = empty
        return empty
    data.setdefault("items", {})
    data.setdefault("stats", {})
    data.setdefault("unresolved", [])
    _CACHE[key] = data
    return data


def clear_cache() -> None:
    _CACHE.clear()


def decorate(items: list[dict], meta: dict | None = None) -> list[dict]:
    """把 ``_hub_meta`` 挂到条目上（返回新字典，不改入参）。"""
    m = (meta if meta is not None else load()).get("items") or {}
    if not m:
        return items
    out: list[dict] = []
    for item in items:
        hit = m.get(item.get("dataset_id") or "")
        if hit:
            d = dict(item)
            d["_hub_meta"] = hit
            out.append(d)
        else:
            out.append(item)
    return out


def categories_of(item: dict) -> list[dict]:
    """平台声明的主题：[{id, en}]。缺失返回空列表（未知 ≠ 无主题）。"""
    meta = item.get("_hub_meta") or {}
    cats = meta.get("categories") or []
    return [c for c in cats if isinstance(c, dict)]


def native_labels(item: dict) -> list[str]:
    """平台主题的短标签：有官方码就用码，源方自定义只有名称就用名称。"""
    out = []
    for c in categories_of(item):
        code = (c.get("id") or "").strip()
        label = (c.get("en") or "").strip()
        out.append(code or label)
    return [x for x in out if x]


def summary_of(item: dict) -> str:
    """源方的一句话描述（截断 300 字符）。

    为什么需要它：目录里 213 条的名称有一半是编号（``ETA02 - Environment
    Taxes``、``MIP05 - Energy productivity``、``ICP Forests Level II``），
    agent 光看名字判不出"这到底装了什么"，只能去抓页面——那是最贵的一步。
    中文条目基本没有这个字段（源站是 SPA），所以空字符串是常态，不是缺陷。
    """
    meta = item.get("_hub_meta") or {}
    return (meta.get("description") or "").strip()


def summary_lang(item: dict) -> str:
    """描述的原始语言。欧盟条目里有一半是 lv/uk/el，标出来免得被当成英文。"""
    return ((item.get("_hub_meta") or {}).get("description_lang") or "").strip()


def native_keywords(item: dict) -> list[dict]:
    """源方自己的关键词 ``[{label, lang}]``。

    不做翻译、不做归一、不进主题判定：欧盟这批 41/50 条带关键词，但语言是
    fr/sl/en/uk 混排，还夹着站内 slug（``transparencia-sns-gov-pt``、``Vda``），
    映射不到 DCAT 的 data-theme。它只用来帮检索匹配"用户原话里的词"。
    """
    kws = (item.get("_hub_meta") or {}).get("keywords") or []
    return [k for k in kws if isinstance(k, dict) and (k.get("label") or "").strip()][:8]


def distribution_summary(item: dict) -> dict:
    """分发摘要：几个文件、什么格式、是不是高价值数据集(HVD)。"""
    meta = item.get("_hub_meta") or {}
    dist = meta.get("distributions") or {}
    formats = [f for f in (dist.get("formats") or []) if f.get("id")]
    return {
        "count": dist.get("count") or 0,
        "formats": [f"{f['id']}×{f.get('n', 1)}" if (f.get("n") or 1) > 1 else f["id"]
                    for f in formats[:5]],
        "is_hvd": bool(meta.get("is_hvd")),
        "accrual_periodicity": meta.get("accrual_periodicity") or "",
    }


def coverage() -> dict:
    """当前快照的覆盖率，给体检脚本和文档引用同一份数字。"""
    data = load()
    return dict(data.get("stats") or {})
