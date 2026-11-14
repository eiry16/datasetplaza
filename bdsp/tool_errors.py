"""One vocabulary for every tool reply, so an agent can branch without prose.

Before this module an agent had to read ``reason`` (a free-form internal
string), ``shelf_gap`` / ``access_gap`` / ``not_held`` (three different
shapes), and the JSON-RPC ``isError`` flag (which only fires on exceptions).
That is four ways to learn "this call did not get you a road sign". The codes
below are a *projection* of those existing signals, not a new decision layer:
nothing here changes a score, a candidate or a judgement.

    OK                 the tool did what it says; read the payload
    NO_MATCH           nobody on the shelf can answer this. Do not rephrase
                       forever: a missing axis will not appear by rewording
    ACCESS_RESTRICTED  the road exists but has a source-side gate. Show the
                       link to the user; never pay or ask for credentials
    SOURCE_UNAVAILABLE the source is unreachable / its snapshot is stale.
                       Retrying later is reasonable; do not read it as "no data"
    INVALID_REQUEST    bad coordinates / unknown id / malformed argument.
                       Fix the call, not the question
    CHANNEL_OFF        a snapshot-era tool was called. Use dp_discover instead
    BUDGET_EXCEEDED    the request's declared budget cannot be met here
    TOOL_FAILED        anything else went wrong inside the platform

All codes are stable strings. ``retryable`` is advice about *this* failure,
not a promise about the upstream.
"""
from __future__ import annotations

CODES: dict[str, dict] = {
    "OK": {"label": "完成", "retryable": False,
           "next": "按工具说明读候选 / 交接单，取数由你直连源方"},
    "NO_MATCH": {"label": "货架无匹配", "retryable": False,
                 "next": "读 gap 看缺哪一轴；缺轴不会因换词出现，改走外部检索或 dp_hub_route"},
    "ACCESS_RESTRICTED": {"label": "源方闸门", "retryable": False,
                          "next": "把 buy_url / apply_at 给用户；平台不代付、不代取、不索账号"},
    "SOURCE_UNAVAILABLE": {"label": "源方暂不可用", "retryable": True,
                           "next": "不等于没有数据；稍后重试或换出口/UA，见 harvest.note"},
    "INVALID_REQUEST": {"label": "调用不合法", "retryable": False,
                        "next": "按 errors / hint 修参数或坐标；用 dp_map / dp_catalog 取合法值"},
    "CHANNEL_OFF": {"label": "通道已下线", "retryable": False,
                    "next": "改用 dp_discover → dp_usage → 直连源方；平台不持有内容"},
    "BUDGET_EXCEEDED": {"label": "预算约束不满足", "retryable": False,
                        "next": "放宽 budget_limit 或改找开放源；平台不代收费用"},
    "TOOL_FAILED": {"label": "平台内部失败", "retryable": True,
                    "next": "按 ref 让操作员查日志；不要把它当成货架没有数据"},
}

_INVALID = frozenset({
    "empty_query", "term_too_short", "invalid_need", "empty_need", "unknown_dataset",
    "dataset_not_found", "citation_not_found", "contract_violation", "bad_json",
    "bad_payload", "bad_signature", "bad_timestamp", "invalid_token",
    "missing_signature_headers", "receipt_rejected", "receipt_required",
    "invoice_unknown", "invoice_expired", "unknown_tool", "bad_stop_token",
    "bad_console_token", "csrf_failed", "rate_limited", "invalid_password",
    "unauthorized", "not_found", "invalid_request",
})
_ACCESS = frozenset({
    "needs_auth", "payment_required", "proof_only", "not_allowed", "forbidden",
    "snapshot_channel_off_gate", "budget_exceeded_gate",
})
_UNAVAILABLE = frozenset({
    "index_missing", "stale_snapshot", "snapshot_mismatch", "claim_mismatch",
    "source_unavailable", "upstream_unreachable", "snapshot_channel_error",
})
_NO_MATCH = frozenset({
    "no_hit", "no_match", "shelf_gap", "empty_shelf", "no_candidates",
})


def classify(payload: dict) -> str:
    """Map an existing tool payload onto one code. Pure, no side effects."""
    if not isinstance(payload, dict):
        return "TOOL_FAILED"
    # A tool may already know better (e.g. a budget guard). Respect it.
    given = str(payload.get("code") or "").upper()
    if given in CODES:
        return given
    reason = str(payload.get("reason") or "").strip().lower()
    if reason == "snapshot_channel_off" or payload.get("channel_off"):
        return "CHANNEL_OFF"
    if reason == "budget_exceeded" or payload.get("budget_exceeded"):
        return "BUDGET_EXCEEDED"
    if reason in _ACCESS:
        return "ACCESS_RESTRICTED"
    if reason in _UNAVAILABLE:
        return "SOURCE_UNAVAILABLE"
    if reason in _INVALID or reason.startswith("unknown_"):
        return "INVALID_REQUEST"
    if reason in _NO_MATCH:
        return "NO_MATCH"
    if payload.get("ok") is False:
        # Unmapped internal reasons are platform failures, not bad questions.
        return "TOOL_FAILED" if reason else "INVALID_REQUEST"
    if "candidates" in payload and not payload.get("candidates"):
        return "NO_MATCH"
    if payload.get("shelf_gap"):
        return "NO_MATCH"
    if payload.get("access_gap") or payload.get("gate_first"):
        return "ACCESS_RESTRICTED"
    up = str(payload.get("upstream_status") or "").lower()
    if up in ("unreachable", "auth_failed", "empty_response"):
        return "ACCESS_RESTRICTED" if up == "auth_failed" else "SOURCE_UNAVAILABLE"
    return "OK"


def decorate(payload: dict) -> dict:
    """Return a copy with ``code`` and (on failure) ``code_detail`` added.

    ``ok`` / ``reason`` / ``NEXT`` and every domain field stay untouched: old
    callers keep working, new callers can branch on one stable string.
    """
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    code = classify(out)
    out["code"] = code
    if code != "OK":
        detail = dict(CODES[code])
        # NO_MATCH 有两副面孔：一条候选都没有，或「有候选但全是字面相关、
        # 没有能力声明接得住」（shelf_gap）。后者最容易误读成「这里有答案」，
        # 所以下一步要分开说，别让 agent 拿着 literal_only 的候选去取数。
        if code == "NO_MATCH" and out.get("candidates"):
            detail["next"] = (
                "candidates 非空但 shelf_gap 也非空：这些都是字面相关、"
                "平台不认为能回答该问题的路牌（literal_only），别当答案来源；"
                "读 gap 看缺哪一轴，改走外部检索或 dp_hub_route。")
        out["code_detail"] = detail
    return out


def catalogue() -> dict:
    """For MCP ``initialize`` instructions: the code vocabulary itself."""
    return {k: {"label": v["label"], "retryable": v["retryable"]}
            for k, v in CODES.items()}
