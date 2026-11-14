"""Side-by-side comparability *warnings*, without fetching a single value.

``cross_platform_value.md`` promised that a cross-platform shelf lets an agent
put two suppliers next to each other. The dangerous half of that promise is
``compare_sources`` in the reviewed design: it would fetch, normalize and
deliver comparable numbers — which is exactly the proxy role this project
switched off in 0.26/0.27.

The useful, honest half is this module: **tell the agent why two road signs
may not describe the same thing**. One source counts establishments, another
counts people; one is annual, one is a snapshot; one declares CC BY 4.0, the
other declares nothing; one card was written by the publisher, the other was
inferred from a catalog title. Those differences are visible *before* anyone
fetches, and they are the reason a naive "source A said X, source B said Y"
comparison lies.

We compare declarations only. We never claim the numbers are comparable, and
we never produce a number. Unknown stays unknown: a missing declaration is
reported as a gap, not silently treated as "same as the other one".
"""
from __future__ import annotations

from . import capability as capmod
from . import source_terms as termsmod

VERSION = "compare-1"

# dimension -> (label, where the value comes from)
_DIMENSIONS = (
    ("geo", "地理覆盖"),
    ("granularity", "记录粒度"),
    ("measures", "度量轴"),
    ("entities", "主体轴"),
    ("temporal_coverage", "时间覆盖"),
    ("update_frequency", "更新频率"),
    ("redistribution", "再分发许可"),
    ("access_level", "准入门槛"),
)


def _row(item: dict) -> dict:
    cap = item.get("capability") or {}
    md = termsmod.metadata(item)
    lic = termsmod.license_summary(item)
    tc = md.get("temporal_coverage")
    return {
        "dataset_id": item.get("dataset_id") or "",
        "name": item.get("name") or "",
        "publisher": item.get("publisher") or "",
        "access_level": item.get("access_level") or "unknown",
        "shelf_tier": item.get("shelf_tier") or "unknown",
        "capability_basis": cap.get("basis") or "unknown",
        "license_summary": lic,
        "source_metadata": md,
        "values": {
            "geo": cap.get("geo") or "unknown",
            "granularity": cap.get("granularity") or "unknown",
            "measures": list(cap.get("measures") or []) or "unknown",
            "entities": list(cap.get("entities") or []) or "unknown",
            "temporal_coverage": tc,
            "update_frequency": md.get("update_frequency") or "unknown",
            "redistribution": lic.get("redistribution") or "unknown",
            "access_level": item.get("access_level") or "unknown",
        },
    }


def _norm(v) -> str:
    if isinstance(v, (list, tuple)):
        return "|".join(sorted(str(x) for x in v))
    if isinstance(v, dict):
        return "|".join(f"{a}={b}" for a, b in sorted(v.items()))
    return str(v)


def compare(items: list[dict]) -> dict:
    """Return the side-by-side declarations and the reasons they may not agree."""
    rows = [_row(i) for i in items if isinstance(i, dict)]
    if len(rows) < 2:
        return {"ok": False, "reason": "invalid_request",
                "hint": "并排至少需要 2 个条目。",
                "NEXT": "用 dp_discover 找两个候选，或 dp_catalog 按发布方挑。"}
    dims = []
    warnings: list[dict] = []
    unknown_dims = 0
    differing_dims = 0
    for key, label in _DIMENSIONS:
        vals = [_norm(r["values"][key]) for r in rows]
        unknown = sum(1 for v in vals if v in ("unknown", "", "{}", "[]"))
        distinct = set(vals)
        differs = len(distinct) > 1
        dims.append({
            "dimension": key, "label": label,
            "values": [r["values"][key] for r in rows],
            "same": not differs, "unknown_count": unknown,
        })
        if unknown:
            unknown_dims += 1
            warnings.append({
                "code": "UNDECLARED_METADATA", "dimension": key,
                "note": f"{label}有 {unknown}/{len(rows)} 条未声明；"
                        "没声明不等于「与另一条相同」，不能据此认定可比。",
            })
        if differs and not (unknown and len(distinct) == 2):
            differing_dims += 1
            code = {
                "geo": "GEO_MISMATCH", "granularity": "GRANULARITY_MISMATCH",
                "measures": "MEASURE_MISMATCH", "entities": "ENTITY_MISMATCH",
                "temporal_coverage": "TEMPORAL_MISMATCH",
                "update_frequency": "UPDATE_CADENCE_MISMATCH",
                "redistribution": "LICENSE_MISMATCH",
                "access_level": "ACCESS_MISMATCH",
            }[key]
            warnings.append({
                "code": code, "dimension": key,
                "note": f"{label}不一致（{' / '.join(sorted(distinct))[:160]}）；"
                        "并排展示前先按源方字段字典确认是不是同一个口径。",
            })

    bases = {r["capability_basis"] for r in rows}
    if len(bases) > 1:
        warnings.append({
            "code": "BASIS_MIXED", "dimension": "capability_basis",
            "note": f"能力卡来历不同（{' / '.join(sorted(bases))}）："
                    "一方是源方声明，另一方是平台按目录推的，推的要回源核对。",
        })
    tiers = {r["shelf_tier"] for r in rows}
    if "registry_only" in tiers and len(tiers) > 1:
        warnings.append({
            "code": "TIER_MIXED", "dimension": "shelf_tier",
            "note": "其中一条只在册（无可交付内容），另一条手里有货；"
                    "它们不该被读成两个等价的数据源。",
        })

    key_unknown = sum(1 for d in dims
                      if d["dimension"] in ("geo", "granularity", "measures",
                                            "temporal_coverage")
                      and d["unknown_count"])
    if key_unknown >= 2 or (unknown_dims >= 4 and differing_dims):
        verdict = "not_comparable"
        verdict_note = "关键口径多数未声明，平台无法判定可比；不要并排给数值结论。"
    elif warnings:
        verdict = "caution"
        verdict_note = "声明存在差异或空缺，可以并排列路牌，不能并排列数值。"
    else:
        verdict = "declared_same"
        verdict_note = ("所比维度都有声明且一致；这仍只是**声明一致**，"
                        "字段字典、单位、口径与精度由你按源方规范核对。")

    return {
        "ok": True,
        "version": VERSION,
        "count": len(rows),
        "rows": rows,
        "dimensions": dims,
        "comparability": verdict,
        "comparability_note": verdict_note,
        "warnings": warnings,
        "unknown_dimensions": unknown_dims,
        "differing_dimensions": differing_dims,
        "platform_fetches_data": False,
        "proof_available": False,
        "boundary": ("平台只并排**声明**，不取数、不换算单位、不做字段映射、"
                     "不出质量分。数值能不能比，由你按两个源方各自的字段字典"
                     "与条款判断；许可未声明时不要推定可再分发。"),
        "NEXT": ("先读 warnings 里每条 dimension 对应的差异；需要数值时分别"
                 "dp_usage(dataset_id) 取交接单，直连各自源方取数，"
                 "再自行核对粒度/单位/时间覆盖。不要把本工具的 declared_same "
                 "当成平台替你验证过口径。"),
    }
