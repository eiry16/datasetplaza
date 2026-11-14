"""Admission contract: how a dataset introduces itself to the platform.

A source.json is the whole opt-in agreement. Two rules are enforced here
rather than left as goodwill, because they are the product's core promise:

  * evidence_policy.proof_free        -> proof must be free, before payment
  * evidence_policy.no_hit_no_charge -> zero hits means zero charge

The contract also carries representative_queries (routing index keys),
coverage (which the platform re-measures), access (auth / price / links)
and commitment (snapshot hash, index digest written at build time).
"""
from __future__ import annotations

import json
from pathlib import Path

from .text import terms_of

SCHEMA = "bdsp/manifest-v1"

REQUIRED = (
    "schema",
    "dataset_id",
    "name",
    "publisher",
    "publisher_level",
    "domain",
    "modality",
    "update_frequency",
    "license",
    "version",
    "source_url",
    "retrieved_at",
    "sha256",
    "raw_file",
    "row_key",
    "fields",
    "ingest",
    "representative_queries",
    "coverage",
    "access",
    "evidence_policy",
    "commitment",
)

PUBLISHER_LEVELS = {
    "government",
    "international",
    "research",
    "market",
    "commercial",
    "project",
    "other",
}
ACCESS_LEVELS = {"open", "registered", "authorized", "paid"}
FORMATS = {"lines", "json", "csv"}


class ContractError(ValueError):
    pass


class SnapshotMismatch(RuntimeError):
    pass


def validate(src: dict) -> list[str]:
    """Return violations; empty list means the manifest is admissible."""
    errs: list[str] = []
    for key in REQUIRED:
        if key not in src or src[key] in (None, "", [], {}):
            errs.append(f"missing {key}")
    if src.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    if src.get("publisher_level") not in PUBLISHER_LEVELS:
        errs.append("publisher_level outside the authority vocabulary")

    fields = src.get("fields")
    if not isinstance(fields, list) or not fields:
        errs.append("fields must be a non-empty list")
    else:
        for f in fields:
            if not isinstance(f, dict) or not f.get("name"):
                errs.append("each field needs a name")
            elif not f.get("description"):
                errs.append(f"field {f['name']} needs a description")

    rqs = src.get("representative_queries")
    if not isinstance(rqs, list) or not (2 <= len(rqs) <= 5):
        errs.append("representative_queries must hold 2..5 sample questions")
    else:
        for q in rqs:
            if not isinstance(q, str) or len(q.strip()) < 3:
                errs.append("each representative query needs 3+ characters")
            elif not terms_of(q)[0]:
                errs.append(f"representative query has no searchable keyword: {q!r}")

    cov = src.get("coverage") or {}
    if not isinstance(cov.get("declared_records"), int) or cov["declared_records"] < 0:
        errs.append("coverage.declared_records must be a non-negative int")

    acc = src.get("access") or {}
    level = acc.get("level")
    price = acc.get("price") or {}
    if level not in ACCESS_LEVELS:
        errs.append("access.level outside the access vocabulary")
    if level == "paid":
        if not isinstance(price.get("amount"), (int, float)) or price["amount"] <= 0:
            errs.append("paid datasets must declare a positive price.amount")
        if not acc.get("payment_url"):
            errs.append("paid datasets must declare payment_url")
    elif price.get("amount", 0):
        errs.append("free datasets must not declare a positive price")
    if level in ("registered", "authorized") and not acc.get("auth_url"):
        errs.append(f"{level} datasets must declare auth_url")

    ep = src.get("evidence_policy") or {}
    if ep.get("proof_free") is not True:
        errs.append("evidence_policy.proof_free must be true (platform rule)")
    if ep.get("no_hit_no_charge") is not True:
        errs.append("evidence_policy.no_hit_no_charge must be true (platform rule)")
    if not isinstance(ep.get("max_proof_chars"), int) or ep["max_proof_chars"] < 40:
        errs.append("evidence_policy.max_proof_chars must be >= 40")

    com = src.get("commitment") or {}
    if com.get("snapshot_sha256") != src.get("sha256"):
        errs.append("commitment.snapshot_sha256 must equal the snapshot sha256")
    if len(src.get("sha256") or "") != 64:
        errs.append("sha256 must be a full hex digest")

    ing = src.get("ingest") or {}
    if ing.get("format") not in FORMATS:
        errs.append(f"ingest.format must be one of {sorted(FORMATS)}")
    if not isinstance(ing.get("row_key"), list) or not ing.get("row_key"):
        errs.append("ingest.row_key must be a non-empty list")
    if not isinstance(src.get("row_key"), list) or not src.get("row_key"):
        errs.append("row_key must be a non-empty list")
    return errs


def load(path: str | Path) -> dict:
    path = Path(path)
    src = json.loads(path.read_text(encoding="utf-8"))
    errs = validate(src)
    if errs:
        raise ContractError(f"{path}: " + "; ".join(errs))
    src["_dir"] = str(path.parent)
    return src


def iter_sources(root: str | Path) -> list[dict]:
    return [load(p) for p in sorted(Path(root).glob("*/source.json"))]


def describe(src: dict) -> dict:
    """Directory-level view: what a registry would show to an agent.

    `catalog` / `connection` are optional (added for the control console);
    manifests written before they existed still validate and still get a
    complete directory row here.
    """
    acc = src["access"]
    cat = src.get("catalog") or {}
    conn = src.get("connection") or {}
    return {
        "dataset_id": src["dataset_id"],
        "name": src["name"],
        "publisher": src["publisher"],
        "publisher_level": src["publisher_level"],
        "domain": src["domain"],
        "category": cat.get("category", "other"),
        "category_source": cat.get("category_source", "legacy"),
        "developer": cat.get("developer", {}),
        "access_level": acc["level"],
        "price": acc.get("price", {}),
        "version": src["version"],
        "representative_queries": src["representative_queries"],
        "declared_records": src["coverage"]["declared_records"],
        "fields": [f["name"] for f in src["fields"]],
        "fields_count": len(src["fields"]),
        "snapshot_sha256": src["sha256"],
        "endpoint": conn.get("endpoint", src.get("source_url", "")),
        "enabled": bool(cat.get("enabled", True)),
    }
