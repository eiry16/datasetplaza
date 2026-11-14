"""datasetplazz: command line for the agent-facing dataset platform."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

from . import payments as paymod
from .api import Platform
from .contract import describe, iter_sources

ROOT = Path(__file__).resolve().parents[1]


def emit(payload, as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)


def platform(args) -> Platform:
    return Platform(sources=args.sources, db=args.db, runtime=getattr(args, "runtime", None))


def proof_lines(proof: dict | None) -> list[str]:
    if not proof:
        return ["  proof  : (none)"]
    out = [
        f"  proof  : hit_count={proof['hit_count']} sampled={proof['sampled']} "
        f"free={proof['free']} charged={proof['charged']}",
        f"           snapshot={proof['snapshot_sha256'][:16]}... "
        f"claim={proof['claim_status']}",
    ]
    for s in proof["snippets"][:3]:
        flag = "[redacted]" if s.get("redacted") else ("..." if s["truncated"] else "")
        out.append(f"           [{s['matched_on']}] {s['citation']}: {s['text']}{flag}")
    for w in proof.get("warnings", []):
        out.append(f"  warning: {w}")
    return out


def cmd_build(args) -> None:
    with platform(args) as p:
        result = p.build()
    emit(
        result,
        args.json,
        [
            f"built {result['datasets']} datasets / {result['cells']} cells "
            f"in {result['build_ms']} ms",
            f"index_digest={result['index_digest'][:16]}...",
            f"claim_mismatches={result['claim_mismatches']}",
        ],
    )


def cmd_list(args) -> None:
    payload = [describe(s) for s in iter_sources(args.sources)]
    emit(
        payload,
        args.json,
        [
            f"{d['dataset_id']:20s} {d['access_level']:11s} "
            f"{d['publisher_level']:13s} records={d['declared_records']:<5} "
            f"{d['version']}"
            for d in payload
        ]
        or ["(no registered datasets)"],
    )


def cmd_fields(args) -> None:
    with platform(args) as p:
        rows = p.datasets()
    payload = []
    for row in rows:
        for f in json.loads(row["fields_json"]):
            payload.append(
                {
                    "dataset_id": row["dataset_id"],
                    "column": f["name"],
                    "type": f.get("type", ""),
                    "description": f.get("description", ""),
                    "unit": f.get("unit", ""),
                }
            )
    emit(
        payload,
        args.json,
        [
            f"{x['dataset_id']:20s} {x['column']:22s} {x['unit']:14s} {x['description']}"
            for x in payload
        ],
    )


def cmd_route(args) -> None:
    with platform(args) as p:
        result = p.route(args.query, args.top_n)
    lines = []
    if not result["ok"]:
        lines.append(f"ok=false reason={result['reason']}")
    else:
        lines.append(
            f"mode={result['match_mode']} "
            f"{result['datasets_with_content_hits']}/{result['all_datasets']} "
            "datasets have content hits (free, charged=0)"
        )
        for c in result["candidates"]:
            lines.append(
                f"  {c['score']:7.2f} {c['dataset_id']:20s} "
                f"hits={c['hit_count']:<4} access={c['access_level']:11s} "
                f"claim={c['claim_status']:15s} fields={','.join(c['top_fields'][:3])}"
            )
        for w in result["warnings"]:
            lines.append(f"  warning: {w}")
    emit(result, args.json, lines)


def cmd_prove(args) -> None:
    with platform(args) as p:
        result = p.prove(args.query, args.dataset, args.k)
    lines = [f"ok={result['ok']} reason={result.get('reason', 'ok')} "
             f"charged={result['charged']}"]
    lines += proof_lines(result.get("proof"))
    emit(result, args.json, lines)


def cmd_answer(args) -> None:
    with platform(args) as p:
        result = p.answer(
            args.query,
            dataset_id=args.dataset,
            payment_token=args.token,
            top_n=args.top_n,
            k=args.k,
        )
    lines = [
        f"stage={result.get('stage', '-')} ok={result['ok']} "
        f"reason={result.get('reason', 'ok')} charged={result['charged']}",
    ]
    if result.get("invoice"):
        lines.append(f"  invoice : {result['invoice']['invoice_id']}")
    if result.get("payment_url"):
        lines.append(f"  payment : {result['payment_url']}  price={result.get('price')}")
    if result.get("auth_url"):
        lines.append(f"  auth    : {result['auth_url']}")
    lines += proof_lines(result.get("proof"))
    for v in result.get("values", [])[:3]:
        lines.append(
            f"  value  : {v['citation']} = {v['value'][:90]!r} [{v['unit']}]"
        )
    for c in result.get("citations", [])[:3]:
        lines.append(f"  citation: {c}")
    emit(result, args.json, lines)


def cmd_grant(args) -> None:
    with platform(args) as p:
        token = p.grant(args.invoice_id, args.dataset)
    emit(
        {"ok": True, "token": token, "dataset_id": args.dataset,
         "invoice_id": args.invoice_id},
        args.json,
        [f"token={token}", "simulated payment/authorization callback; pass via --token"],
    )


def cmd_verify(args) -> None:
    with platform(args) as p:
        result = p.verify(args.citation, args.token)
    if not result["ok"]:
        lines = [f"ok=false reason={result['reason']}"]
        if result["reason"] == "proof_only":
            lines += [
                f"  exists : {result['exists']} (citation is real, value is gated)",
                f"  column : {result['column']} ({result['field_desc']})",
                f"  snapshot: {result['snapshot_sha256']}",
                "  hint   : use an authorized answer() with --token to read the value",
            ]
    else:
        ds = result["dataset"]
        lines = [
            f"citation : {result['citation']}",
            f"value    : {result['value'][:200]}",
            f"column   : {result['column']} ({result['field_desc']})",
            f"source   : {ds['publisher']} / {ds['source_url']}",
            f"snapshot : {result['snapshot_sha256']}",
        ]
    emit(result, args.json, lines)


def cmd_claims(args) -> None:
    with platform(args) as p:
        result = p.claims(args.dataset)
    lines = [f"honesty_rate={result['honesty_rate']}"]
    for i in result["items"]:
        lines.append(
            f"  {i['dataset_id']:20s} {i['claim_status']:15s} "
            f"declared={i['declared_records']} actual={i['actual_records']}"
        )
        for v in i["violations"]:
            lines.append(f"      ! {v}")
    emit(result, args.json, lines)


def cmd_audit(args) -> None:
    with platform(args) as p:
        rows = p.audit_log(args.limit)
    emit(
        rows,
        args.json,
        [
            f"{r['id']:>3} {r['action']:7s} {r['reason']:18s} "
            f"charged={r['charged']:<6} {r['dataset_id']:20s} {r['query'][:32]}"
            for r in rows
        ],
    )


def cmd_stats(args) -> None:
    with platform(args) as p:
        result = p.stats()
    emit(
        result,
        args.json,
        [
            f"{result['datasets']} datasets / {result['cells']} cells / "
            f"{result['audit_rows']} audit rows / charged={result['total_charged']}"
        ],
    )


def cmd_admin(args) -> None:
    from .admin import ConsoleSession, serve

    console = None
    if getattr(args, "console_token", ""):
        console = ConsoleSession(
            args.console_token,
            idle_seconds=args.console_idle,
            boot_grace_seconds=args.console_boot_grace,
        )
        print(f"控制台联动已启用：会话 {console.token[:8]}，"
              f"关闭全部控制台页面后自动退出。")
    else:
        print("Dataset Plazz 控制后台启动中；Ctrl+C 停止。")
    serve(args.host, args.port, args.sources, args.db, args.runtime,
          auth=args.auth, password=args.password, https=args.https,
          cert=args.cert or None, key=args.key or None, listings=args.listings,
          console=console)


def cmd_pay_config(args) -> None:
    gw = paymod.PaymentGateway(runtime=args.runtime)
    if args.show:
        cfg = gw.public_config()
        emit(cfg, args.json, [
            f"mode={cfg['mode']} provider={cfg['provider']} sandbox={cfg['sandbox']}",
            f"webhook={cfg['webhook_url']} secret_configured={cfg['webhook_secret_configured']}",
        ])
        return
    body = {}
    if args.mode:
        body["mode"] = args.mode
    if args.checkout_url is not None:
        body["checkout_url_template"] = args.checkout_url
    if args.provider is not None:
        body["provider"] = args.provider
    if args.secret is not None:
        secret = sys.stdin.readline().strip() if args.secret == "-" else args.secret
        body["webhook_secret"] = secret
    cfg = gw.update(body)
    emit(cfg, args.json, [
        f"已写入 {gw.path}",
        f"mode={cfg['mode']} provider={cfg['provider']} "
        f"secret_configured={cfg['webhook_secret_configured']}",
    ])


def cmd_pay_sign(args) -> None:
    """Produce a signed webhook body for testing an external payment gateway."""
    gw = paymod.PaymentGateway(runtime=args.runtime)
    secret = args.secret
    if secret == "-":
        secret = sys.stdin.readline().strip()
    secret = secret or gw.secret
    if not secret:
        raise ValueError("未提供 webhook_secret；用 pay-config --secret 或 --secret 指定")
    payload = {
        "invoice_id": args.invoice_id,
        "dataset_id": args.dataset_id,
        "query": args.query,
        "amount": args.amount,
        "currency": args.currency,
        "status": "paid",
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ts, nonce = int(time.time()), secrets.token_hex(8)
    headers = {
        "X-DP-Timestamp": str(ts),
        "X-DP-Nonce": nonce,
        "X-DP-Signature": gw.sign(raw, ts, nonce, secret),
        "Content-Type": "application/json",
    }
    out = {
        "ok": True, "url": gw.webhook_url, "headers": headers,
        "body": payload, "raw_body": raw.decode("utf-8"),
        "curl": "curl -X POST " + gw.webhook_url + " "
                + " ".join(f"-H '{k}: {v}'" for k, v in headers.items())
                + f" -d '{raw.decode('utf-8')}'",
    }
    emit(out, args.json, [out["curl"]])


def cmd_mcp(args) -> None:
    from .mcp_server import run

    run(sources=args.sources, db=args.db, runtime=args.runtime)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="emit JSON")
    common.add_argument("--db", default=str(ROOT / "index.sqlite3"))
    common.add_argument("--sources", default=str(ROOT / "sources"))
    common.add_argument("--runtime", default=str(ROOT / "runtime"))

    parser = argparse.ArgumentParser(
        prog="datasetplazz", description="Dataset Plazz · agent-facing search over opt-in datasets"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", parents=[common], help="rebuild index + audit claims").set_defaults(fn=cmd_build)
    sub.add_parser("list", parents=[common], help="dataset directory").set_defaults(fn=cmd_list)
    sub.add_parser("fields", parents=[common], help="field dictionary").set_defaults(fn=cmd_fields)
    sub.add_parser("stats", parents=[common], help="index counters").set_defaults(fn=cmd_stats)

    p = sub.add_parser("route", parents=[common], help="deterministic cross-dataset routing")
    p.add_argument("query")
    p.add_argument("--top-n", type=int, default=3)
    p.set_defaults(fn=cmd_route)

    p = sub.add_parser("prove", parents=[common], help="free evidence bundle")
    p.add_argument("query")
    p.add_argument("--dataset", required=True)
    p.add_argument("--k", type=int, default=5)
    p.set_defaults(fn=cmd_prove)

    p = sub.add_parser("answer", parents=[common], help="full loop: route->prove->gate->value")
    p.add_argument("query")
    p.add_argument("--dataset")
    p.add_argument("--token")
    p.add_argument("--top-n", type=int, default=3)
    p.add_argument("--k", type=int, default=5)
    p.set_defaults(fn=cmd_answer)

    p = sub.add_parser("grant", parents=[common], help="simulate a completed payment")
    p.add_argument("invoice_id")
    p.add_argument("--dataset", required=True)
    p.set_defaults(fn=cmd_grant)

    p = sub.add_parser("verify", parents=[common], help="resolve one citation")
    p.add_argument("citation")
    p.add_argument("--token", help="authorization token for gated datasets")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("claims", parents=[common], help="declaration honesty audit")
    p.add_argument("--dataset")
    p.set_defaults(fn=cmd_claims)

    p = sub.add_parser("audit", parents=[common], help="runtime ledger")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("admin", help="launch the control console (HTTP/HTTPS + UI)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--db", default=str(ROOT / "index.sqlite3"))
    p.add_argument("--sources", default=str(ROOT / "sources"))
    p.add_argument("--runtime", default=str(ROOT / "runtime"))
    p.add_argument("--listings", default=str(ROOT / "listings" / "listings.json"))
    p.add_argument("--no-auth", dest="auth", action="store_false", default=True)
    p.add_argument("--password", default=None)
    p.add_argument("--https", action="store_true")
    p.add_argument("--cert", default="", help="CA 证书 PEM（默认自动生成自签证书）")
    p.add_argument("--key", default="", help="私钥 PEM")
    p.add_argument("--console-token", default="",
                   help="绑定控制台会话（16-64 位小写十六进制）：页面心跳停止后后端自行退出")
    p.add_argument("--console-idle", type=float, default=20.0,
                   help="心跳最长静默秒数（默认 20）")
    p.add_argument("--console-boot-grace", type=float, default=120.0,
                   help="控制台从未打开时的退出宽限秒数（默认 120）")
    p.set_defaults(fn=cmd_admin)

    p = sub.add_parser("pay-config", parents=[common], help="查看/切换支付通道配置")
    p.add_argument("--show", action="store_true", help="只显示当前配置（不写文件）")
    p.add_argument("--mode", choices=["sandbox", "external"])
    p.add_argument("--checkout-url", default=None, help="external 收银台模板，必须含 {invoice_id}")
    p.add_argument("--provider", default=None, help="支付服务商显示名")
    p.add_argument("--secret", default=None, help="webhook HMAC 密钥；传 - 则从 stdin 读")
    p.set_defaults(fn=cmd_pay_config)

    p = sub.add_parser("pay-sign", parents=[common], help="生成签名 webhook 回执（对接测试用）")
    p.add_argument("invoice_id")
    p.add_argument("--dataset", dest="dataset_id", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--amount", type=float, required=True)
    p.add_argument("--currency", default="CNY")
    p.add_argument("--secret", default=None)
    p.set_defaults(fn=cmd_pay_sign)

    p = sub.add_parser("mcp", parents=[common], help="启动 agent 侧 MCP stdio server")
    p.set_defaults(fn=cmd_mcp)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {"ok": False, "reason": "error", "detail": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
