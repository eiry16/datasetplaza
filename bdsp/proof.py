"""Proof layer: answer the question "is it really in this dataset?".

A proof is FREE and is produced BEFORE any auth or payment step. It gives
the agent enough to decide, but for a paid dataset it carries truncated
snippets, not full values. Full values are only released by gate.py.
"""
from __future__ import annotations

import json
import sqlite3

from .errors import fail, ok
from .index import count_fts, query_fts_rows
from .text import fts_terms, match_expr, terms_of


def _matched_on(value: str, terms: list[str]) -> str:
    return "value" if any(t in value for t in terms) else "field_desc"


def zero_hit_bundle(con: sqlite3.Connection, query: str, ds, warnings: list[str]) -> dict:
    return {
        "query": query,
        "dataset_id": ds["dataset_id"],
        "hit_count": 0,
        "sampled": 0,
        "citations": [],
        "fields": [],
        "snippets": [],
        "snapshot_sha256": ds["sha256"],
        "index_digest": ds["index_digest"],
        "version": ds["version"],
        "claim_status": ds["claim_status"],
        "free": True,
        "charged": 0.0,
        "warnings": warnings,
    }


def build_proof(
    con: sqlite3.Connection, query: str, dataset_id: str, k: int = 5
) -> dict:
    """Return a verifiable evidence bundle, or no_hit with zero charge."""
    ds = con.execute(
        "SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)
    ).fetchone()
    if ds is None:
        return fail("dataset_not_found", dataset_id=dataset_id)

    terms, dropped = terms_of(query)
    warnings: list[str] = []
    if dropped:
        warnings.append("single-character terms ignored: " + ", ".join(dropped))
    # route 可以用字面扫描接住 2 字词，证明不行：证据必须与索引里的同一个
    # trigram 事实对得上，否则「可复验」就是空话。说清原因，别回一句
    # empty_query 让人以为是自己没给词。
    terms = fts_terms(terms)
    if not terms:
        return fail("term_too_short", warnings=warnings, query=query)

    match = match_expr(terms, "and")
    mode = "and"
    total = count_fts(con, match, dataset_id)
    if total == 0 and len(terms) > 1:
        match = match_expr(terms, "or")
        mode = "or"
        total = count_fts(con, match, dataset_id)
        warnings.append("no conjunctive hit; relaxed to OR")
    if total == 0:
        bundle = zero_hit_bundle(con, query, ds, warnings)
        return fail("no_hit", proof=bundle, dataset_id=dataset_id, warnings=warnings)

    rows = query_fts_rows(con, match, [dataset_id], k)
    ep = json.loads(ds["evidence_json"])
    limit = int(ep.get("max_proof_chars", 160))
    # A short numeric value is shorter than any sane truncation limit, so
    # character truncation alone would hand out the paid answer for free.
    # Non-open datasets get redacted snippets: existence is provable, the
    # value is not. The agent can still re-run the query against the pinned
    # snapshot to verify hit_count and citations.
    redacted = ds["access_level"] != "open"
    if redacted:
        warnings.append(
            "non-open dataset: snippets are redacted; full values are released after auth/payment"
        )
    citations, fields, snippets = [], [], []
    seen_fields: set[str] = set()
    matched_counts = {"value": 0, "field_desc": 0}
    for citation, _ds_id, score in rows:
        cell = con.execute(
            "SELECT * FROM cells WHERE citation = ?", (citation,)
        ).fetchone()
        if cell is None:
            continue
        citations.append(citation)
        matched = _matched_on(cell["value_text"], terms)
        matched_counts[matched] += 1
        if cell["column_name"] not in seen_fields:
            seen_fields.add(cell["column_name"])
            fields.append(
                {
                    "column": cell["column_name"],
                    "description": cell["field_desc"],
                    "unit": cell["unit"],
                    "type": cell["field_type"],
                    "matched_on": matched,
                }
            )
        text = cell["value_text"]
        if redacted:
            shown = (
                (text[:2] + f"…[redacted {len(text)} chars]")
                if len(text) > 2
                else f"[redacted {len(text)} chars]"
            )
            truncated = True
        else:
            shown = text[:limit]
            truncated = len(text) > limit
        snippets.append(
            {
                "citation": citation,
                "text": shown,
                "redacted": redacted,
                "truncated": truncated,
                "full_chars": len(text),
                "matched_on": matched,
                "score": score,
            }
        )
    if matched_counts["value"] == 0:
        warnings.append(
            "all sampled hits matched field definitions, not values; "
            "add a year/entity keyword to pin down a concrete answer"
        )

    proof = {
        "query": query,
        "dataset_id": dataset_id,
        "hit_count": total,
        "sampled": len(snippets),
        "match_mode": mode,
        "citations": citations,
        "fields": fields,
        "snippets": snippets,
        "snapshot_sha256": ds["sha256"],
        "index_digest": ds["index_digest"],
        "version": ds["version"],
        "claim_status": ds["claim_status"],
        "claim_violations": json.loads(ds["claim_violations_json"]),
        "matched_on_summary": matched_counts,
        "verification": {
            "method": "re-run this query against the pinned snapshot; hit_count and citations must match",
            "query": query,
            "snapshot_sha256": ds["sha256"],
            "index_digest": ds["index_digest"],
            "expected_hit_count": total,
            "redaction": "values hidden for non-open datasets" if redacted else "none",
        },
        "free": True,
        "charged": 0.0,
        "warnings": warnings,
    }
    return ok(proof=proof, dataset_id=dataset_id, hit_count=total)
