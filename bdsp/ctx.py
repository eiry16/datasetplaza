"""Per-request identity, carried in a ContextVar instead of a parameter.

Why not an argument: the ledger is written deep inside ``Platform.route`` /
``prove`` / ``answer``, and every one of those call sites would grow a new
parameter that no caller outside the gateway cares about. A ContextVar keeps
the facade signature honest and is safe here because ``ThreadingHTTPServer``
runs one thread per request and a thread starts with its own copy of the
context — a value set inside a request never leaks into the next one.

**Empty means "us"**, not "anonymous": stdio MCP, the CLI and the tests all
run with no client set. That distinction matters when the ledger is read —
absent identity is local operator traffic, ``anon`` is a stranger from the
internet, and the two must never be added up silently.

``intent`` is the line between "a stranger touched the sign" and "a stranger
asked a question":

    handshake  initialize / tools/list / ping —— 不表达任何数据需求
    browse     dp_index / dp_catalog / dp_hub —— 在货架上找类型
    query      dp_route / dp_discover / dp_card / dp_answer —— 带着问题来的
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

CLIENT: ContextVar[str] = ContextVar("bdsp_client", default="")
INTENT: ContextVar[str] = ContextVar("bdsp_intent", default="query")

INTENTS = ("handshake", "browse", "query")


def current() -> tuple[str, str]:
    return CLIENT.get(), INTENT.get()


@contextmanager
def scope(client: str = "", intent: str = "query"):
    """Set identity for the duration of one request; restore on exit."""
    c_tok = CLIENT.set(str(client or ""))
    i_tok = INTENT.set(intent if intent in INTENTS else "query")
    try:
        yield
    finally:
        INTENT.reset(i_tok)
        CLIENT.reset(c_tok)


def set_intent(intent: str) -> None:
    """Switch the intent tag mid-request (handshake -> tools/call)."""
    INTENT.set(intent if intent in INTENTS else "query")
