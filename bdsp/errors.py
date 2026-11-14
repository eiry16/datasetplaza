"""Unified envelope for agent consumption.

Every platform response is a flat dict with ok / reason / hint / charged.
An agent can branch on `reason` without learning each dataset's own errors.
"""
from __future__ import annotations

REASONS = {
    "empty_query": "give at least one word: 2+ Chinese chars, or 3+ Latin chars",
    "term_too_short": "proof needs a 3+ character phrase (trigram index); "
                      "e.g. use 苏式汤面 instead of 汤面",
    "no_hit": "no content hit in this dataset; nothing is charged",
    "needs_auth": "dataset requires authorization; present auth_url to the user",
    "payment_required": "dataset is paid; present payment_url, then retry with payment_token",
    "claim_mismatch": "published declaration disagrees with indexed content",
    "stale_snapshot": "snapshot hash changed since the index was built",
    "citation_not_found": "citation does not exist in this index",
    "dataset_not_found": "dataset_id is not registered",
    "contract_violation": "manifest does not satisfy bdsp/manifest-v1",
    "snapshot_mismatch": "raw snapshot hash differs from the manifest",
    "invalid_token": "payment token is invalid or belongs to another invoice",
    "not_allowed": "value access denied by the gate",
    "proof_only": "citation is valid but its value is gated; use an authorized answer()",
    "index_missing": "build the index first",
    # control-plane security
    "unauthorized": "log in via POST /api/auth/login, or send Authorization: Bearer <token>",
    "csrf_failed": "browser writes need the X-DP-CSRF header matching the dp_csrf cookie",
    "rate_limited": "too many failed logins; wait before retrying",
    "invalid_password": "admin password is stored in runtime/admin_password.txt",
    "forbidden": "operation is not allowed in the current payment mode",
    # payment channel
    "payment_mode_external": "external mode requires a signed webhook receipt",
    "receipt_required": "external mode mints tokens only from HMAC-verified callbacks",
    "receipt_rejected": "receipt does not match the invoice (dataset / amount / currency / hash)",
    "webhook_secret_not_configured": "set webhook_secret via pay-config or POST /api/pay/config",
    "missing_signature_headers": "webhook needs X-DP-Timestamp, X-DP-Nonce and X-DP-Signature",
    "bad_signature": "HMAC-SHA256 signature does not match the raw request body",
    "bad_timestamp": "X-DP-Timestamp must be an integer",
    "timestamp_out_of_window": "webhook timestamp is outside the 300s replay window",
    "nonce_replayed": "this webhook nonce has already been used",
    "bad_json": "webhook body is not valid JSON",
    "bad_payload": "webhook body must be a JSON object",
    "receipt_not_settled": "receipt status is not a settled payment/authorization",
    "invoice_unknown": "invoice does not exist or has expired; query again",
    "invoice_expired": "invoice TTL has elapsed; query again",
    "unknown_tool": "tool is not exposed by this MCP server",
}


def fail(reason: str, **extra) -> dict:
    out = {
        "ok": False,
        "reason": reason,
        "hint": REASONS.get(reason, ""),
        "charged": 0.0,
    }
    out.update(extra)
    return out


def ok(**fields) -> dict:
    out = {"ok": True, "charged": 0.0}
    out.update(fields)
    return out
