"""Platform facade: the whole agent-facing loop in one object.

    answer(query)  -> route -> free proof -> gate -> value (or auth/pay link)

No step before `gate=allowed` can produce a charge, and every step is
written to the audit ledger. The agent never needs to know a dataset's own
protocol, only the unified reason codes in errors.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .contract import describe, iter_sources
from .errors import fail, ok
from .gate import Gate
from .index import build, open_db, write_audit
from .payments import PaymentGateway
from .proof import build_proof
from .router import route

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = ROOT / "sources"
DEFAULT_DB = ROOT / "index.sqlite3"
DEFAULT_RUNTIME = ROOT / "runtime"
DEFAULT_BASE_URL = "http://127.0.0.1:8787"

# ── 快照通道（平台代取）总开关，默认关闭 ───────────────────────────────
# 0.27 起平台默认只指路：不代发请求、不落快照、不返回值、不代收款。
# 下面的 Platform.route/prove/answer/verify/grant 是 0.20 之前那条产品面的
# 实现，代码保留是为了可复核，也为了将来真有源方书面委托平台代取并代收
# 费用时能一处打开。开关关闭时，MCP 面不暴露下列工具，控制台也不再接受
# 新的「一键接入」。打开它之前必须先解决三件事：委托与再分发授权、计价与
# 结算口径、精度责任归属——缺任何一件，平台就会重新变成数据代理。
SNAPSHOT_CHANNEL = False
SNAPSHOT_CHANNEL_NOTE = (
    "平台不代取：只指路到源方，调用与清洗由 agent 与源方直接发生，平台只记账。"
)


class Platform:
    def __init__(self, sources=None, db=None, runtime=None, payments=None, base_url=None):
        self.sources = Path(sources or DEFAULT_SOURCES)
        self.db_path = Path(db or DEFAULT_DB)
        self.runtime = Path(runtime or DEFAULT_RUNTIME)
        self.payments = payments or PaymentGateway(
            runtime=self.runtime, base_url=base_url or DEFAULT_BASE_URL
        )
        self.gate = Gate(payments=self.payments, runtime=self.runtime)
        self._con = None

    def set_base_url(self, base_url: str) -> None:
        self.payments.base_url = str(base_url).rstrip("/")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    @property
    def con(self):
        if self._con is None:
            if not self.db_path.exists():
                raise FileNotFoundError(f"index not built: {self.db_path}")
            self._con = open_db(self.db_path)
        return self._con

    def build(self) -> dict:
        self.close()
        # `catalog.enabled=false` means "delisted": keep the manifest on disk
        # (so the console can show/re-enable it) but keep it out of the agent's
        # searchable index.
        sources = [
            s for s in iter_sources(self.sources)
            if (s.get("catalog") or {}).get("enabled", True)
        ]
        return build(self.db_path, sources)

    def datasets(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT d.*, (SELECT count(*) FROM cells c "
            "WHERE c.dataset_id = d.dataset_id) AS cells FROM datasets d "
            "ORDER BY d.dataset_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def route(self, query: str, top_n: int = 3) -> dict:
        result = route(self.con, query, top_n)
        write_audit(
            self.con,
            "route",
            query,
            "",
            "ok" if result["ok"] else result["reason"],
            0.0,
            {"candidates": [c["dataset_id"] for c in result.get("candidates", [])]},
        )
        return result

    def log_points(self, query: str, dataset_ids, via: str = "discover") -> int:
        """把「指路」写进账本：平台没收下这个问题，只是把它指到了外部源。

        为什么必须记：货架条目不参与 route/proof，指路是它们**唯一**可能
        产生的事件。不记账，它们的热度就结构性地恒为 0，显示上又被读成
        「没人要」——把记账缺口说成了市场结论（2026-11-08 修正）。

        ``via`` 只进 detail，不进热度：discover（问到就指路）/ admin（同一个
        入口从后台来）/ catalog（带条件翻货架，如「有没有商业数据」）/ card
        （agent 走到那块路牌前看细节）。不带条件的全量浏览不算问路，不记。
        操作员在后台点「演练」也不记——自己造的流量记进去，等于把「没人问」
        包装成「有人问」，那比显示 0 更坏。写入失败不抛：这是旁路记账，
        不能因为账本锁住就让一次问路失败。
        """
        ids = [d for d in dict.fromkeys(dataset_ids or []) if d]
        if not ids:
            return 0
        try:
            con = self.con
            con.execute("PRAGMA busy_timeout=3000")
            write_audit(con, "point", query, "", "ok", 0.0,
                        {"candidates": ids, "via": via})
        except Exception:  # noqa: BLE001 - 账本不可写时不能拖垮查询
            return 0
        return len(ids)

    def log_touch(self, via: str = "mcp") -> bool:
        """记一条「曝光」：有调用方连上了，但还没提任何数据需求。

        为什么和 point 分开：路牌生意本来就是两个数——曝光量（有多少人
        路过、抬头看了一眼）和到店量（有多少人真的开口问）。混成一个数，
        扫描器就会把排行榜刷成假象；一点都不记，又会丢掉「被多少人看到」
        这个真实信息（2026-11-08 主人提出的点）。

        所以：``touch`` 进账本，但**不进热度分**（``heat.WEIGHTS`` 里没有
        它），且**按客户端每天去重**——一个每分钟轮询的扫描器不该比一个
        认真问了三次的 agent 更"热"。身份来自 ``bdsp.ctx``，本地 stdio
        调用（client 为空）不记：那是我们自己。
        """
        from . import ctx as _ctx

        client, _ = _ctx.current()
        if not client:
            return False
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            con = self.con
            con.execute("PRAGMA busy_timeout=3000")
            seen = con.execute(
                "SELECT 1 FROM audit WHERE action = 'touch' AND client_id = ?"
                " AND substr(ts,1,10) = ? LIMIT 1", (client, day)).fetchone()
            if seen:
                return False
            write_audit(con, "touch", "", "", "ok", 0.0,
                        {"via": via}, client=client, intent="handshake")
        except Exception:  # noqa: BLE001 - 旁路记账不能拖垮握手
            return False
        return True

    def prove(self, query: str, dataset_id: str, k: int = 5) -> dict:
        result = build_proof(self.con, query, dataset_id, k)
        write_audit(
            self.con,
            "prove",
            query,
            dataset_id,
            "ok" if result["ok"] else result["reason"],
            0.0,
            {"hit_count": result.get("hit_count", 0)},
        )
        return result

    def grant(self, invoice_id: str, dataset_id: str, receipt: dict | None = None) -> str:
        return self.gate.grant(invoice_id, dataset_id, receipt=receipt, con=self.con)

    def answer(
        self,
        query: str,
        dataset_id: str | None = None,
        payment_token: str | None = None,
        top_n: int = 3,
        k: int = 5,
    ) -> dict:
        routing = self.route(query, top_n)
        if not routing["ok"]:
            return fail(routing["reason"], stage="route", route=routing, proof=None)

        chosen = dataset_id
        if chosen is None:
            with_hits = [c for c in routing["candidates"] if c["has_content_hit"]]
            if not with_hits:
                write_audit(self.con, "gate", query, "", "no_hit", 0.0, {})
                return fail("no_hit", stage="route", route=routing, proof=None)
            chosen = with_hits[0]["dataset_id"]

        proved = self.prove(query, chosen, k)
        if not proved["ok"]:
            return fail(
                proved["reason"],
                stage="prove",
                route=routing,
                proof=proved.get("proof"),
                dataset_id=chosen,
            )
        proof = proved["proof"]

        ds = self.con.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?", (chosen,)
        ).fetchone()
        gated = self.gate.check(self.con, ds, proof, payment_token)
        write_audit(
            self.con,
            "gate",
            query,
            chosen,
            "ok" if gated["ok"] else gated["reason"],
            0.0,  # charge is recorded once, by the fetch step
            {"access_level": ds["access_level"]},
        )
        if not gated["ok"]:
            return fail(
                gated["reason"],
                stage="gate",
                route=routing,
                proof=proof,
                gate=gated,
                dataset_id=chosen,
                invoice=gated.get("invoice"),
                payment_url=gated.get("payment_url"),
                auth_url=gated.get("auth_url"),
                price=gated.get("price"),
            )

        values = self._fetch(proof["citations"])
        charged = gated.get("charged", 0.0)
        write_audit(
            self.con,
            "fetch",
            query,
            chosen,
            "ok",
            charged,
            {"citations": proof["citations"]},
        )
        return ok(
            stage="fetch",
            route=routing,
            proof=proof,
            gate=gated,
            values=values,
            citations=proof["citations"],
            charged=charged,
        )

    def _fetch(self, citations: list[str]) -> list[dict]:
        out = []
        for citation in citations:
            row = self.con.execute(
                "SELECT * FROM cells WHERE citation = ?", (citation,)
            ).fetchone()
            if row is None:
                continue
            out.append(
                {
                    "citation": row["citation"],
                    "value": row["value_text"],
                    "column": row["column_name"],
                    "field_desc": row["field_desc"],
                    "unit": row["unit"],
                    "version": row["version"],
                    "snapshot_sha256": row["source_sha"],
                    "row_key": row["row_key"],
                    "dataset_id": row["dataset_id"],
                }
            )
        return out

    def verify(self, citation: str, payment_token: str | None = None) -> dict:
        row = self.con.execute(
            "SELECT * FROM cells WHERE citation = ?", (citation,)
        ).fetchone()
        if row is None:
            return fail("citation_not_found", citation=citation)
        ds = self.con.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?", (row["dataset_id"],)
        ).fetchone()
        # Citations are public (they are part of the free proof), so verify()
        # must not become a back door around the paid gate. For a gated
        # dataset, an unauthenticated caller still gets existence + provenance,
        # which is exactly what "is the answer really there?" needs.
        if ds and ds["access_level"] != "open" and not self.gate.is_authorized(
            payment_token, row["dataset_id"]
        ):
            return fail(
                "proof_only",
                citation=citation,
                exists=True,
                dataset_id=row["dataset_id"],
                column=row["column_name"],
                field_desc=row["field_desc"],
                unit=row["unit"],
                version=row["version"],
                snapshot_sha256=row["source_sha"],
                index_digest=ds["index_digest"],
            )
        return ok(
            citation=citation,
            value=row["value_text"],
            column=row["column_name"],
            field_desc=row["field_desc"],
            unit=row["unit"],
            version=row["version"],
            snapshot_sha256=row["source_sha"],
            index_digest=ds["index_digest"] if ds else "",
            dataset={
                "dataset_id": row["dataset_id"],
                "name": ds["name"] if ds else "",
                "publisher": ds["publisher"] if ds else "",
                "publisher_level": ds["publisher_level"] if ds else "",
                "source_url": ds["source_url"] if ds else "",
            },
        )

    def claims(self, dataset_id: str | None = None) -> dict:
        sql = (
            "SELECT dataset_id, claim_status, claim_violations_json, "
            "actual_records, coverage_json FROM datasets"
        )
        params: tuple = ()
        if dataset_id:
            sql += " WHERE dataset_id = ?"
            params = (dataset_id,)
        rows = self.con.execute(sql, params).fetchall()
        items = [
            {
                "dataset_id": r["dataset_id"],
                "claim_status": r["claim_status"],
                "violations": json.loads(r["claim_violations_json"]),
                "declared_records": json.loads(r["coverage_json"]).get(
                    "declared_records"
                ),
                "actual_records": r["actual_records"],
            }
            for r in rows
        ]
        honest = sum(1 for i in items if i["claim_status"] == "consistent")
        return ok(
            items=items,
            honesty_rate=round(honest / len(items), 4) if items else 0.0,
        )

    def audit_log(self, limit: int = 20) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "action": r["action"],
                "query": r["query"],
                "dataset_id": r["dataset_id"],
                "reason": r["reason"],
                "charged": r["charged"],
                "detail": json.loads(r["detail_json"]),
            }
            for r in rows
        ]

    def audit_summary(self, days: int = 7) -> dict:
        """Aggregates only — this is what a stranger may see of the ledger.

        ``audit_log`` returns ``query`` verbatim. That is fine on an operator
        console and fatal on a public endpoint: every caller would be able to
        read what every other caller asked for. The public view therefore
        drops the individual rows entirely and keeps four aggregates —
        counts by action, by intent, by day, and how many distinct clients
        there were.
        """
        con = self.con
        span = max(1, min(int(days or 7), 90))
        where = "substr(ts,1,10) >= date('now', ?)"
        since = f"-{span} days"
        total = con.execute(
            f"SELECT count(*) FROM audit WHERE {where}", (since,)).fetchone()[0]
        by_action = {
            r[0]: r[1] for r in con.execute(
                f"SELECT action, count(*) FROM audit WHERE {where}"
                " GROUP BY 1 ORDER BY 2 DESC", (since,)).fetchall()
        }
        by_intent = {
            r[0]: r[1] for r in con.execute(
                f"SELECT intent, count(*) FROM audit WHERE {where}"
                " GROUP BY 1 ORDER BY 2 DESC", (since,)).fetchall()
        }
        by_day = [
            {"day": r[0], "events": r[1]} for r in con.execute(
                f"SELECT substr(ts,1,10) AS d, count(*) FROM audit WHERE {where}"
                " GROUP BY 1 ORDER BY 1", (since,)).fetchall()
        ]
        clients = con.execute(
            f"SELECT count(DISTINCT client_id) FROM audit"
            f" WHERE {where} AND client_id <> ''", (since,)).fetchone()[0]
        return ok(
            window_days=span,
            total=total,
            by_action=by_action,
            by_intent=by_intent,
            by_day=by_day,
            distinct_clients=clients,
            note=("只给聚合计数。逐条账本含调用方原始问题，属客户意图，"
                  "不对公网开放；需要明细请在后台「审计账本」页查看。"),
        )

    def stats(self) -> dict:
        con = self.con
        return ok(
            datasets=con.execute("SELECT count(*) FROM datasets").fetchone()[0],
            cells=con.execute("SELECT count(*) FROM cells").fetchone()[0],
            indexed=con.execute("SELECT count(*) FROM cells_fts").fetchone()[0],
            audit_rows=con.execute("SELECT count(*) FROM audit").fetchone()[0],
            total_charged=con.execute(
                "SELECT coalesce(sum(charged),0) FROM audit"
            ).fetchone()[0],
            payment_mode=self.payments.mode,
            payment_provider=self.payments.config["provider"],
            db=str(self.db_path),
        )
