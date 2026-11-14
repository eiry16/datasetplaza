"""Source-declared licence / metadata facts, with ``unknown`` as a first-class value.

The shelf has always answered "can I get in?" (``access.level`` + a gate
profile). It never answered "once I am in, may I redistribute what I get?",
nor "how often does the publisher update this?" — both of which decide whether
a source is usable for a given job. The design review (0.40) asked for those
as structured fields.

The hard rule here: **we only transcribe what the source declared**. No
crawling of terms pages, no "it's a government site so it must be CC BY", no
guessing an update frequency from the dataset's name. Where no declaration
exists the value is the string ``"unknown"`` and ``basis`` says why. A field
full of ``unknown`` is a useful answer: it tells the agent to go check, instead
of quietly implying permission.

Where the declarations live today (all local, all already on disk):

* ``item.hk.update_frequency`` — data.gov.hk CKAN field, 3,885/3,885 entries.
* ``listings/hub_meta.json`` — DCAT fields harvested from EU portals:
  ``license`` (36 × ``CC BY 4.0``), ``temporal`` (17), ``accrual_periodicity``
  (36), ``access_right`` (87), ``spatial`` (64). ``enrich.decorate()`` attaches
  the block as ``_hub_meta`` on full loads; the thin index does not carry it,
  so we also look it up by ``dataset_id``.

``CC BY 4.0`` is the only licence id we interpret. That interpretation is a
fact about the *licence family*, not a claim about a particular publisher, so
it is marked ``license_family`` and the id itself is always returned for the
agent to verify. Adding another family requires an explicit table entry.
"""
from __future__ import annotations

import ast
from collections import Counter

from . import enrich

VERSION = "source-terms-1"
UNKNOWN = "unknown"

# Only well-known, unambiguous licence ids are expanded. Anything else is
# returned verbatim as ``license_id`` with every permission left unknown.
_LICENSE_FAMILY = {
    "cc by 4.0": {
        "license_family": "CC-BY-4.0",
        "redistribution": "allowed_with_attribution",
        "attribution_required": True,
        "terms_url": "https://creativecommons.org/licenses/by/4.0/",
        "terms_url_basis": "license_family",
    },
    "cc-by-4.0": {},   # filled below by alias
}
_LICENSE_FAMILY["cc-by-4.0"] = _LICENSE_FAMILY["cc by 4.0"]
_LICENSE_FAMILY["cc by 4.0 international"] = _LICENSE_FAMILY["cc by 4.0"]

_NO_DECLARATION_NOTE = (
    "源方未在货架上声明许可；平台不实测、不推测、不按站点类型推断。"
    "再分发、署名、用途范围一律 unknown，请到源方条款页自行确认。"
)

# Fields we can honestly fill from a source declaration, and their fallbacks.
_FIELDS = ("license_id", "redistribution", "attribution_required",
           "allowed_purposes", "terms_url")


_META_ITEMS: dict | None = None


def clear_cache() -> None:
    """Call alongside ``enrich.clear_cache()`` when hub_meta.json is refreshed."""
    global _META_ITEMS
    _META_ITEMS = None


def _all_meta() -> dict:
    # `enrich.load()` re-derives str(Path) on every call; on Windows that is
    # pathlib-heavy and it showed up as ~13% of record() when shaping 35k rows.
    # The snapshot is immutable during a build, so cache the items dict here.
    global _META_ITEMS
    if _META_ITEMS is None:
        _META_ITEMS = enrich.load().get("items") or {}
    return _META_ITEMS


def _meta_of(item: dict) -> dict:
    m = item.get("_hub_meta")
    if isinstance(m, dict) and m:
        return m
    did = str(item.get("dataset_id") or "")
    return _all_meta().get(did) or {}


