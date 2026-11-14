"""SQLite FTS5 (trigram) index over field-level cells, plus the registry.

The registry table keeps the *full* manifest, so routing, gating and claim
auditing never need to re-read source.json at query time. The audit table is
intentionally NOT dropped on rebuild: it is a runtime ledger, not index data.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .ingest import iter_cells, summarize
from .text import fts_terms, match_expr, terms_of

DDL = """
DROP TABLE IF EXISTS cells;
DROP TABLE IF EXISTS datasets;
DROP TABLE IF EXISTS cells_fts;
DROP TABLE IF EXISTS meta;
CREATE TABLE cells (
    citation     TEXT PRIMARY KEY,
    dataset_id   TEXT NOT NULL,
    version      TEXT NOT NULL,
    row_key      TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    value_text   TEXT NOT NULL,
    field_desc   TEXT NOT NULL,
    unit         TEXT NOT NULL,
    field_type   TEXT NOT NULL,
    source_sha   TEXT NOT NULL
);
CREATE TABLE datasets (
    dataset_id        TEXT PRIMARY KEY,
    version           TEXT NOT NULL,
    name              TEXT NOT NULL,
    publisher         TEXT NOT NULL,
    publisher_level   TEXT NOT NULL,
    source_url        TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    fields_json       TEXT NOT NULL,
    manifest_json     TEXT NOT NULL,
    access_level      TEXT NOT NULL,
    access_json       TEXT NOT NULL,
    coverage_json     TEXT NOT NULL,
    representative_json TEXT NOT NULL,
    evidence_json     TEXT NOT NULL,
    actual_records    INTEGER NOT NULL,
    actual_cells      INTEGER NOT NULL,
    fill_rates_json   TEXT NOT NULL,
    index_digest      TEXT NOT NULL,
    claim_status      TEXT NOT NULL,
    claim_violations_json TEXT NOT NULL
);
CREATE VIRTUAL TABLE cells_fts USING fts5(
    citation UNINDEXED,
    dataset_id UNINDEXED,
    body,
    tokenize = 'trigram'
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,
    query       TEXT NOT NULL,
    dataset_id  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    charged     REAL NOT NULL,
    detail_json TEXT NOT NULL,
    client_id   TEXT NOT NULL DEFAULT '',
    intent      TEXT NOT NULL DEFAULT 'query'
);
"""

# Columns added after the ledger had already been running. ``CREATE TABLE IF
# NOT EXISTS`` will not touch an existing table, so a live index keeps the old
# shape until we say so explicitly. Both are additive with defaults, so old
# rows stay valid: '' = 我们自己（stdio / CLI / 测试），'query' = 带问题来的。
_AUDIT_MIGRATIONS = (
    ("client_id", "TEXT NOT NULL DEFAULT ''"),
    ("intent", "TEXT NOT NULL DEFAULT 'query'"),
)


def open_db(db_path: str | Path) -> sqlite3.Connection:
    # The control console serves requests from a thread pool and serialises
    # them with a lock; the connection itself must tolerate that hand-off.
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    _migrate_audit(con)
    return con


def _migrate_audit(con: sqlite3.Connection) -> None:
    """Add the identity columns to a ledger that predates them.

    Read-mostly path: this runs on every open, so it costs one ``pragma`` and
    nothing else once the columns exist. Skipped entirely when the ``audit``
    table is absent (a fresh file before ``build``).
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(audit)").fetchall()}
    if not cols:
        return
    for name, decl in _AUDIT_MIGRATIONS:
        if name not in cols:
            con.execute(f"ALTER TABLE audit ADD COLUMN {name} {decl}")
    con.commit()


def query_fts_rows(
    con: sqlite3.Connection,
    match: str,
    dataset_ids: list[str] | None = None,
    k: int = 10,
) -> list[tuple[str, str, float]]:
    """Return (citation, dataset_id, positive_score), best first."""
    sql = "SELECT citation, dataset_id, bm25(cells_fts) AS s FROM cells_fts WHERE cells_fts MATCH ?"
    params: list = [match]
    if dataset_ids:
        sql += " AND dataset_id IN (%s)" % ",".join("?" * len(dataset_ids))
        params.extend(dataset_ids)
    sql += " ORDER BY s LIMIT ?"
    params.append(k)
    rows = con.execute(sql, params).fetchall()
    return [(r["citation"], r["dataset_id"], round(-r["s"], 4)) for r in rows]


def query_fts(con, match, dataset_ids=None, k=10) -> list[tuple[str, float]]:
    return [(c, s) for c, _d, s in query_fts_rows(con, match, dataset_ids, k)]


def count_fts(con: sqlite3.Connection, match: str, dataset_id: str) -> int:
    row = con.execute(
        "SELECT count(*) FROM cells_fts WHERE cells_fts MATCH ? AND dataset_id = ?",
        (match, dataset_id),
    ).fetchone()
    return int(row[0])


def _digest(cells) -> str:
    h = hashlib.sha256()
    for c in cells:
        h.update((c.citation + "\x00" + c.value + "\x00" + c.source_sha + "\n").encode("utf-8"))
    return h.hexdigest()


