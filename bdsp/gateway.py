"""Public read-half gateway: MCP over Streamable HTTP for strangers.

The console in ``bdsp.admin`` is an operator tool — session cookie, CSRF,
payment config, dataset onboarding — and it binds to 127.0.0.1 on purpose.
This module is the opposite: a deliberately small, anonymous, read-only
surface meant to be put on the public internet behind Cloudflare.

What it does **not** do, and must never start doing:

* no onboarding, no refresh, no toggle, no ``grant()``, no payment config —
  the tool list is whatever ``MCPServer`` exposes, and that list is the
  read/query half only;
* no session store. Every request is self-contained, so a restart loses
  nothing and there is no ``Mcp-Session-Id`` to keep in sync;
* no per-user identity. Anonymous callers are recorded under a salted
  pseudonym so the ledger can count distinct callers without storing IPs.

Three things a public endpoint needs that stdio never does, all here:

    Origin     浏览器能发的请求必须来自白名单；非浏览器客户端不带 Origin。
    Quota      按 IP 的分钟窗 + 日上限；写操作（踩坑留言）另算，更严。
    Redaction  ``MCPServer(public=True)`` 把异常换成 ref、把内部路径剥掉。

    python -m bdsp.gateway --port 8080 --trust-proxy --tunnel

``--tunnel`` 会自己拉起 ``cloudflared``（免费 quick tunnel），把拿到的
``https://xxx.trycloudflare.com`` 打印出来，Ctrl+C 一起收。想自己开一个终端
跑隧道就把它去掉，命令不变：
``cloudflared tunnel --url http://127.0.0.1:8080``
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import ctx as ctxmod
from .mcp_server import MCPServer, TOOLS, tools_for

MAX_BODY = 256 * 1024           # a tools/call body is < 4 KB; 256 KB is slack
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_VERSION = "2025-06-18"
SERVER_LABEL = "DatasetPlazz-Gateway/0.8"


# ---------------------------------------------------------------- quota


class Quota:
    """Per-caller rate limits, in memory, one process.

    Deliberately not persisted: a restart resetting the minute window is
    harmless, and a durable counter on the hot path would mean a write per
    request. The daily cap is the one that matters for abuse, so it is
    evaluated against a date key and survives only as long as the process.
    If this ever runs multi-instance, move it to SQLite — see README.
    """

    def __init__(self, per_min: int = 30, per_day: int = 300,
                 writes_per_day: int = 5):
        self.per_min = max(0, int(per_min))
        self.per_day = max(0, int(per_day))
        self.writes_per_day = max(0, int(writes_per_day))
        self._minute: dict[str, deque] = {}
        self._day: dict[tuple[str, str], int] = {}
        self._writes: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _bump(self, table: dict, key, limit: int) -> tuple[bool, int]:
        n = table.get(key, 0) + 1
        if limit and n > limit:
            return False, 0
        table[key] = n
        return True, limit - n

    def hit(self, caller: str) -> tuple[bool, str, int]:
        """Return (allowed, reason, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            if len(self._minute) > 4096:     # cheap unbounded-growth guard
                self._minute = {k: v for k, v in self._minute.items() if v}
            win = self._minute.setdefault(caller, deque())
            while win and now - win[0] > 60:
                win.popleft()
            if self.per_min and len(win) >= self.per_min:
                return False, "rate_limited_minute", max(1, int(60 - (now - win[0])))
            win.append(now)
            ok, _ = self._bump(self._day, (caller, self._today()), self.per_day)
            if not ok:
                return False, "rate_limited_day", 3600
            return True, "", 0

    def hit_write(self, caller: str) -> tuple[bool, str, int]:
        with self._lock:
            ok, _ = self._bump(self._writes, (caller, self._today()),
                               self.writes_per_day)
            if not ok:
                return False, "write_quota_exceeded", 3600
            return True, "", 0


# --------------------------------------------------------------- tunnel


TUNNEL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com", re.I)


def display_width(text: str) -> int:
    """Terminal cells, not code points: CJK takes two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


def landing_html(host: str, at_endpoint: bool = False,
                 scheme: str = "https") -> str:
    """The page a human gets when they paste the domain into a browser.

    Nothing here is product surface: no dataset names, no stats, no paths.
    It exists so that a URL typed by hand does not look like a broken deploy.
    """
    url = f"{scheme}://{host}/mcp"
    if at_endpoint:
        lead = ("你打开的是 MCP 端点本身。浏览器用 GET 拿不到东西——"
                "本服务不主动开服务端流，所以这里规范地回 405。"
                "要真正用起来，请把地址填进 MCP 客户端。")
    else:
        lead = "这是一个 MCP 端点，不是网页。"
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Dataset Plazz · MCP 路牌</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ margin:0; padding:48px 24px; font:16px/1.7 -apple-system,BlinkMacSystemFont,
   "Segoe UI","Microsoft YaHei",sans-serif; background:#faf9f7; color:#1f2328; }}
 main {{ max-width:640px; margin:0 auto; }}
 h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:.02em; }}
 .sub {{ color:#6b7280; font-size:14px; margin:0 0 28px; }}
 p {{ margin:0 0 16px; }}
 code {{ font:14px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;
   background:#f1efeb; padding:3px 6px; border-radius:4px; word-break:break-all; }}
 .addr {{ display:block; padding:14px 16px; margin:20px 0 24px;
   background:#fff; border:1px solid #e3e0da; border-left:3px solid #2f6f4e;
   border-radius:6px; }}
 dl {{ display:grid; grid-template-columns:7em 1fr; gap:8px 16px; margin:0 0 24px;
   font-size:14px; }}
 dt {{ color:#6b7280; }}
 dd {{ margin:0; }}
 footer {{ margin-top:36px; padding-top:16px; border-top:1px solid #e3e0da;
   font-size:13px; color:#6b7280; }}
 @media (prefers-color-scheme:dark) {{
   body {{ background:#15171a; color:#e6e3de; }}
   code {{ background:#22262b; }}
   .addr {{ background:#1c1f24; border-color:#2c3138; border-left-color:#5fb98b; }}
   footer {{ border-top-color:#2c3138; color:#9aa3ad; }}
   .sub, dt {{ color:#9aa3ad; }}
 }}
</style></head>
<body><main>
<h1>Dataset Plazz · MCP 路牌</h1>
<p class="sub">只读数据指路服务 · Streamable HTTP</p>
<p>{lead}</p>
<p>给 agent 的地址就这一条：</p>
<span class="addr"><code>{url}</code></span>
<dl>
  <dt>传输</dt><dd>Streamable HTTP（无状态，无 Mcp-Session-Id）</dd>
  <dt>工具</dt><dd>{len(TOOLS)} 个，只读；上手 / 支付 / 上下架都不在其中</dd>
  <dt>鉴权</dt><dd>匿名开放，按调用方 IP 限流；无需 OAuth</dd>
  <dt>健康检查</dt><dd><a href="/health">/health</a></dd>
</dl>
<p>填进 MCP 客户端的「URL / 服务器地址」即可，不用填命令、不用装包。
直接 GET 这个地址会得到 405，那是规范行为，不是故障。</p>
<footer>平台只指路，不代取：目录与索引类返回都不构成答案存在性证明。
取数请按 <code>dp_usage</code> 的交接单直接连源方（有 MCP 就挂它的 MCP），平台只记一条使用账。</footer>
</main></body></html>
"""


def parse_tunnel_url(line: str) -> str:
    """Lift the public URL out of a cloudflared stderr line.

    cloudflared prints it inside an ASCII box, sometimes with a trailing
    ``|`` glued to the host, so anchor on the host graph itself instead of
    splitting words.
    """
    m = TUNNEL_RE.search(line or "")
    return m.group(0).lower() if m else ""


def find_cloudflared(explicit: str = "") -> str:
    if explicit:
        return explicit
    found = shutil.which("cloudflared")
    if found:
        return found
    for cand in (Path.home() / "bin" / "cloudflared.exe",
                 Path.home() / "bin" / "cloudflared",
                 Path("/usr/local/bin/cloudflared"),
                 Path("/usr/bin/cloudflared")):
        if cand.is_file():
            return str(cand)
    return ""


class QuickTunnel:
    """``cloudflared tunnel --url`` as a child process, URL captured live.

    The banner cloudflared prints is 60 lines of ASCII art and connection
    noise; only what a human needs gets forwarded (the URL, the error lines).
    Nothing here touches the MCP path — if the tunnel dies the local server
    keeps serving 127.0.0.1, which is the right failure mode.
    """

    def __init__(self, target: str, binary: str = "", proto: str = "auto"):
        self.target = target
        self.binary = binary
        self.proto = proto
        self.proc: subprocess.Popen | None = None
        self.url = ""
        self.ready = threading.Event()
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def start(self) -> bool:
        cmd = [self.binary, "tunnel", "--url", self.target, "--no-autoupdate"]
        if self.proto and self.proto != "auto":
            cmd += ["--protocol", self.proto]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  ! 起不来 cloudflared：{exc}", file=sys.stderr)
            return False
        threading.Thread(target=self._pump, daemon=True).start()
        return True

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            with self._lock:
                if len(self._lines) < 400:
                    self._lines.append(line)
            found = parse_tunnel_url(line)
            if found and not self.url:
                self.url = found
                self.ready.set()
                continue
            low = line.lower()
            if any(k in low for k in ("error", "failed", "unable", "denied",
                                      "timeout", "refused")):
                print(f"  ! {line}", file=sys.stderr)
        self.ready.set()        # process ended: unblock the waiter

    def wait(self, timeout: float = 25.0) -> str:
        self.ready.wait(timeout)
        return self.url

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass


def open_landing(url: str, enabled: bool) -> None:
    """Best-effort browser open in a daemon thread.

    Never blocks startup and never fails hard: on a headless box (the eventual
    贵州云 host) ``webbrowser`` just returns False, and printing the URL is
    already enough.
    """
    if not enabled:
        return

    def run() -> None:
        try:
            ok = webbrowser.open(url)
        except Exception:            # noqa: BLE001 - no display, no problem
            ok = False
        if not ok:
            print(f"  （没能自动打开浏览器，手动访问 {url}）",
                  file=sys.stderr, flush=True)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- gateway


class Gateway:
    def __init__(self, server: MCPServer, token: str = "",
                 quota: Quota | None = None, allow_origins=(),
                 trust_proxy: bool = False, salt: str = ""):
        self.server = server
        self.token = token or ""
        self.quota = quota or Quota()
        self.allow_origins = {o.rstrip("/") for o in allow_origins if o}
        self.trust_proxy = bool(trust_proxy)
        self.salt = salt or secrets.token_hex(16)
        # The ledger is written through a single sqlite connection; serialize
        # the whole call so two threads cannot interleave a commit.
        self.lock = threading.RLock()

    # ---- identity -------------------------------------------------

    def caller_ip(self, handler) -> str:
        if self.trust_proxy:
            real = (handler.headers.get("CF-Connecting-IP")
                    or handler.headers.get("X-Real-IP"))
            if real:
                return real.strip()
            fwd = handler.headers.get("X-Forwarded-For") or ""
            if fwd:
                return fwd.split(",")[0].strip()
        return handler.client_address[0]

    def pseudonym(self, ip: str) -> str:
        """Salted hash: count distinct callers without keeping their address."""
        digest = hashlib.sha256((self.salt + ip).encode("utf-8")).hexdigest()
        return "anon:" + digest[:16]

    def check_origin(self, handler) -> bool:
        origin = (handler.headers.get("Origin") or "").strip()
        if not origin:
            return True                     # non-browser client: no Origin
        if not self.allow_origins:
            return False                    # browser + no allowlist = refuse
        return origin.rstrip("/") in self.allow_origins

    def check_auth(self, handler) -> bool:
        if not self.token:
            return True                     # open tier
        got = (handler.headers.get("Authorization") or "").strip()
        if not got.lower().startswith("bearer "):
            return False
        return secrets.compare_digest(got[7:].strip(), self.token)


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = SERVER_LABEL
    protocol_version = "HTTP/1.1"

    @property
    def gw(self) -> Gateway:
        return self.server.gateway          # type: ignore[attr-defined]

    # ---- plumbing -------------------------------------------------

    def log_message(self, fmt, *args):
        # Keep the console quiet; one structured line per call is in _log().
        return

    def handle_one_request(self):
        # One handler instance serves every request on a keep-alive socket, so
        # the "body already consumed" flag must reset per request — otherwise a
        # drained body on request N would skip draining on N+1.
        self._body_done = False
        super().handle_one_request()

    def _log(self, status: int, note: str = "") -> None:
        # The path is in the line because a bare "404 not_found" is
        # undiagnosable: it looks identical whether a client mistyped /mcp or
        # a scanner is walking random URLs.
        path = urllib.parse.urlparse(getattr(self, "path", "") or "").path or "-"
        sys.stderr.write(
            f"  mcp {status} {getattr(self, '_ms', 0):.0f}ms"
            f" {self.command} {path} {getattr(self, '_caller', '-')} {note}".rstrip() + "\n")
        sys.stderr.flush()

    def _send(self, status: int, payload: bytes, ctype: str, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("MCP-Protocol-Version", DEFAULT_VERSION)
        for name, value in (extra or []):
            self.send_header(name, value)
        try:
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (ConnectionError, TimeoutError):
            # 与 admin 侧同一类：客户端在响应写完前走了（WinError 10053/10054）。
            # 请求已经处理完，没人收结果不算服务端故障，静默关连接即可。
            self.close_connection = True

    def _json(self, status: int, obj, extra=None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _fail(self, status: int, reason: str, extra=None, hint: str = "") -> None:
        # Drain first: an early rejection (403/400/415/429/404) answers before
        # the body is read. On HTTP/1.1 keep-alive those leftover bytes get
        # parsed as the next request line, and the eventual close RSTs the
        # client mid-read. Draining costs one bounded read and keeps the
        # socket honest.
        self._discard_body()
        body = {"ok": False, "reason": reason}
        if hint:
            body["hint"] = hint
        self._json(status, body, extra)
        self._log(status, reason)

    def _body_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -1

    def _discard_body(self) -> None:
        if getattr(self, "_body_done", False):
            return
        length = self._body_length()
        if length <= 0:
            self._body_done = True
            if length < 0:
                self.close_connection = True
            return
        if length > MAX_BODY:
            self.close_connection = True      # too big to chew: close cleanly
            self._body_done = True
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            self.close_connection = True
        self._body_done = True

    def _read_body(self) -> bytes:
        length = self._body_length()
        if length < 0:
            raise ValueError("bad_content_length")
        if length == 0:
            self._body_done = True
            return b""
        if length > MAX_BODY:
            raise ValueError("body_too_large")
        chunks, remaining = [], length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        self._body_done = True
        return b"".join(chunks)

    # ---- verbs ----------------------------------------------------

    def do_OPTIONS(self):
        if not self.gw.check_origin(self):
            return self._fail(403, "origin_not_allowed")
        origin = self.headers.get("Origin") or ""
        extra = [("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS"),
                 ("Access-Control-Allow-Headers",
                  "Content-Type, Authorization, MCP-Protocol-Version"),
                 ("Access-Control-Max-Age", "86400")]
        if origin:
            extra += [("Access-Control-Allow-Origin", origin),
                      ("Vary", "Origin")]
        self._send(204, b"", "text/plain; charset=utf-8", extra)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/health", "/healthz"):
            # Not a query, not counted: it is a probe, and probes are exactly
            # the traffic that must not become "heat".
            return self._json(200, {"ok": True, "service": "dataset-plazz-mcp",
                                    "transport": "streamable-http",
                                    "protocol": DEFAULT_VERSION})
        if path == "/":
            return self._send(200, landing_html(self._host(), scheme=self._scheme())
                              .encode("utf-8"), "text/html; charset=utf-8")
        if path == "/mcp":
            # Stateless server: we never open a server->client stream, so the
            # spec-legal answer to GET is 405 with Allow. A browser still gets
            # prose — "no_server_stream" in JSON reads like a broken deploy.
            extra = [("Allow", "POST, DELETE, OPTIONS")]
            if "text/html" in (self.headers.get("Accept") or "").lower():
                return self._send(405, landing_html(self._host(),
                                                    at_endpoint=True,
                                                    scheme=self._scheme())
                                  .encode("utf-8"), "text/html; charset=utf-8",
                                  extra)
            return self._json(405, {"ok": False, "reason": "no_server_stream"},
                              extra)
        return self._fail(404, "not_found")

    def _scheme(self) -> str:
        """Public scheme: Cloudflare terminates TLS and says so in the header."""
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0]
        proto = proto.strip().lower()
        return proto if proto in ("http", "https") else "http"

    def _host(self) -> str:
        """Host header, filtered — it is attacker-controlled and lands in HTML."""
        raw = (self.headers.get("Host") or "127.0.0.1").strip()
        safe = "".join(ch for ch in raw
                       if ch.isalnum() or ch in ".:-[]")[:120]
        return safe or "127.0.0.1"


    def do_DELETE(self):
        if urllib.parse.urlparse(self.path).path != "/mcp":
            return self._fail(404, "not_found")
        # Nothing to tear down; successful no-op keeps clients happy.
        self._json(200, {"ok": True, "session": "stateless"})

    def do_POST(self):
        started = time.monotonic()
        self._ms = 0.0
        self._caller = "-"
        path = urllib.parse.urlparse(self.path).path
        if path != "/mcp":
            return self._fail(404, "not_found")

        gw = self.gw
        if not gw.check_origin(self):
            return self._fail(403, "origin_not_allowed",
                              hint="浏览器来源不在白名单；非浏览器客户端不要带 Origin。")
        if not gw.check_auth(self):
            return self._fail(
                401, "unauthorized",
                [("WWW-Authenticate", 'Bearer realm="dataset-plazz"')])

        stated = (self.headers.get("MCP-Protocol-Version") or "").strip()
        if stated and stated not in SUPPORTED_VERSIONS:
            return self._fail(400, "unsupported_protocol_version",
                              hint=f"支持 {', '.join(SUPPORTED_VERSIONS)}")

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            return self._fail(415, "unsupported_media_type",
                              hint="请用 Content-Type: application/json")

        ip = gw.caller_ip(self)
        ok, reason, retry = gw.quota.hit(ip)
        if not ok:
            return self._fail(429, reason, [("Retry-After", str(retry))],
                              hint="路牌是免费的，但每个调用方有额度；稍后再试。")
        self._caller = gw.pseudonym(ip)

        try:
            raw = self._read_body()
        except ValueError as exc:
            return self._fail(413 if str(exc) == "body_too_large" else 400,
                              str(exc))

        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._ms = (time.monotonic() - started) * 1000
            return self._json(200, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700,
                                              "message": "Parse error"}})

        batch = isinstance(msg, list)
        messages = msg if batch else [msg]
        for one in messages:
            if (isinstance(one, dict) and one.get("method") == "tools/call"
                    and (one.get("params") or {}).get("name") == "dp_report_pitfall"):
                allowed, wreason, wretry = gw.quota.hit_write(ip)
                if not allowed:
                    self._ms = (time.monotonic() - started) * 1000
                    return self._fail(429, wreason,
                                      [("Retry-After", str(wretry))],
                                      hint="踩坑留言每天限 5 条：匿名写入需要防灌。")

        with gw.lock:
            with ctxmod.scope(self._caller, "query"):
                out = [r for r in (gw.server.handle(m) for m in messages) if r]

        self._ms = (time.monotonic() - started) * 1000
        payload = out if batch else (out[0] if out else {})
        if not batch and not out:
            # A bare notification: 202 with no body is the polite answer.
            return self._send(202, b"", "application/json; charset=utf-8")
        self._json(200, payload)
        tool = ""
        for one in messages:
            if isinstance(one, dict) and one.get("method") == "tools/call":
                tool = (one.get("params") or {}).get("name") or ""
        self._log(200, tool)