def _declared_license(item: dict, meta: dict) -> str:
    for v in ((item.get("hk") or {}).get("license"),
              item.get("license"), meta.get("license")):
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def license_summary(item: dict) -> dict:
    """Four question-answering fields + provenance. Never invents a permission."""
    meta = _meta_of(item)
    raw = _declared_license(item, meta)
    if not raw:
        return {
            "license_id": UNKNOWN, "redistribution": UNKNOWN,
            "attribution_required": UNKNOWN, "allowed_purposes": UNKNOWN,
            "terms_url": UNKNOWN, "terms_url_basis": UNKNOWN,
            "basis": "no_declaration", "note": _NO_DECLARATION_NOTE,
        }
    key = raw.lower().strip()
    fam = _LICENSE_FAMILY.get(key) or {}
    terms = ""
    if str(raw).startswith(("http://", "https://")):
        terms, terms_basis = raw, "source_declared"
    elif meta.get("license_url"):
        terms, terms_basis = str(meta["license_url"]), "source_declared"
    else:
        terms = fam.get("terms_url", UNKNOWN)
        terms_basis = fam.get("terms_url_basis", UNKNOWN)
    return {
        "license_id": raw,
        "license_family": fam.get("license_family", UNKNOWN),
        "redistribution": fam.get("redistribution", UNKNOWN),
        "attribution_required": fam.get("attribution_required", UNKNOWN),
        # A licence id does not declare the caller's purpose; only the source can.
        "allowed_purposes": UNKNOWN,
        "terms_url": terms,
        "terms_url_basis": terms_basis,
        "basis": "source_declared",
        "note": (f"源方声明 {raw}；"
                 + (f"按 {fam['license_family']} 许可族解释，请仍以条款页为准。"
                    if fam else "该标识不在平台已核的许可族表内，权限一律 unknown。")),
    }


def _temporal(raw) -> dict:
    """DCAT ``temporal`` is a list of dicts, sometimes stored as their repr."""
    out = {"from": UNKNOWN, "to": UNKNOWN}
    for x in raw or []:
        d = x
        if isinstance(x, str):
            try:
                d = ast.literal_eval(x)
            except (ValueError, SyntaxError):
                continue
        if not isinstance(d, dict):
            continue
        for key, target in (("gte", "from"), ("lte", "to")):
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                out[target] = v.strip()[:10]
    return out


def metadata(item: dict) -> dict:
    """Time / granularity facts, each with its own provenance.

    ``last_updated_at`` is deliberately always ``unknown``: the shelf carries
    our *probe* time (``probe.checked_at``) and the EU portal's *fetch* time
    (``fetched_at``), neither of which is the publisher's last update. Writing
    either into that slot would be a fabricated freshness signal.
    """
    hk = item.get("hk") or {}
    meta = _meta_of(item)
    cap = item.get("capability") or {}
    freq = hk.get("update_frequency") or meta.get("accrual_periodicity") or ""
    freq = str(freq).strip() if freq else ""
    tc = _temporal(meta.get("temporal"))
    spatial = meta.get("spatial") or []
    gran = cap.get("granularity") or ""
    return {
        "update_frequency": freq or UNKNOWN,
        "temporal_coverage": tc if tc["from"] != UNKNOWN or tc["to"] != UNKNOWN else UNKNOWN,
        # See docstring: no source declaration for this on the shelf.
        "last_updated_at": UNKNOWN,
        "access_right": str(meta.get("access_right") or "").strip() or UNKNOWN,
        "spatial": list(spatial) if isinstance(spatial, list) else [],
        "granularity": gran or UNKNOWN,
        "geo": cap.get("geo") or UNKNOWN,
        "basis": {
            "update_frequency": "source_declared" if freq else "no_declaration",
            "temporal_coverage": ("source_declared"
                                  if isinstance(tc, dict) and
                                  (tc["from"] != UNKNOWN or tc["to"] != UNKNOWN)
                                  else "no_declaration"),
            "last_updated_at": "no_declaration",
            "access_right": ("source_declared" if meta.get("access_right") else
                             "no_declaration"),
            # Capability axes are our declaration, not the publisher's.
            "granularity": "platform_capability" if gran else "no_declaration",
            "geo": "platform_capability" if gran else "no_declaration",
        },
    }


_COV_CACHE: dict = {}