def audit_declarations(con: sqlite3.Connection) -> dict:
    """Verify each publisher's self-description against the built index.

    Two independent checks: declared record count, and whether every
    representative_queries example actually has a content hit. A declaration
    that cannot be reproduced is a claim_mismatch, not a rounding error.
    """
    report = {}
    for ds in con.execute("SELECT * FROM datasets").fetchall():
        ds_id = ds["dataset_id"]
        violations = list(json.loads(ds["claim_violations_json"]))
        manifest = json.loads(ds["manifest_json"])
        for q in manifest["representative_queries"]:
            terms, _ = terms_of(q)
            # Verifying a declaration is a trigram check (same index the
            # proof layer uses), so a 2-char-only query cannot be verified
            # here even though it is a legitimate term for routing.
            terms = fts_terms(terms)
            if not terms:
                violations.append(f"unsearchable representative query: {q!r}")
                continue
            hits = query_fts(con, match_expr(terms, "and"), [ds_id], 1)
            if not hits and len(terms) > 1:
                hits = query_fts(con, match_expr(terms, "or"), [ds_id], 1)
            if not hits:
                violations.append(f"representative query has no content hit: {q!r}")
        status = "claim_mismatch" if violations else "consistent"
        con.execute(
            "UPDATE datasets SET claim_status = ?, claim_violations_json = ? "
            "WHERE dataset_id = ?",
            (status, json.dumps(violations, ensure_ascii=False), ds_id),
        )
        report[ds_id] = {"status": status, "violations": violations}
    con.commit()
    return report


def build(db_path: str | Path, sources: list[dict]) -> dict:
    """Rebuild from pinned snapshots and re-audit every declaration."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        con.executescript(DDL)
        all_cells = []
        per_dataset = {}
        for src in sources:
            cells = iter_cells(src)
            stats = summarize(src, cells)
            all_cells.extend(cells)
            per_dataset[src["dataset_id"]] = (src, stats)

        digest = _digest(all_cells)
        for ds_id, (src, stats) in per_dataset.items():
            violations = []
            declared = src["coverage"]["declared_records"]
            if declared != stats["actual_records"]:
                violations.append(
                    f"declared {declared} records, indexed {stats['actual_records']}"
                )
            manifest = dict(src)
            manifest.pop("_dir", None)
            con.execute(
                "INSERT INTO datasets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ds_id,
                    str(src["version"]),
                    src["name"],
                    src["publisher"],
                    src["publisher_level"],
                    src["source_url"],
                    src["sha256"],
                    json.dumps(src["fields"], ensure_ascii=False),
                    json.dumps(manifest, ensure_ascii=False),
                    src["access"]["level"],
                    json.dumps(src["access"], ensure_ascii=False),
                    json.dumps(src["coverage"], ensure_ascii=False),
                    json.dumps(src["representative_queries"], ensure_ascii=False),
                    json.dumps(src["evidence_policy"], ensure_ascii=False),
                    stats["actual_records"],
                    stats["actual_cells"],
                    json.dumps(stats["fill_rates"], ensure_ascii=False),
                    digest,
                    "pending",
                    json.dumps(violations, ensure_ascii=False),
                ),
            )
        con.executemany(
            "INSERT OR REPLACE INTO cells VALUES (?,?,?,?,?,?,?,?,?,?)",
            [c.as_tuple() for c in all_cells],
        )
        con.executemany(
            "INSERT INTO cells_fts (citation, dataset_id, body) VALUES (?,?,?)",
            [
                # body 必须含**列名**。此前只放 value + field_desc，而 field_desc
                # 是「比率 / 覆盖率」这类模板套话，列名 "Under-five mortality
                # rate" 根本没进索引——于是 agent 问「儿童死亡率」时，值里只有
                # "Afghanistan"，内容命中恒为 0（2026-11-08 实测 mortality/killed）。
                # 列名是这张表唯一能表达"这列是什么"的字段，必须可检索。
                (c.citation, c.dataset_id,
                 " ".join(x for x in (c.column, c.value, c.field_desc) if x).strip())
                for c in all_cells
            ],
        )
        con.execute(
            "INSERT INTO meta VALUES (?,?)",
            ("index_digest", digest),
        )
        con.execute(
            "INSERT INTO meta VALUES (?,?)",
            ("built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        claims = audit_declarations(con)
        con.commit()
        con.execute("PRAGMA optimize")
    finally:
        con.close()
    return {
        "ok": True,
        "datasets": len(sources),
        "cells": len(all_cells),
        "index_digest": digest,
        "claims": claims,
        "claim_mismatches": [
            k for k, v in claims.items() if v["status"] != "consistent"
        ],
        "build_ms": round((time.perf_counter() - started) * 1000, 1),
        "db": str(db_path),
    }


def write_audit(
    con: sqlite3.Connection,
    action: str,
    query: str,
    dataset_id: str,
    reason: str,
    charged: float,
    detail: dict | None = None,
    client: str | None = None,
    intent: str | None = None,
) -> None:
    """Append one ledger row.

    ``client`` / ``intent`` default to the request context (see ``bdsp.ctx``),
    so the identity travels with the call without touching any facade
    signature. Explicit values win, which is what the gateway uses when it
    wants to tag a call the context cannot know about.
    """
    if client is None or intent is None:
        from . import ctx as _ctx  # local import: keep index import-only

        cur_client, cur_intent = _ctx.current()
        client = cur_client if client is None else client
        intent = cur_intent if intent is None else intent
    con.execute(
        "INSERT INTO audit (ts, action, query, dataset_id, reason, charged,"
        " detail_json, client_id, intent) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            action,
            query,
            dataset_id,
            reason,
            float(charged),
            json.dumps(detail or {}, ensure_ascii=False),
            str(client or ""),
            str(intent or "query"),
        ),
    )
    con.commit()
