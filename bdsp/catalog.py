"""Control-plane catalog: what the admin console is allowed to show.

A manifest is public by design: it is the dataset's self-introduction. This
module adds the operator-facing view on top of it -- category, developer,
connection state, health and field-level public detail -- without ever
putting credentials into the manifest.

Two states are deliberately kept separate:

  * service_status  : can the platform answer queries right now? Depends on
    the pinned snapshot, the built index and the declaration audit.
  * upstream_status : is the publisher endpoint reachable right now? A dead
    upstream does NOT make a pinned dataset unavailable; it only makes its
    next refresh impossible. Conflating the two would report false outages.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .contract import iter_sources

CATEGORIES = [
    {"id": "public", "label": "公共数据", "hint": "政府 / 国际组织 / 公共机构开放的统计数据"},
    {"id": "commercial", "label": "商业数据", "hint": "企业经营、交易、市场与供应商数据"},
    {"id": "production", "label": "生产数据", "hint": "产线、设备、IoT、供应链运行数据"},
    {"id": "research", "label": "科研数据", "hint": "论文、实验、观测与评测基线"},
    {"id": "other", "label": "其他", "hint": "尚未归类，建议人工复核"},
]
CATEGORY_IDS = {c["id"] for c in CATEGORIES}
CATEGORY_LABELS = {c["id"]: c["label"] for c in CATEGORIES}

ACCESS_LABELS = {
    "open": "开放",
    "registered": "需鉴权",
    "authorized": "需授权",
    "paid": "需付费",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _publisher_defaults(src: dict) -> dict:
    """Honest fallbacks for manifests written before the catalog existed."""
    url = str(src.get("source_url", ""))
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or ""
    homepage = ""
    if parsed.scheme in ("http", "https") and host:
        homepage = f"{parsed.scheme}://{host}"
    return {
        "name": src.get("publisher", "") or host or "未提供",
        "org_type": src.get("publisher_level", "other"),
        "homepage": homepage,
        "contact": "",
    }


def catalog_meta(src: dict) -> dict:
    """Normalize the optional catalog/connection blocks. Never invents facts."""
    cat = dict(src.get("catalog") or {})
    conn = dict(src.get("connection") or {})
    category = cat.get("category") or "other"
    if category not in CATEGORY_IDS:
        category = "other"
    return {
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "category_source": cat.get("category_source", "legacy"),
        "category_reason": cat.get("category_reason", "历史契约未标注分类，默认归为其他"),
        "developer": {**_publisher_defaults(src), **(cat.get("developer") or {})},
        "tags": list(cat.get("tags") or []),
        "added_at": cat.get("added_at") or src.get("retrieved_at", ""),
        "enabled": bool(cat.get("enabled", True)),
        "notes": cat.get("notes", ""),
        "endpoint": conn.get("endpoint") or src.get("source_url", ""),
        "method": conn.get("method", "GET"),
        "auth_scheme": conn.get("auth_scheme", "none"),
        "credential_required": bool(conn.get("credential_required", False)),
        "content_type": conn.get("content_type", ""),
        "last_checked": conn.get("last_checked", ""),
        "last_error": conn.get("last_error", ""),
    }


def _short(sha: str, n: int = 12) -> str:
    return (sha or "")[:n]


def _row_map(platform) -> dict:
    try:
        return {r["dataset_id"]: r for r in platform.datasets()}
    except Exception:
        return {}


def _local_checks(src: dict, row: dict | None) -> dict:
    """Cheap, deterministic checks. No network, no side effects."""
    folder = Path(src["_dir"])
    raw_path = folder / src["raw_file"]
    snapshot_exists = raw_path.exists()
    sha = ""
    snapshot_ok = False
    if snapshot_exists:
        sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        snapshot_ok = sha == src.get("sha256")
    indexed = bool(row)
    claims_ok = bool(row and row.get("claim_status") == "consistent")
    return {
        "snapshot_file": snapshot_exists,
        "snapshot_hash": snapshot_ok,
        "indexed": indexed,
        "claims": claims_ok,
    }


def service_status(checks: dict, enabled: bool) -> tuple[str, str]:
    if not enabled:
        return "disabled", "已停用"
    if checks["snapshot_file"] and checks["snapshot_hash"] and checks["indexed"]:
        if checks["claims"]:
            return "online", "可用"
        return "degraded", "降级（声明不符）"
    return "offline", "离线"


def records(
    sources_root, platform, health_cache: dict | None = None, listings_root=None
) -> list[dict]:
    """One row per dataset: indexed contracts first, then catalogue-only shelf entries.

    The two kinds share the same table but never the same guarantees: a
    catalogue-only row carries ``indexed=False`` and is excluded from routing.
    """
    health_cache = health_cache or {}
    rows = _row_map(platform)
    out = []
    for src in iter_sources(sources_root):
        cat = catalog_meta(src)
        row = rows.get(src["dataset_id"])
        checks = _local_checks(src, row)
        status, status_label = service_status(checks, cat["enabled"])
        cached = health_cache.get(src["dataset_id"], {})
        acc = src["access"]
        price = acc.get("price") or {}
        fields = src.get("fields") or []
        out.append(
            {
                "dataset_id": src["dataset_id"],
                "name": src["name"],
                "category": cat["category"],
                "category_label": cat["category_label"],
                "category_source": cat["category_source"],
                "developer": cat["developer"],
                "publisher": src["publisher"],
                "publisher_level": src["publisher_level"],
                "domain": src.get("domain", []),
                "modality": src.get("modality", ""),
                "license": src.get("license", ""),
                "update_frequency": src.get("update_frequency", ""),
                "access_level": acc["level"],
                "access_label": ACCESS_LABELS.get(acc["level"], acc["level"]),
                "is_paid": acc["level"] == "paid",
                "price": price,
                "status": status,
                "status_label": status_label,
                "upstream_status": cached.get("upstream_status", "unchecked"),
                "upstream_label": _upstream_label(cached.get("upstream_status", "unchecked")),
                "last_checked": cached.get("checked_at", cat["last_checked"]),
                "last_error": cached.get("error", cat["last_error"]),
                "checks": checks,
                "enabled": cat["enabled"],
                "version": str(src["version"]),
                "fields_count": len(fields),
                "field_names": [f["name"] for f in fields],
                "declared_records": src["coverage"].get("declared_records", 0),
                "actual_records": row.get("actual_records") if row else None,
                "cells": row.get("actual_cells") if row else None,
                "claim_status": row.get("claim_status") if row else "unknown",
                "index_digest": _short(row.get("index_digest") if row else ""),
                "snapshot_sha256": _short(src.get("sha256", "")),
                "source_url": cat["endpoint"],
                "added_at": cat["added_at"],
                "retrieved_at": src.get("retrieved_at", ""),
                "kind": "indexed",
                "content_status": "indexed",
                "content_label": "已接入索引",
                "linked_dataset_id": "",
                "catalog_url": cat["endpoint"],
                "evidence": {"level": "local_snapshot", "source_url": cat["endpoint"],
                             "note": "本地固定快照 + 字段级索引 + 声明审计"},
                "evidence_label": "本地快照已校验",
                "weak_evidence": False,
                "probe": {"upstream_status": cached.get("upstream_status", "unchecked"),
                          "note": cached.get("error", ""), "http_status": 0,
                          "checked_at": cached.get("checked_at", "")},
            }
        )
    from . import listings as listingmod  # local import: listings imports catalog

    for item in listingmod.load(listings_root):
        out.append(listingmod.record(item))
    return out


def _upstream_label(code: str) -> str:
    return {
        "reachable": "上游可达",
        "unreachable": "上游不可达",
        "auth_failed": "凭证失效",
        "empty_response": "源返回空数据",
        "local": "本地快照",
        "skipped": "无需探测",
        "unchecked": "未体检",
    }.get(code, code)


def overview(items: list[dict], platform) -> dict:
    by_category = []
    for c in CATEGORIES:
        group = [i for i in items if i["category"] == c["id"]]
        by_category.append(
            {
                **c,
                "count": len(group),
                "indexed": sum(1 for i in group if i["kind"] == "indexed"),
                "catalog_only": sum(1 for i in group if i["kind"] == "catalog_only"),
                "online": sum(1 for i in group if i["status"] == "online"),
                "paid": sum(1 for i in group if i["is_paid"]),
                "fields": sum(i["fields_count"] for i in group),
            }
        )
    stats = {}
    try:
        stats = platform.stats()
    except Exception as exc:  # index not built yet
        stats = {"ok": False, "reason": str(exc)}
    listing_rows = [i for i in items if i.get("kind") == "catalog_only"]
    shelf = {
        "total": len(listing_rows),
        "indexed": sum(1 for i in listing_rows if i.get("linked_dataset_id")),
        "catalog_only": sum(1 for i in listing_rows if not i.get("linked_dataset_id")),
        "reachable": sum(1 for i in listing_rows if i.get("upstream_status") == "reachable"),
        "unreachable": sum(1 for i in listing_rows if i.get("upstream_status") == "unreachable"),
        "auth_failed": sum(1 for i in listing_rows if i.get("upstream_status") == "auth_failed"),
        "empty_response": sum(1 for i in listing_rows if i.get("upstream_status") == "empty_response"),
        "paid": sum(1 for i in listing_rows if i.get("is_paid")),
        "gated": sum(1 for i in listing_rows if i.get("access_level") != "open"),
        "weak_evidence": sum(1 for i in listing_rows if i.get("weak_evidence")),
        "connect_errors": sum(1 for i in listing_rows if i.get("connect_error")),
        # 上架/下架全量计数（0.39.2）：目录改分页后前端只有一页，
        # 「对 agent 可见多少条」必须由后端给，不能在浏览器里数。
        "published_on": sum(1 for i in listing_rows if i.get("published") is not False),
        "published_off": sum(1 for i in listing_rows if i.get("published") is False),
    }
    return {
        "ok": True,
        "total": len(items),
        "indexed_total": sum(1 for i in items if i["kind"] == "indexed"),
        "catalog_only": sum(1 for i in items
                            if i["kind"] == "catalog_only" and not i.get("linked_dataset_id")),
        "linked_listings": sum(1 for i in items
                               if i["kind"] == "catalog_only" and i.get("linked_dataset_id")),
        "online": sum(1 for i in items if i["status"] == "online"),
        "degraded": sum(1 for i in items if i["status"] == "degraded"),
        "offline": sum(1 for i in items if i["status"] in ("offline", "disabled")),
        "paid": sum(1 for i in items if i["is_paid"]),
        "gated": sum(1 for i in items if i["access_level"] != "open"),
        "open": sum(1 for i in items if i["access_level"] == "open"),
        "fields": sum(i["fields_count"] for i in items),
        "records": sum((i["declared_records"] or 0) for i in items),
        "by_category": by_category,
        "shelf": shelf,
        "index": stats,
        "generated_at": now_iso(),
    }


def detail(src: dict, row: dict | None, health: dict | None = None) -> dict:
    """Everything the registry may disclose, modelled on a national catalog."""
    cat = catalog_meta(src)
    health = health or {}
    checks = _local_checks(src, row)
    status, status_label = service_status(checks, cat["enabled"])
    fields = src.get("fields") or []
    rates = {}
    if row and row.get("fill_rates_json"):
        rates = json.loads(row["fill_rates_json"])
    field_rows = [
        {
            **f,
            "fill_rate": rates.get(f["name"]),
        }
        for f in fields
    ]
    return {
        "ok": True,
        "dataset_id": src["dataset_id"],
        "name": src["name"],
        "category": cat["category"],
        "category_label": cat["category_label"],
        "category_source": cat["category_source"],
        "category_reason": cat["category_reason"],
        "tags": cat["tags"],
        "developer": cat["developer"],
        "publisher": src["publisher"],
        "publisher_level": src["publisher_level"],
        "domain": src.get("domain", []),
        "modality": src.get("modality", ""),
        "license": src.get("license", ""),
        "update_frequency": src.get("update_frequency", ""),
        "version": str(src["version"]),
        "status": status,
        "status_label": status_label,
        "upstream_status": health.get("upstream_status", "unchecked"),
        "upstream_label": _upstream_label(health.get("upstream_status", "unchecked")),
        "last_checked": health.get("checked_at", cat["last_checked"]),
        "last_error": health.get("error", cat["last_error"]),
        "checks": {**checks, **{k: v for k, v in health.get("checks", {}).items()}},
        "connection": src.get("connection", {}),
        "access": src["access"],
        "access_label": ACCESS_LABELS.get(src["access"]["level"], src["access"]["level"]),
        "evidence_policy": src["evidence_policy"],
        "coverage": src["coverage"],
        "commitment": src.get("commitment", {}),
        "representative_queries": src["representative_queries"],
        "fields": field_rows,
        "fields_count": len(fields),
        "row_key": src["row_key"],
        "ingest": src["ingest"],
        "source_url": cat["endpoint"],
        "credential_required": cat["credential_required"],
        "credential_configured": bool(health.get("credential_configured", cat["credential_required"])),
        "snapshot_sha256": src["sha256"],
        "raw_file": src["raw_file"],
        "retrieved_at": src.get("retrieved_at", ""),
        "added_at": cat["added_at"],
        "notes": cat["notes"],
        "index": {}
        if not row
        else {
            "actual_records": row.get("actual_records"),
            "actual_cells": row.get("actual_cells"),
            "claim_status": row.get("claim_status"),
            "claim_violations": json.loads(row.get("claim_violations_json") or "[]"),
            "index_digest": row.get("index_digest"),
        },
    }


def _endpoint_request(endpoint: str, api_key: str | None, timeout: float, ua: str = "browser"):
    """构造探测请求。

    UA 策略（2026-11-07 FRED 案例教训）：部分源（如 fred.stlouisfed.org 的
    fredgraph.csv）对非标准 UA 直接断连。默认用 browser UA（接受面最广），
    失败时由 probe_endpoint 用 curl UA 重试一次，并在 note 里记录命中的 UA。
    """
    parsed = urllib.parse.urlparse(endpoint)
    headers = {
        "User-Agent": _PROBE_UAS.get(ua, _PROBE_UAS["browser"]),
        "Accept": "application/json, text/csv, text/plain, */*",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key
    return urllib.request.Request(endpoint, headers=headers, method="GET"), parsed


_PROBE_UAS = {
    "browser": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "curl": "curl/8.0.1",
}


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def probe_endpoint(
    endpoint: str,
    api_key: str | None = None,
    timeout: float = 4.0,
    allow_private: bool = False,
) -> tuple[str, str, int]:
    """Return (upstream_status, note, http_status). Never raises.

    UA 回退：browser UA 失败时用 curl UA 重试一次；重试成功则在 note 标注
    「(UA=curl)」——这本身就是给 agent 的取数情报（该源对 UA 敏感）。
    """
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme in ("file", ""):
        return "local", "本地文件快照，无远端上游", 0
    if parsed.scheme not in ("http", "https"):
        return "skipped", f"不支持的协议 {parsed.scheme}", 0
    if not allow_private and not _is_public_host(parsed.hostname or ""):
        return "skipped", "内网/本地地址已按安全策略跳过探测", 0

    def _once(ua: str) -> tuple[str, str, int]:
        try:
            req, _ = _endpoint_request(endpoint, api_key, timeout, ua=ua)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(2048)
                return "reachable", f"HTTP {resp.status}", resp.status
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return "auth_failed", f"HTTP {exc.code} 凭证未通过", exc.code
            return "unreachable", f"HTTP {exc.code}", exc.code
        except Exception as exc:  # noqa: BLE001 - DNS / TLS / timeout
            return "unreachable", f"{type(exc).__name__}: {exc}", 0

    code, note, status = _once("browser")
    if code == "reachable":
        return code, note, status
    code2, note2, status2 = _once("curl")
    if code2 == "reachable":
        return code2, f"{note2} (UA=curl)", status2
    return code, note, status


def check_dataset(
    src: dict,
    row: dict | None,
    api_key: str | None = None,
    timeout: float = 4.0,
    allow_private: bool = False,
) -> dict:
    cat = catalog_meta(src)
    local = _local_checks(src, row)
    upstream, note, http_status = probe_endpoint(
        cat["endpoint"], api_key, timeout, allow_private
    )
    status, label = service_status(local, cat["enabled"])
    return {
        "ok": True,
        "dataset_id": src["dataset_id"],
        "checked_at": now_iso(),
        "status": status,
        "status_label": label,
        "upstream_status": upstream,
        "upstream_label": _upstream_label(upstream),
        "upstream_note": note,
        "http_status": http_status,
        "checks": local,
        "error": "" if upstream in ("reachable", "local", "skipped") else note,
        "credential_configured": bool(api_key),
    }