def coverage(items: list[dict]) -> dict:
    """How much of the shelf actually carries each declaration.

    This number is the guard against fake precision: if only 0.1% of entries
    declare a licence, a hard "redistribution allowed" filter would silently
    drop 99.9% of good answers. Every consumer must read this before filtering.

    A 35k-entry scan costs ~0.6s, which would double ``dp_discover``.
    ``discover()`` calls ``visible(items)`` first, which builds a *new list*
    every time — caching on ``id(items)`` would miss on every single call.
    The dicts inside that list are shared shelf objects, so we key on the
    ids of the first and last element instead: stable for the same shelf,
    different for a different corpus with the same length.
    """
    first = id(items[0]) if items else 0
    last = id(items[-1]) if items else 0
    key = (VERSION, len(items), first, last)
    if key in _COV_CACHE:
        return _COV_CACHE[key]
    c: Counter = Counter()
    for i in items:
        hk = i.get("hk") or {}
        meta = _meta_of(i)
        if _declared_license(i, meta):
            c["license"] += 1
        if hk.get("update_frequency") or meta.get("accrual_periodicity"):
            c["update_frequency"] += 1
        if meta.get("temporal"):
            c["temporal_coverage"] += 1
        if meta.get("access_right"):
            c["access_right"] += 1
        if (i.get("capability") or {}).get("granularity"):
            c["granularity"] += 1
    total = len(items) or 1
    out = {
        "total": len(items),
        "declared": {k: c[k] for k in ("license", "update_frequency",
                                       "temporal_coverage", "access_right",
                                       "granularity")},
        "ratio": {k: round(c[k] / total, 4)
                  for k in ("license", "update_frequency", "temporal_coverage",
                           "access_right", "granularity")},
    }
    if len(_COV_CACHE) > 8:
        _COV_CACHE.clear()
    _COV_CACHE[key] = out
    return out


# ── Constraint evaluation (advisory only) ─────────────────────────────────
CONSTRAINT_KEYS = ("freshness_requirement", "quality_requirement",
                   "budget_limit", "purpose", "granularity", "regions")

_STALE_FREQ = ("一次性", "有需要时", "当有需要时", "资料需要更新时",
               "于新增设施及数据更新时", "never", "not planned", "one-off")