def serve(host: str = "127.0.0.1", port: int = 8080, sources=None, db=None,
          runtime=None, listings=None, pit_dir=None, token: str = "",
          per_min: int = 30, per_day: int = 300, writes_per_day: int = 5,
          allow_origins=(), trust_proxy: bool = False, salt: str = "",
          quiet: bool = False, tunnel: bool = False, tunnel_bin: str = "",
          tunnel_proto: str = "auto", open_page: bool | None = None,
          strict_readonly: bool = False) -> None:
    # 0.44.3：默认保持既有行为（踩坑回写在匿名限流下可用）；公共部署
    # 加 --strict-readonly 后，写工具既不出现在 tools/list，也不接受调用。
    server = MCPServer(sources=sources, db=db, runtime=runtime,
                       listings=listings, pit_dir=pit_dir, public=True,
                       strict_readonly=strict_readonly)
    salt = salt or _load_salt(Path(runtime) if runtime else None)
    gw = Gateway(server, token=token, quota=Quota(per_min, per_day, writes_per_day),
                 allow_origins=allow_origins, trust_proxy=trust_proxy, salt=salt)
    httpd = ThreadingHTTPServer((host, port), GatewayHandler)
    httpd.daemon_threads = True
    httpd.gateway = gw
    scheme = "http"
    if not quiet:
        auth = "Bearer 必需" if token else "匿名开放（按 IP 限流）"
        print(f"Dataset Plazz MCP 网关: {scheme}://{host}:{port}/mcp")
        print(f"  传输    = Streamable HTTP（无状态，无 Mcp-Session-Id）")
        print(f"  工具    = {len(tools_for(strict_readonly))} 个"
              f"{'，strict 只读（写工具已移除）' if strict_readonly else '，含 1 个限流写工具'}"
              "；上手/支付配置不在其中")
        print(f"  鉴权    = {auth}")
        print(f"  额度    = {per_min}/分钟 · {per_day}/天 · 留言 {writes_per_day}/天")
        print(f"  来源    = {', '.join(sorted(gw.allow_origins)) or '仅非浏览器客户端（不带 Origin）'}")
        print(f"  代理    = {'信任 CF-Connecting-IP / X-Forwarded-For' if trust_proxy else '不信任转发头（直连）'}")
        print(f"  数据    = {server.platform.db_path}（公网不返回此路径）")
        if not tunnel and host in ("127.0.0.1", "localhost"):
            print("  提示    = 要给别人连，加 --tunnel（自动起 cloudflared），"
                  "或另开终端跑 cloudflared tunnel --url "
                  f"http://127.0.0.1:{port}")

    tun: QuickTunnel | None = None
    if tunnel:
        binary = find_cloudflared(tunnel_bin)
        if not binary:
            print("  ! 没找到 cloudflared。装一个再试：winget install Cloudflare.cloudflared",
                  file=sys.stderr)
            httpd.server_close()
            server.close()
            raise SystemExit(2)
        tun = QuickTunnel(f"http://127.0.0.1:{port}", binary=binary,
                          proto=tunnel_proto)
        print(f"\n  正在建立隧道（{Path(binary).name}，协议 {tunnel_proto}）…")
        if not tun.start():
            httpd.server_close()
            server.close()
            raise SystemExit(2)
        url = tun.wait(30.0)
        if url:
            mcp_url = f"{url}/mcp"
            content = f"  公网 MCP 地址   {mcp_url} "
            rule = "─" * display_width(content)
            print(f"\n  ┌{rule}┐\n"
                  f"  │{content}│\n"
                  f"  └{rule}┘\n"
                  f"  给 agent / 客户端的就这一条：{mcp_url}\n"
                  "  临时域名，进程退出即失效；要长期域名改用命名隧道（需自有域名）。\n"
                  "  刚建好的头几秒可能报 1033（DNS 还在传播），重试即可。\n"
                  f"  自检：curl {url}/health\n", flush=True)
        else:
            print("  ! 30 秒内没拿到 trycloudflare 域名。常见原因：\n"
                  "    1) 出网被拦：先设 HTTPS_PROXY（cloudflared 认这个变量）再重试；\n"
                  "    2) QUIC 被墙：加 --tunnel-proto http2；\n"
                  "    3) 域名被占：重跑即可。\n"
                  "    网关本身仍在本地正常服务。", file=sys.stderr, flush=True)

    if open_page is None:
        open_page = bool(tunnel)        # tunnel ⇒ 这是一次“上线”，顺手打开看着
    if open_page:
        # 打开落地页而不是 /mcp：/mcp 在浏览器里注定是 405，看着像坏了。
        target = (f"{tun.url}/" if (tun and tun.url)
                  else f"{scheme}://{host}:{port}/")
        if not quiet:
            print(f"  浏览器  = {target}（--no-open 可关）", flush=True)
        open_landing(target, True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tun:
            tun.stop()
        httpd.server_close()
        server.close()


def _load_salt(runtime: Path | None) -> str:
    """Persistent salt so pseudonyms stay stable across restarts."""
    path = (runtime or Path("runtime")) / ".client-salt"
    try:
        if path.is_file():
            got = path.read_text(encoding="utf-8").strip()
            if got:
                return got
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(16)
        path.write_text(value, encoding="utf-8")
        return value
    except OSError:
        return secrets.token_hex(16)   # 无盘可写也不阻断：只是重启后换个假名


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="dataset-plazz-gateway",
        description="公网 MCP 网关（Streamable HTTP，只读半边）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--sources")
    ap.add_argument("--db")
    ap.add_argument("--runtime")
    ap.add_argument("--listings")
    ap.add_argument("--pit-dir")
    ap.add_argument("--token", default="", help="非空则要求 Authorization: Bearer <token>")
    ap.add_argument("--per-min", type=int, default=30)
    ap.add_argument("--per-day", type=int, default=300)
    ap.add_argument("--writes-per-day", type=int, default=5)
    ap.add_argument("--allow-origin", action="append", default=[],
                    help="允许的浏览器来源，可重复；不传则拒绝一切带 Origin 的请求")
    ap.add_argument("--trust-proxy", action="store_true",
                    help="信任 CF-Connecting-IP / X-Forwarded-For（置于 Cloudflare 之后）")
    ap.add_argument("--salt", default="", help="假名盐；默认用 runtime/.client-salt")
    ap.add_argument("--tunnel", action="store_true",
                    help="顺手拉起 cloudflared quick tunnel，打印公网 /mcp 地址")
    ap.add_argument("--tunnel-bin", default="", help="cloudflared 路径；默认从 PATH 找")
    ap.add_argument("--tunnel-proto", default="auto",
                    choices=("auto", "http2", "quic"),
                    help="隧道传输协议；QUIC 被墙时用 http2")
    ap.add_argument("--open", dest="open_page", action="store_true", default=None,
                    help="启动后自动打开落地页（默认：带 --tunnel 时自动打开）")
    ap.add_argument("--no-open", dest="open_page", action="store_false",
                    help="不打开浏览器；在无桌面的服务器上跑时用它")
    ap.add_argument("--strict-readonly", action="store_true",
                    help="Public deployment: drop dp_report_pitfall from tools/list "
                         "and refuse its calls (default keeps the 5/day write quota)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    serve(host=args.host, port=args.port, sources=args.sources, db=args.db,
          runtime=args.runtime, listings=args.listings, pit_dir=args.pit_dir,
          token=args.token, per_min=args.per_min, per_day=args.per_day,
          writes_per_day=args.writes_per_day, allow_origins=args.allow_origin,
          trust_proxy=args.trust_proxy, salt=args.salt, quiet=args.quiet,
          tunnel=args.tunnel, tunnel_bin=args.tunnel_bin,
          tunnel_proto=args.tunnel_proto, open_page=args.open_page,
          strict_readonly=args.strict_readonly)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