def check_constraints(need: dict, candidates: list[dict], cov: dict) -> dict:
    """Turn declared constraints into **warnings**, never into a filter.

    Why advisory: with `coverage()` at ~0.1% for licences and ~0% for temporal
    coverage, a hard filter would produce false negatives on essentially every
    question. The 0.39 lesson ("don't fabricate a judgement") applies here too:
    an unchecked constraint is reported as unchecked, not silently passed.
    """
    declared = {k: need.get(k) for k in CONSTRAINT_KEYS if need.get(k)}
    warnings: list[dict] = []
    checked: list[str] = []
    unchecked: list[str] = []
    ratios = cov.get("ratio") or {}

    fresh = need.get("freshness_requirement") or ""
    if fresh:
        if ratios.get("update_frequency", 0) > 0:
            checked.append("freshness_requirement")
            if fresh in ("realtime", "recent"):
                for c in candidates[:3]:
                    md = c.get("source_metadata") or metadata(c)
                    freq = md.get("update_frequency") or UNKNOWN
                    if freq == UNKNOWN:
                        warnings.append({
                            "constraint": "freshness_requirement",
                            "code": "UPDATE_FREQUENCY_UNDECLARED",
                            "dataset_id": c.get("dataset_id", ""),
                            "note": "源方未声明更新频率，「最新」无法在平台侧验证；"
                                    "到源方落地页看最近一次更新。",
                        })
                        break
                    if any(w in str(freq) for w in _STALE_FREQ):
                        warnings.append({
                            "constraint": "freshness_requirement",
                            "code": "UPDATE_FREQUENCY_MISMATCH",
                            "dataset_id": c.get("dataset_id", ""),
                            "note": f"源方声明更新频率为「{freq}」，与「{fresh}」"
                                    "这一时效要求可能不符；平台不替你判断，请回源核对。",
                        })
        else:
            unchecked.append("freshness_requirement")

    budget = need.get("budget_limit") or {}
    free_only = budget.get("free_only") is True if isinstance(budget, dict) \
        else str(budget) == "free_only"
    if free_only:
        checked.append("budget_limit")
        gated = [c for c in candidates[:3]
                 if (c.get("access") or {}).get("level") in ("paid", "authorized")
                 or (c.get("usage") or {}).get("gate") in ("paid", "authorized")]
        if gated:
            warnings.append({
                "constraint": "budget_limit", "code": "GATED_CANDIDATE",
                "dataset_id": gated[0].get("dataset_id", ""),
                "note": "本次要求免费，但首屏含源方收费/授权条目；把 buy_url 给用户，"
                        "平台不代付、不代取，也不要把它当成「没有数据」。",
            })
    elif budget:
        unchecked.append("budget_limit")

    purposes = need.get("purpose") or []
    if "redistribution" in purposes:
        checked.append("purpose")
        unknown = [c for c in candidates[:3]
                   if ((c.get("license_summary") or license_summary(c))
                       .get("redistribution") == UNKNOWN)]
        if unknown:
            warnings.append({
                "constraint": "purpose", "code": "REDISTRIBUTION_UNDECLARED",
                "dataset_id": unknown[0].get("dataset_id", ""),
                "note": "用途含再分发，但首屏候选的许可未声明；不能推定可再分发，"
                        "请读 license_summary.terms_url 或找源方确认。",
            })
    elif purposes:
        unchecked.append("purpose")

    quality = need.get("quality_requirement") or []
    if quality:
        checked.append("quality_requirement")
        weak = [c for c in candidates[:3] if c.get("weak_evidence")]
        if weak:
            warnings.append({
                "constraint": "quality_requirement", "code": "WEAK_EVIDENCE",
                "dataset_id": weak[0].get("dataset_id", ""),
                "note": "首屏含证据等级偏弱的条目（站根可达不等于条目存在）；"
                        "要求权威/可核验时先看 evidence_label 与 access_evidence。",
            })

    gran = need.get("granularity") or {}
    if gran:
        checked.append("granularity")
        for c in candidates[:3]:
            declared_gran = (c.get("source_metadata") or metadata(c)).get("granularity")
            if declared_gran and gran.get("temporal") and \
                    gran["temporal"] not in str(declared_gran):
                warnings.append({
                    "constraint": "granularity", "code": "GRANULARITY_UNVERIFIED",
                    "dataset_id": c.get("dataset_id", ""),
                    "note": f"能力卡声明粒度「{declared_gran}」，与要求的"
                            f"「{gran['temporal']}」不同；回源看字段字典再判断。",
                })
                break

    regions = need.get("regions") or []
    if len(regions) >= 2:
        checked.append("regions")
        for c in candidates[:3]:
            geo = (c.get("source_metadata") or metadata(c)).get("geo") or UNKNOWN
            if geo != UNKNOWN and all(r not in str(geo) for r in regions):
                warnings.append({
                    "constraint": "regions", "code": "REGION_COVERAGE_UNVERIFIED",
                    "dataset_id": c.get("dataset_id", ""),
                    "note": f"本次要跨 {len(regions)} 个地区，首屏候选声明覆盖「{geo}」；"
                            "平台不做多地区并集判断，逐条回源核对。",
                })
                break

    # Constraints we can say nothing about because the metadata is missing.
    for key in CONSTRAINT_KEYS:
        if declared.get(key) and key not in checked and key not in unchecked:
            unchecked.append(key)
    # 「验过了」不等于「验得出」：许可覆盖率 0.1% 时，再分发这条约束在
    # 绝大多数候选上根本无从判断。把这个事实明写出来，别让 agent 把
    # checked 读成「平台替我确认过了」。
    _backing = {
        "freshness_requirement": "temporal_coverage",
        "purpose": "license",
        "quality_requirement": "access_right",
    }
    unverifiable = [
        {"constraint": k, "metadata": meta_key,
         "shelf_ratio": ratios.get(meta_key, 0),
         "note": f"货架上只有 {ratios.get(meta_key, 0):.1%} 条目声明了 "
                 f"{meta_key}，这条约束在多数候选上无法验证。"}
        for k, meta_key in _backing.items()
        if declared.get(k) and ratios.get(meta_key, 0) < 0.5
    ]
    return {
        "mode": "advisory",
        "hard_filter_applied": False,
        "declared": declared,
        "checked": checked,
        "unchecked": unchecked,
        "unverifiable": unverifiable,
        "coverage": cov,
        "warnings": warnings[:6],
        "note": ("约束只出警告、不删候选：货架上多数源方未声明许可与时间覆盖，"
                 "硬过滤会把没有元数据但真实可取的条目全部误杀。"
                 "unchecked 里的约束平台无法验证，别把它读成「已满足」。"),
    }
