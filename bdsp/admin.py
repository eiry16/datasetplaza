"""Dataset Plazz control plane: zero-dependency HTTP API + static console.

Run:  python -m bdsp.admin --port 8787
Open: http://127.0.0.1:8787/

The console is intentionally read-mostly. Every mutating endpoint rebuilds
the pinned index, so what the agent can query always matches what the
operator sees. Credentials live in runtime/credentials.json, never in a
public source.json and never in an API response.

Security: /api/* requires a session (HttpOnly + SameSite cookie) or a Bearer
token; browser writes additionally need a CSRF header. Auth endpoints, the
local checkout page and the signed payment webhook are the only exceptions.
--https wraps the socket with a locally generated self-signed certificate.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import (capability, catalog, enrich as enrichmod, forms as formsmod,
               harvest, heat as heatmod,
               ledger as ledgermod, listings as listingmod, payments as paymod,
               pitfalls, profile as profilemod, probe as probemod,
               rows_cache as rowscache, store as storemod, tls)
from .api import Platform
from .auth import AuthManager
from .contract import load

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "admin_ui"
DEFAULT_SOURCES = ROOT / "sources"
DEFAULT_DB = ROOT / "index.sqlite3"
DEFAULT_RUNTIME = ROOT / "runtime"
DEFAULT_LISTINGS = ROOT / "listings" / "listings.json"
MAX_BODY = 1024 * 1024
# 目录行缓存（0.39.2）。货架扩到 3.5 万条后，每次请求都重读 259 MB JSON
# （10.7s）再把每条重排成列表行（36s），首页要空等 47 秒；改成「按货架版本
# 缓存 + 落盘复用」，改行结构时把这个版本号加一，旧缓存自动作废。
CATALOG_ROWS_VERSION = rowscache.VERSION
CATALOG_ROWS_CACHE = rowscache.CACHE_NAME
SESSION = "会话 / Bearer"
PUBLIC = "公开"

API_DOC = [
    {"method": "GET", "path": "/api/overview", "desc": "总数、按类型分布、可用/付费统计、索引计数、支付模式", "auth": SESSION},
    {"method": "GET", "path": "/api/datasets", "desc": "数据集目录（已索引契约 + 目录条目）；支持 category / q / status / access / kind / publish 过滤，sort=heat|name|records|added（默认按交接账本 runtime/ledger.jsonl 的热度降序，每次 handoff 记 1 分；0.30 起不再读已删除的 sqlite 审计表）；limit/offset 分页（不传 limit 返回全量，3.5 万条约 177 MB，界面按 100 条一页拉）；每行带 heat 明细、ledger_state 与 pool（indexed|catalog_only）", "auth": SESSION},
    {"method": "GET", "path": "/api/datasets/{id}", "desc": "公开详情：接入档案（形态/准入/协议/入口/鉴权/限额/请求示例）、字段字典、覆盖声明、证据策略、连接状态、开发者、交接记账（与源方对账用）", "auth": SESSION},
    {"method": "GET", "path": "/api/categories", "desc": "分类词表", "auth": SESSION},
    {"method": "GET", "path": "/api/listings", "desc": "目录条目（仅声明存在与来源，未建内容索引）；支持 category / access / q 过滤", "auth": SESSION},
    {"method": "GET", "path": "/api/listings/{id}", "desc": "目录条目公开详情：接入档案（form/gate/endpoint/auth/limits/示例）、档案缺项、准入与付费（含源方受理链接）、证据等级、可达性", "auth": SESSION},
    {"method": "POST", "path": "/api/listings/{id}/publish", "desc": "上架 / 下架：改 published 开关，控制该数据源对 agent 与平台用户的可见性。下架不删档案、不影响后台管理视图；每次操作写入 runtime/publish_log.jsonl", "auth": SESSION},
    {"method": "POST", "path": "/api/listings/publish-batch", "desc": "批量上架 / 下架：传 ids 数组按当前筛选结果批量切换，或传 batch 按接入批次切换；只改动状态确实变化的条目", "auth": SESSION},
    {"method": "GET", "path": "/api/pitfalls/{id}", "desc": "踩坑留言区：agent 众包的遇坑记录（与 MCP dp_pitfalls 同源，只读）", "auth": SESSION},
    {"method": "POST", "path": "/api/discover", "desc": "目录级发现：按问题找可能相关的数据集，明确不产生答案存在性证明；命中即写一条 point 账（演练传 drill=true 不记）", "auth": SESSION},
    {"method": "POST", "path": "/api/listings/{id}/check", "desc": "重新实测该目录条目上游可达性", "auth": SESSION},
    {"method": "POST", "path": "/api/listings/{id}/harvest", "desc": "打开该条目背后的数据集看真实字段，重写实测能力卡", "auth": SESSION},
    {"method": "POST", "path": "/api/listings/{id}/connect", "desc": "【已下线 0.27】原：一键接入为真实快照；现返回 410——平台不代取，接入档案交 agent", "auth": SESSION},
    {"method": "POST", "path": "/api/datasets/probe", "desc": "只传 endpoint_url + api_key，自动探测并生成待确认契约", "auth": SESSION},
    {"method": "POST", "path": "/api/datasets", "desc": "【已下线 0.27】原：确认上架并落盘快照；现返回 403——新源请走目录条目", "auth": SESSION},
    {"method": "POST", "path": "/api/datasets/{id}/check", "desc": "连接体检：快照哈希 / 索引 / 声明 / 上游可达性", "auth": SESSION},
    {"method": "POST", "path": "/api/datasets/{id}/refresh", "desc": "【已下线 0.27】原：重拉快照；现返回 410——历史快照只读，不再刷新", "auth": SESSION},
    {"method": "POST", "path": "/api/datasets/{id}/toggle", "desc": "上架 / 下架（enabled=false 时从索引移除）", "auth": SESSION},
    {"method": "POST", "path": "/api/check-all", "desc": "批量体检", "auth": SESSION},
    {"method": "POST", "path": "/api/rebuild", "desc": "重建字段级索引并复验全部声明", "auth": SESSION},
    {"method": "GET", "path": "/api/audit", "desc": "交接账本（runtime/ledger.jsonl 最近 N 条 handoff）；0.30 起不再读已删除的 sqlite 审计表", "auth": SESSION},
    {"method": "POST", "path": "/api/query", "desc": "【已下线 0.27】原：route → 免费 proof → 门禁 → 取值；现返回 410——平台不代取", "auth": SESSION},
    {"method": "POST", "path": "/api/grant", "desc": "sandbox 模拟回调；external 模式必须走验签 webhook", "auth": SESSION},
    {"method": "GET", "path": "/api/api-doc", "desc": "本接口清单", "auth": SESSION},
    {"method": "GET", "path": "/api/auth/status", "desc": "登录状态、是否启用鉴权、是否 HTTPS", "auth": PUBLIC},
    {"method": "POST", "path": "/api/auth/login", "desc": "管理员密码登录，返回会话 Cookie 与 CSRF token", "auth": PUBLIC},
    {"method": "POST", "path": "/api/auth/logout", "desc": "注销会话", "auth": PUBLIC},
    {"method": "GET", "path": "/api/pay/config", "desc": "支付通道配置（不含密钥）", "auth": SESSION},
    {"method": "POST", "path": "/api/pay/config", "desc": "切换 sandbox / external 并配置收银台与 webhook 密钥", "auth": SESSION},
    {"method": "GET", "path": "/api/pay/invoice/{id}", "desc": "按发票号读取发票元数据", "auth": PUBLIC},
    {"method": "GET", "path": "/pay?invoice_id={id}", "desc": "本地沙盒收银台 / 授权确认页", "auth": PUBLIC},
    {"method": "POST", "path": "/api/pay/confirm", "desc": "sandbox 模拟结算，签发 invoice 绑定的 token", "auth": PUBLIC},
    {"method": "POST", "path": "/api/pay/webhook", "desc": "真实支付回调：HMAC-SHA256 验签 + nonce 防重放后签发 token", "auth": "HMAC 签名"},
    {"method": "GET", "path": "/api/console/status", "desc": "控制台会话状态：后端是否绑定了本次打开的控制台", "auth": PUBLIC},
    {"method": "POST", "path": "/api/console/heartbeat", "desc": "控制台页面心跳；后端只靠它续命，控制台全部关闭即自行退出", "auth": "会话 token"},
    {"method": "POST", "path": "/api/console/leave", "desc": "控制台页面关闭上报；最后一个页面离开后宽限数秒即退出", "auth": "会话 token"},
    {"method": "POST", "path": "/api/console/stop", "desc": "本机启动器接管端口时请求旧后端自行退出（口令存 runtime/，仅本机可达）", "auth": "本机口令"},
]


def _json_response(status: int, payload: dict) -> tuple[int, dict]:
    return status, payload


TOKEN_RE = re.compile(r"[0-9a-f]{16,64}\Z")


class ConsoleSession:
    """「控制台活着，后端才活着」的会话凭证。

    启动器为每次打开控制台生成一个随机 token；后端只在收到带该 token 的
    页面心跳时续命。这样生命周期绑定的是「页面」，不是浏览器进程——
    浏览器复用进程、多标签、刷新、换浏览器都不会误判。
    启动器即使被强杀，后端也会在心跳消失后自行退出，不留孤儿进程。
    """

    def __init__(self, token: str, idle_seconds: float = 20.0,
                 boot_grace_seconds: float = 120.0, leave_grace_seconds: float = 6.0):
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise ValueError("console token 必须是 16-64 位小写十六进制")
        self.token = token
        self.idle = max(3.0, float(idle_seconds))
        self.boot_grace = max(float(boot_grace_seconds), self.idle)
        self.leave_grace = max(1.0, float(leave_grace_seconds))
        self.started = time.monotonic()
        self.last_seen: float | None = None
        self.clients: dict[str, float] = {}
        self.leave_at: float | None = None
        self.seen_any = False
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.expired_flag = False

    def check(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(str(token), self.token)

    def touch(self, client_id: str = "") -> None:
        now = time.monotonic()
        with self.lock:
            self.last_seen = now
            self.seen_any = True
            self.leave_at = None
            if client_id:
                self.clients[client_id] = now

    def leave(self, client_id: str = "") -> None:
        now = time.monotonic()
        with self.lock:
            if client_id:
                self.clients.pop(client_id, None)
            if not self.clients:
                self.leave_at = now

    def expired(self) -> bool:
        now = time.monotonic()
        with self.lock:
            if not self.seen_any:
                # 控制台从未真正打开（页面加载失败/浏览器没起来）：宽限后退出
                return now - self.started > self.boot_grace
            if self.leave_at and not self.clients and now - self.leave_at > self.leave_grace:
                return True
            return now - (self.last_seen or self.started) > self.idle

    def public_state(self) -> dict:
        with self.lock:
            return {
                "bound": True,
                "session": self.token[:8],  # 只暴露前缀，供启动器核对，不当凭证用
                "idle_seconds": self.idle,
                "boot_grace_seconds": self.boot_grace,
                "clients": len(self.clients),
                "seen_any": self.seen_any,
            }


def console_lifetime(session: ConsoleSession, on_expire, poll_interval: float = 0.5) -> str:
    """守护循环：会话过期就触发停机。单独成函数便于无网络单测。"""
    while not session.stop_event.wait(poll_interval):
        if session.expired():
            session.expired_flag = True
            on_expire()
            return "expired"
    return "stopped"


def inject_console_token(html: bytes, token: str | None) -> bytes:
    """把本次会话 token 注入静态首页；生产模式（无 token）保持空值。"""
    if not token:
        return html
    needle = b'<meta name="dp-console-token" content="">'
    if needle not in html:
        return html
    return html.replace(needle, b'<meta name="dp-console-token" content="'
                        + token.encode("ascii") + b'">', 1)


class AdminService:
    def __init__(self, sources=None, db=None, runtime=None, auth_enabled=True, password=None,
                 listings=None, console: ConsoleSession | None = None, ledger=None):
        self.sources = Path(sources or DEFAULT_SOURCES)
        self.db = Path(db or DEFAULT_DB)
        self.runtime = Path(runtime or DEFAULT_RUNTIME)
        self.listings_path = Path(listings or DEFAULT_LISTINGS)
        # 热度读这本交接账本（BDSP_LEDGER 可覆盖）；不传就走 ledger.default_path()。
        self.ledger_path = Path(ledger) if ledger else ledgermod.default_path()
        self.console = console  # None = 生产常驻；绑定了就是「随控制台开关」
        self._stop_callback = None
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.credentials_path = self.runtime / "credentials.json"
        self.health_path = self.runtime / "health.json"
        self.payments = paymod.PaymentGateway(runtime=self.runtime)
        self.platform = Platform(
            sources=self.sources, db=self.db, runtime=self.runtime, payments=self.payments
        )
        self.auth = AuthManager(runtime=self.runtime, enabled=auth_enabled, password=password)
        self.lock = threading.RLock()
        self.drafts: dict[str, probemod.ProbeResult] = {}
        self.health = self._load(self.health_path, {})
        # (账本签名, 热度表, 账本状态)：目录页每次请求都要热度，账本只追加，
        # 用「是否存在 + 行数 + mtime」当版本号即可，避免每开一个详情抽屉
        # 就全表扫一遍。状态要让 UI 分清「账本缺失」和「没人问」。
        self._heat_cache: tuple[tuple, dict, str] = ((), {}, "")
        # (签名, 目录行)：3.5 万条重排一次 36s，必须按货架版本复用；
        # 落盘一份，重启后直接反序列化，不用再等一轮。
        self._rows_cache: tuple[tuple, list[dict]] | None = None
        self._rows_path = self.listings_path.parent / CATALOG_ROWS_CACHE
        self._rows_meta_path = self._rows_path.with_suffix(".meta.json")

    def set_base_url(self, url: str) -> None:
        self.platform.set_base_url(url)

    # ---------------- security ----------------

    def auth_status(self, headers, secure=False) -> dict:
        out = self.auth.status(headers, secure)
        self.payments.reload()
        out["payment"] = self.payments.public_config()
        # Version marker: lets the launcher tell a fresh 0.4+ backend from an
        # older instance still holding the port after a partial restart.
        out["shelf_enabled"] = True
        out["shelf_version"] = "1"
        # 启动器据此确认「这次打开控制台」确实绑定了后端
        out["console"] = self.console.public_state() if self.console else {"bound": False}
        return out

    def set_stop_callback(self, callback) -> None:
        """启动器接管端口时，让本进程体面退出（不依赖 netstat 找 PID）。"""
        self._stop_callback = callback

    # ---------------- console lifetime ----------------

    def console_dispatch(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        if method == "POST" and path == "/api/console/stop":
            # 与会话 token 无关：常驻后端也能被新的「打开控制台」接管
            try:
                expected = (self.runtime / "console_stop.token").read_text(encoding="utf-8").strip()
            except OSError:
                expected = ""
            given = str(body.get("stop_token") or "")
            if not expected or not given or not hmac.compare_digest(given, expected):
                return _json_response(403, {"ok": False, "reason": "bad_stop_token"})
            cb = getattr(self, "_stop_callback", None)
            if cb is None:
                return _json_response(503, {"ok": False, "reason": "stop_unavailable"})
            cb()
            return _json_response(200, {"ok": True, "stopping": True})
        sess = self.console
        if sess is None:
            return _json_response(404, {"ok": False, "reason": "console_not_bound"})
        if method == "GET" and path == "/api/console/status":
            return _json_response(200, {"ok": True, **sess.public_state()})
        if method != "POST" or path not in ("/api/console/heartbeat", "/api/console/leave"):
            return _json_response(404, {"ok": False, "reason": "not_found", "path": path})
        if not sess.check(str(body.get("token") or "")):
            return _json_response(403, {"ok": False, "reason": "bad_console_token"})
        cid = str(body.get("client_id") or "")[:64]
        if path.endswith("/leave"):
            sess.leave(cid)
        else:
            sess.touch(cid)
        return _json_response(200, {
            "ok": True, "alive": True, "session": sess.token[:8],
            "idle_seconds": sess.idle, "clients": sess.public_state()["clients"],
        })

    def auth_login(self, body: dict, headers, client_ip: str, secure=False) -> tuple[int, dict]:
        return self.auth.login(str(body.get("password") or ""), client_ip, secure)

    def auth_logout(self, headers, secure=False) -> tuple[int, dict]:
        return self.auth.logout(headers, secure)

    # ---------------- payment channel ----------------

    def pay_invoice(self, invoice_id: str) -> dict:
        invoice = self.platform.gate.get_invoice(invoice_id)
        if invoice is None:
            raise KeyError(f"invoice_unknown: {invoice_id}")
        return {"ok": True, "invoice": invoice, "config": self.payments.public_config()}

    def pay_checkout(self, invoice_id: str) -> str:
        self.payments.reload()
        invoice = self.platform.gate.get_invoice(invoice_id)
        if invoice is None:
            return (
                "<!DOCTYPE html><html lang='zh-CN'><meta charset='utf-8'>"
                "<body style='font:14px sans-serif;padding:40px'>"
                "<h2>发票不存在或已过期</h2><p>请用同一通道重新发起查询，"
                "拿到新的 invoice_id 后再打开收银台。</p></body></html>"
            )
        return paymod.checkout_html(invoice, self.payments)

    def pay_confirm(self, body: dict) -> dict:
        if self.payments.is_external:
            raise PermissionError("external 模式禁止本地模拟结算，请走签名 webhook")
        iid = str(body.get("invoice_id") or "")
        invoice = self.platform.gate.get_invoice(iid)
        if invoice is None:
            raise KeyError(f"invoice_unknown: {iid}")
        expires = str(invoice.get("expires_at") or "")
        if expires and expires < catalog.now_iso():
            raise ValueError("invoice_expired: 发票已过期，请重新查询")
        token = self.platform.grant(iid, invoice["dataset_id"])
        return {
            "ok": True,
            "token": token,
            "invoice_id": iid,
            "dataset_id": invoice["dataset_id"],
            "mode": "sandbox",
            "sandbox": True,
            "note": "本地模拟结算，无真实扣款；把 token 作为 payment_token 重试查询",
        }

    def pay_webhook(self, raw_body: bytes, headers) -> tuple[int, dict]:
        valid, reason, receipt = self.payments.verify(raw_body, headers)
        if not valid or receipt is None:
            return 401, {"ok": False, "reason": reason, "charged": 0.0}
        try:
            token = self.platform.grant(
                str(receipt.get("invoice_id") or ""),
                str(receipt.get("dataset_id") or ""),
                receipt=receipt,
            )
        except PermissionError as exc:
            return 400, {"ok": False, "reason": "receipt_rejected", "detail": str(exc)}
        return 200, {
            "ok": True,
            "token": token,
            "receipt_id": receipt["_receipt_id"],
            "invoice_id": receipt.get("invoice_id"),
            "dataset_id": receipt.get("dataset_id"),
            "mode": self.payments.mode,
            "note": "签名回执已核销；把 token 作为 payment_token 重试查询",
        }

    @staticmethod
    def _load(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def credentials(self) -> dict:
        return self._load(self.credentials_path, {})

    def mask(self, api_key: str | None) -> str:
        if not api_key:
            return ""
        return api_key[:3] + "••••" + api_key[-4:]

    def _save_health(self) -> None:
        self.health_path.write_text(
            json.dumps(self.health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _rows(self):
        return {r["dataset_id"]: r for r in self.platform.datasets()}

    # ---------------- 目录行缓存 ----------------

    def _rows_signature(self) -> tuple:
        """0.40：签名与离线预生成共用一份实现，避免「构建时算一套、
        运行时算另一套」永远对不上。规则版本也进了签名。"""
        return rowscache.signature(self.sources, self.listings_path,
                                   self.db, self.health_path)

    def _read_rows_cache(self, sig: tuple) -> list[dict] | None:
        return rowscache.read(self._rows_path, sig)

    def _write_rows_cache(self, sig: tuple, rows: list[dict]) -> None:
        """写不进去不算错：缓存是加速件，磁盘满/只读目录都不该拖垮后台。"""
        rowscache.write(self._rows_path, self._rows_meta_path, sig, rows)

    def _patch_rows_cache(self, changed: list[dict]) -> bool:
        """上下架之后只重算受影响的行，别为 3 个开关重排 3.5 万条。

        0.40 之前：publish 改了 listings.json 的 mtime → 签名失效 → 下一位
        读者的首屏要冷建 49.5s。实际上 ``record()`` 里只有 published /
        published_at / publish_note / published_label 四个键跟着变，其余
        几万行的字节一个都没动。这里拿调用方手里的原始条目重算这几行，
        用新签名把缓存写回，代价从 49.5s 降到一次 pickle（约 2s）。

        只有缓存形状仍然可信时才打补丁：版本不一致、进程刚启动还没载入、
        或 pkl 损坏，一律放弃增量，交回 `_catalog_rows()` 的正常冷建。
        """
        if not changed:
            return False
        new_sig = self._rows_signature()
        rows = None
        if self._rows_cache is not None:
            rows = self._rows_cache[1]
        else:
            old_sig, cached = rowscache.read_any(self._rows_path)
            if (old_sig and old_sig[0] == rowscache.VERSION
                    and cached is not None):
                rows = cached
        if rows is None:
            self._rows_cache = None
            return False
        rows = list(rows)          # 别就地改：可能有另一个请求正读这份列表
        pos = {r.get("dataset_id"): n for n, r in enumerate(rows)}
        # 冷建的行使的是 load()→enrich.decorate() 之后的条目，带 ``_hub_meta``；
        # 直接拿原始 dict 重算会把 native_topics / distributions / summary /
        # meta_source 六个源方原生字段抹成空（实测，见 0.40 报告）。补一次
        # decorate 才能保证「打了补丁的行」和「没打的行」形状完全一致。
        for raw in enrichmod.decorate(list(changed)):
            rec = listingmod.record(raw)
            n = pos.get(rec["dataset_id"])
            if n is None:
                pos[rec["dataset_id"]] = len(rows)
                rows.append(rec)
            else:
                rows[n] = rec
        self._write_rows_cache(new_sig, rows)
        self._rows_cache = (new_sig, rows)
        return True

    def listings_count(self) -> int:
        """目录条数：缓存侧车优先，读不到才退回真解析（慢但正确）。"""
        try:
            meta = json.loads(self._rows_meta_path.read_text(encoding="utf-8"))
            if tuple(meta.get("sig") or ()) == self._rows_signature():
                return int(meta.get("count") or 0)
        except (OSError, ValueError, TypeError):
            pass
        return len(listingmod.load(self.listings_path))

    def _catalog_rows(self) -> list[dict]:
        """目录行（含 3.5 万条货架条目），按货架版本缓存。

        调用方要改行（目录页会挂 pitfall_count / heat）必须先 ``dict(row)``
        复制——缓存里的对象是共享的，就地改会串到下一位读者。
        """
        sig = self._rows_signature()
        if self._rows_cache is not None and self._rows_cache[0] == sig:
            return self._rows_cache[1]
        rows = self._read_rows_cache(sig)
        if rows is None:
            rows = catalog.records(self.sources, self.platform, self.health,
                                   self.listings_path)
            self._write_rows_cache(sig, rows)
        self._rows_cache = (sig, rows)
        return rows

    def warm_catalog_rows(self) -> float:
        """启动时预热：把 47s 的一次性重排挪到后台，别砸在第一个页面请求上。"""
        t0 = time.time()
        with self.lock:
            rows = self._catalog_rows()
        return time.time() - t0 if rows is not None else 0.0

    def _heat(self) -> tuple[dict[str, dict], str]:
        """现行交接账本 → (热度计数器, 账本状态)。

        0.28 删快照时把 ``index.sqlite3`` 一起删了，热度从此读到空账本、全表
        显示 0——那不是「没人问」，是「没有账本」。0.30 起以
        ``runtime/ledger.jsonl`` 为准（它一直完好，0.28 之后仍在累积）；
        只有这本不存在时才回退旧 sqlite，两本都不存在才标 ``no_ledger``。
        **两本不混算**：``dp_usage`` 会同时写 jsonl 和 sqlite 的 point。
        """
        p = self.ledger_path
        try:
            exists = p.is_file()
            rows = ledgermod.read(p) if exists else []
            mtime = p.stat().st_mtime_ns if exists else 0
        except OSError:
            exists, rows, mtime = False, [], 0
        sig = (exists, len(rows), mtime)
        if not exists:
            # 旧部署兼容：看 sqlite 里还有没有 audit 表，顺便当缓存版本。
            try:
                sig = (False, self.platform.con.execute(
                    "SELECT COUNT(*) FROM audit").fetchone()[0], 0)
            except Exception:                                  # noqa: BLE001
                sig = (False, -1, 0)
        if self._heat_cache[0] == sig:
            return self._heat_cache[1], self._heat_cache[2]
        if exists:
            recs, state = (heatmod.compute_ledger(rows),
                           heatmod.LEDGER_STATE_CURRENT)
        elif sig[1] >= 0:
            try:
                recs = heatmod.compute(self.platform.con)
            except Exception:                                  # noqa: BLE001
                recs = {}
            state = heatmod.LEDGER_STATE_FALLBACK
        else:
            recs, state = {}, heatmod.LEDGER_STATE_MISSING
        self._heat_cache = (sig, recs, state)
        return recs, state

    def _find(self, dataset_id: str):
        p = self.sources / dataset_id / "source.json"
        if not p.is_file():
            raise KeyError(f"dataset_not_found: {dataset_id}")
        return load(p), p

    # ---------------- read ----------------

    def overview(self) -> dict:
        # 只读：catalog.overview() 不碰行内字段，直接吃缓存，不再复制 3.5 万份。
        with self.lock:
            rows = self._catalog_rows()
        return catalog.overview(rows, self.platform)

    def datasets(self, filters: dict) -> dict:
        with self.lock:
            # 浅复制：下面要往行上挂 pitfall_count / heat，不能改共享缓存。
            items = [dict(r) for r in self._catalog_rows()]
        pit = pitfalls.pitfall_counts()  # 已索引条目同样带踩坑计数
        for i in items:
            i["pitfall_count"] = pit.get(i["dataset_id"], 0)
        q = (filters.get("q") or "").strip().lower()
        category = (filters.get("category") or "all").strip()
        status = (filters.get("status") or "all").strip()
        access = (filters.get("access") or "all").strip()
        kind = (filters.get("kind") or "all").strip()
        publish = (filters.get("publish") or "all").strip()
        if publish != "all":
            want = publish == "published"
            items = [i for i in items if (i.get("published") is not False) == want]
        if category != "all":
            items = [i for i in items if i["category"] == category]
        if status != "all":
            items = [i for i in items if i["status"] == status]
        if access != "all":
            items = [i for i in items if i["access_level"] == access]
        if kind != "all":
            items = [i for i in items if i["kind"] == kind]
        if q:
            items = [
                i for i in items
                if q in " ".join([
                    i["dataset_id"], i["name"], i["publisher"],
                    i["developer"]["name"], " ".join(i["field_names"]),
                    " ".join(i["domain"]), i.get("notes", ""), i.get("source_url", ""),
                ]).lower()
            ]
        # 调用热度：只数交接账本，不预估值、不按索引体积加权。
        # 账本状态跟着卡片走，UI 才能把「账本缺失」和「没人问」分开画。
        heat_recs, heat_state = self._heat()
        for i in items:
            i["heat"] = heatmod.card(heat_recs.get(i["dataset_id"]), i,
                                     ledger_state=heat_state)
        sort = (filters.get("sort") or "heat").strip()
        items = heatmod.sort_items(items, sort)
        # 分页（0.39.2）：3.5 万条一次性下发是 177 MB JSON，浏览器解析完就卡死，
        # 页面看上去「什么都没有」。带 limit 才分页——脚本与测试不带 limit，
        # 仍旧拿到完整列表，行为不变。
        total = len(items)
        try:
            limit = int(filters.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        try:
            offset = max(0, int(filters.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        page = items[offset:offset + limit] if limit > 0 else items[offset:]
        return {"ok": True, "items": page, "total": total,
                "offset": offset, "limit": limit, "shown": len(page),
                "has_more": offset + len(page) < total,
                "sort": sort,
                "heat": heatmod.stats(page),
                "indexed_total": sum(1 for i in page if i["kind"] == "indexed"),
                "catalog_only": sum(1 for i in page if i["kind"] == "catalog_only")}

    def dataset_detail(self, dataset_id: str) -> dict:
        p = self.sources / dataset_id / "source.json"
        heat_recs, heat_state = self._heat()
        heat_rec = heat_recs.get(dataset_id)
        if p.is_file():
            with self.lock:
                src, _ = self._find(dataset_id)
                row = self._rows().get(dataset_id)
                out = catalog.detail(src, row, self.health.get(dataset_id))
            out["kind"] = "indexed"
            out["content_status"] = "indexed"
            out["content_label"] = "已接入索引"
            out["credential_masked"] = self.mask(self.credentials().get(dataset_id, {}).get("api_key"))
            out["pitfall"] = pitfalls.read_pitfalls(dataset_id)
            out["heat"] = heatmod.card(heat_rec, {"kind": "indexed"},
                                       ledger_state=heat_state)
            out["access_profile"] = self._indexed_profile(src, out)
            out["handoff"] = self._handoff(dataset_id)
            return out
        item = self._listing(dataset_id)
        out = listingmod.detail(item)
        out["pitfall"] = pitfalls.read_pitfalls(dataset_id)
        out["heat"] = heatmod.card(heat_rec, {"kind": "catalog_only"},
                                   ledger_state=heat_state)
        out["handoff"] = self._handoff(dataset_id)
        return out

    @staticmethod
    def _indexed_profile(src: dict, det: dict) -> dict:
        """已索引条目的接入档案：比目录条目硬——平台真的取到过数。

        只与 derived 有三处不同，都来自真实契约而非猜测：

        * ``form`` 按 endpoint 形态 + 实测落盘格式定（.csv → file，
          api.worldbank.org → stat_api，HTML → web_only）；
        * ``method`` 取 connection.method（实测用过的方法）；
        * ``basis`` 标 ``probe``——这是实测，不是「源方自述」也不是「推断」。

        请求示例仍然不编：已索引不代表平台知道源方文档里的必需 header。
        缺项也不回传：已索引条目的契约已经过探测校验，拿形态必填表去卡它
        只会给每条挂一个假错，把真正待补的目录条目淹掉。
        """
        conn = det.get("connection") or {}
        endpoint = str(conn.get("endpoint") or det.get("source_url") or "").strip()
        fmt = str((det.get("ingest") or {}).get("format") or "").lower()
        lvl = str((det.get("access") or {}).get("level") or "open")
        ext = endpoint.split("?")[0].rsplit(".", 1)[-1].lower() if "." in endpoint.split("?")[0] else ""
        fam = formsmod.family_from_url(endpoint)
        if ext in formsmod.FILE_FORMATS:
            form = "file"
        elif fam in formsmod.PORTAL_FAMILIES:
            form = "portal_api"
        elif fam in formsmod.STAT_FAMILIES:
            form = "stat_api"
        elif ext in ("html", "htm"):
            form = "web_only"
        else:
            form = "stat_api" if "api" in endpoint.lower() else "file"
        item = {
            "dataset_id": src.get("dataset_id", ""),
            "name": src.get("name", ""),
            "usage": src.get("usage"),
            "access_level": lvl,
            "data_url": endpoint,
            "source_url": endpoint,
            "catalog_url": "",
        }
        prof = profilemod.build(item)
        prof["form"] = form
        prof["gate"] = lvl if lvl in ("open", "registered", "authorized", "paid") else "open"
        prof["endpoint"] = endpoint
        prof["method"] = str(conn.get("method") or "GET").upper()
        prof["protocol"] = ("http-file" if form == "file" else
                            "http-json" if form in ("stat_api", "portal_api") else
                            prof.get("protocol") or "unknown")
        if form == "file" and fmt:
            prof["format"] = "txt" if fmt == "lines" else fmt
        if form == "web_only":
            prof["landing_url"] = endpoint
            prof["how_to"] = "页面为 HTML：需在浏览器里人工导出，或等源方开放接口"
        prof["basis"] = "probe"
        prof["updated_at"] = str(det.get("retrieved_at") or "")[:10] or prof.get("updated_at", "")
        prof["fields_note"] = f"平台已实测 {det.get('fields_count') or 0} 个字段（见下方字段字典）"
        # derive 会把 source_url 当 docs，而这里 source_url 就是取数端点本身——
        # 照抄会让「源方文档」显示成一条 API URL，读起来像有文档其实没有。
        prof["docs"] = ""
        # 已索引条目的 paid/registered 是**本平台**的代持与计价口径，不是源方
        # 订阅报价。照搬 derive 的「源方商业订阅；平台不代销不代收」会当场
        # 自我矛盾（这边明明在收），也会把「平台收费」误报成「源方收费」。
        price = (det.get("access") or {}).get("price") or {}
        amount = price.get("amount")
        cur = price.get("currency") or "CNY"
        per = price.get("per") or "query"
        if lvl != "open":
            prof["pricing"] = {
                "model": "legacy-platform-metered" if amount else "legacy-platform-gated",
                "note": (f"历史口径：平台曾在代持凭证时按次计价 {cur} {amount}/{per}。"
                         "代取通道已于 0.27 停用，此价不再发生；"
                         "源方现在怎么收费、怎么鉴权，需回源确认" if amount
                         else "历史口径：平台曾代持凭证、本平台不计价。代取通道已于 0.27 停用；"
                              "现在请按源方要求自备凭证"),
                "declared": price,
            }
            prof["auth"] = {"kind": "unknown",
                            "label": "历史：平台曾代持凭证 · 现已停止代取",
                            "how_to_get": "按源方文档自行申请；平台不代持、不代收"}
        else:
            prof["pricing"] = {"model": "free", "note": "源方开放取用（平台不代取，直接连源方）"}
            prof["auth"] = {"kind": "none", "label": "无需凭证", "how_to_get": ""}
        return profilemod.handoff_view(prof)

    def _handoff(self, dataset_id: str) -> dict:
        """这个源被「指到门口」几次——与源方对账的唯一凭据。

        0.30 起与页面上的「调用热度」**同读一本** `runtime/ledger.jsonl`：
        这里给出逐条聚合（交接次数、计费次数、最近一次），热度给出折算后的
        分数与排序。两者口径一致，运营可以逐行回溯；旧 sqlite 审计账本已在
        0.28 删除，不再是数据源。
        """
        try:
            agg = ledgermod.summary(dataset_id=dataset_id)
        except OSError:
            return {"handoffs": 0, "billable": 0, "last_at": "", "levels": [],
                    "billable_total": 0, "error": "账本不可读"}
        rows = agg.get("results") or []
        me = rows[0] if rows else {}
        return {
            "handoffs": int(me.get("handoffs") or 0),
            "billable": int(me.get("billable") or 0),
            "last_at": me.get("last_at") or "",
            "levels": me.get("levels") or [],
            "billable_total": int(agg.get("billable_total") or 0),
            "source": "runtime/ledger.jsonl",
        }

    def pitfall_detail(self, dataset_id: str) -> dict:
        """踩坑留言区读取（操作员控制台用，与 MCP dp_pitfalls 同源）。"""
        return pitfalls.read_pitfalls(dataset_id)

    # ---------------- catalogue-only shelf ----------------

    def _listing(self, dataset_id: str) -> dict:
        """按 id 取一条完整条目。

        优先走 ``listings/archive`` 的偏移索引（~45 ms）：``listings.json``
        已经 259 MB，为了一条详情去解析整份要 10.7 秒——后台每点开一次抽屉都
        在付这笔钱。档案不新鲜（条目被后台改过、还没重建 shelf）时退回整份
        解析：宁可慢，也不拿旧档案回答。
        """
        root = self.listings_path.parent
        try:
            if storemod.is_fresh(root, self.listings_path):
                item = storemod.hydrate([dataset_id], root=root).get(dataset_id)
                if item:
                    return item
        except Exception:  # noqa: BLE001 - 档案索引坏了就退回慢路径
            pass
        for item in listingmod.load(self.listings_path):
            if item["dataset_id"] == dataset_id:
                return item
        raise KeyError(f"dataset_not_found: {dataset_id}")

    def _save_listings(self, items: list[dict]) -> None:
        listingmod.save(items, self.listings_path)

    def _write_publish_log(self, dataset_id: str, name: str, enabled: bool,
                           previous: bool, note: str) -> None:
        """上下架台账：只记管理动作，不混进 runtime/ledger.jsonl。

        那本账是与源方对账、谈分成的依据，混入后台点击会把「没人问」
        包装成「有人问」。这一本单独放，运营与审计各看各的。
        """
        p = self.runtime / "publish_log.jsonl"
        row = {"at": listingmod.now_iso(), "dataset_id": dataset_id,
               "name": name, "enabled": enabled, "previous": previous,
               "note": note, "actor": "admin"}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def listing_publish(self, dataset_id: str, body: dict) -> dict:
        """上架 / 下架一个数据源。开关就是条目上的 ``published``。

        这里刻意不走 ``listingmod.save()``：那会把 normalize 的默认字段
        （linked_dataset_id / indexed / published …）重灌进全部采集条目，
        等于一次管理动作污染整份源数据。改在原始 JSON 载体上定点写三个
        字段、原子替换，其余字节保持原样。
        """
        enabled = bool(body.get("enabled", True))
        note = str(body.get("note") or ("后台上架" if enabled else "后台下架")).strip()[:200]
        with self.lock:
            p = self.listings_path
            raw = json.loads(p.read_text(encoding="utf-8"))
            hit = next((i for i in (raw.get("items") or [])
                        if i.get("dataset_id") == dataset_id), None)
            if hit is None:
                raise KeyError(f"listing_not_found: {dataset_id}")
            previous = listingmod.is_published(hit)
            hit["published"] = enabled
            hit["published_at"] = listingmod.now_iso()
            hit["publish_note"] = note
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            tmp.replace(p)
            self._write_publish_log(dataset_id, str(hit.get("name") or ""),
                                    enabled, previous, note)
            # 0.40：只重算这一行，别让下一次翻目录等 49.5s。
            self._patch_rows_cache([hit])
        return {
            "ok": True,
            "dataset_id": dataset_id,
            "published": enabled,
            "previous": previous,
            "visible_to_agents": enabled,
            "published_label": "已上架" if enabled else "已下架",
            "note": note,
        }

    def listing_publish_batch(self, body: dict) -> dict:
        """按 ids（后台当前筛选结果）或 batch（接入批次）批量切换开关。

        只写状态确实变化的条目：重复点「全部上架」不该把 531 条台账
        刷成同样的 531 行，也不该把人工改过的 publish_note 覆盖掉。
        """
        enabled = bool(body.get("enabled", True))
        note = str(body.get("note") or ("批量上架" if enabled else "批量下架")).strip()[:200]
        ids = body.get("ids")
        batch = str(body.get("batch") or "").strip()
        if not ids and not batch:
            raise ValueError("需要 ids 数组或 batch 批次名")
        wanted = {str(x) for x in ids} if isinstance(ids, (list, tuple)) else None
        if wanted is not None and len(wanted) > 5000:
            raise ValueError("单次批量不得超过 5000 条")
        with self.lock:
            p = self.listings_path
            raw = json.loads(p.read_text(encoding="utf-8"))
            stamp = listingmod.now_iso()
            matched = changed = 0
            log = []
            changed_rows = []
            for it in raw.get("items") or []:
                if wanted is not None:
                    if it.get("dataset_id") not in wanted:
                        continue
                elif it.get("onboard_batch") != batch:
                    continue
                matched += 1
                previous = listingmod.is_published(it)
                if previous == enabled and it.get("published") is not None:
                    continue
                it["published"] = enabled
                it["published_at"] = stamp
                it["publish_note"] = note
                changed += 1
                changed_rows.append(it)
                log.append((str(it.get("dataset_id") or ""),
                            str(it.get("name") or ""), previous))
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            tmp.replace(p)
            for ds_id, name, previous in log:
                self._write_publish_log(ds_id, name, enabled, previous, note)
            self._patch_rows_cache(changed_rows)
        return {"ok": True, "enabled": enabled, "matched": matched,
                "changed": changed, "unchanged": matched - changed,
                "published_label": "已上架" if enabled else "已下架",
                "note": note}

    def listings(self, filters: dict) -> dict:
        pit = pitfalls.pitfall_counts()  # 一次目录扫描，全表共用
        items = []
        for i in listingmod.load(self.listings_path):
            rec = listingmod.record(i)
            rec["pitfall_count"] = pit.get(rec["dataset_id"], 0)
            items.append(rec)
        category = (filters.get("category") or "all").strip()
        access = (filters.get("access") or "all").strip()
        q = (filters.get("q") or "").strip().lower()
        if category != "all":
            items = [i for i in items if i["category"] == category]
        if access != "all":
            items = [i for i in items if i["access_level"] == access]
        if q:
            items = [i for i in items if q in " ".join(
                [i["dataset_id"], i["name"], i["publisher"], " ".join(i["domain"]), i["notes"]]
            ).lower()]
        return {"ok": True, "items": items, "total": len(items),
                "stats": listingmod.stats(listingmod.load(self.listings_path))}

    def discover(self, body: dict) -> dict:
        query = str(body.get("query") or "")
        # 后台「发现演练」模拟的是 agent 眼中的货架，所以只读已上架条目：
        # 管理员刚点下架，演练里就该立刻看不见它，否则开关是否生效没法验。
        out = listingmod.discover(
            query, listingmod.load_visible(self.listings_path),
            min(int(body.get("limit") or 8), 30),
        )
        # 后台「演练」按钮不记账：操作员自己造的流量不该冒充读者热度。
        if not body.get("drill"):
            self.platform.log_points(
                query, [c["dataset_id"] for c in out.get("candidates") or []],
                via="admin",
            )
        return out

    def listing_check(self, dataset_id: str) -> dict:
        with self.lock:
            items = listingmod.load(self.listings_path)
            for item in items:
                if item["dataset_id"] == dataset_id:
                    item["probe"] = listingmod.probe_one(item)
                    self._save_listings(items)
                    return {"ok": True, **listingmod.detail(item)}
        raise KeyError(f"listing_not_found: {dataset_id}")

    def patrol_all(self, timeout: float = 6.0, pause: float = 0.15,
                   limit: int | None = None) -> dict:
        """每日巡检：逐条轻探针记账（streak / history），逐条落盘。

        这是路牌模式的背书来源：平台不持有数据流量，但每天亲自确认
        「路牌指向的源还通不通」。N 条条目 = N 次轻 GET，成本与数据
        体量无关——再大的数据集也不会给平台带来巡检压力。
        每条一锁一存：巡检期间后台其他操作照常可用。
        """
        items = listingmod.load(self.listings_path)
        if limit:
            items = items[:limit]
        reachable = down = missing = 0
        for target in items:
            did = target["dataset_id"]
            with self.lock:
                current = listingmod.load(self.listings_path)
                for item in current:
                    if item["dataset_id"] == did:
                        item["probe"] = listingmod.probe_one(item, timeout=timeout)
                        if item["probe"].get("upstream_status") == "reachable":
                            reachable += 1
                        else:
                            down += 1
                        break
                else:
                    missing += 1
                self._save_listings(current)
            if pause:
                time.sleep(pause)
        return {"ok": True, "total": len(items), "reachable": reachable,
                "down": down, "missing": missing,
                "checked_at": listingmod.now_iso()}

    def listing_connect(self, dataset_id: str, body: dict) -> dict:
        """已下线（0.27）：平台不再把目录条目提升为自有快照。

        这条通道会让平台落快照、建字段级索引，然后以「平台已验证」的身份
        返回值——与「指路不代取」直接冲突。实现留在 git 历史里（0.26 及
        之前）；将来若某家源方书面委托平台代取并代收费用，先把
        api.SNAPSHOT_CHANNEL 打开，再从这里恢复，且必须同时解决再分发
        授权、计价结算、精度责任三件事。
        """
        raise PermissionError(
            "snapshot_channel_off: 平台已停止把源方数据接入自有快照库。"
            "请在详情页交付接入档案（form / gate / endpoint / MCP），"
            "由 agent 直接连源方；平台只记一条使用账。"
        )

    def listing_harvest(self, dataset_id: str) -> dict:
        """打开这条目录条目背后的数据集，看真实字段，补一张实测能力卡。

        这是运营最常用的动作：源方不会来填卡，我们只能自己去看。取数放在
        锁外做——一个慢源（15 秒超时）不该把整个后台卡住。
        """
        target = self._listing(dataset_id)
        ev = harvest.resolve(target, timeout=15.0)
        with self.lock:
            items = listingmod.load(self.listings_path)
            row = next((i for i in items if i["dataset_id"] == dataset_id), None)
            if row is None:
                raise KeyError(f"dataset_not_found: {dataset_id}")
            row["harvest"] = ev
            row["capability"] = capability.declare(row)
            self._save_listings(items)
        return {
            "ok": ev.get("status") == "ok",
            "listing_id": dataset_id,
            "status": ev.get("status"),
            "status_label": harvest.STATUS_LABELS.get(ev.get("status", ""), ""),
            "harvest": capability._evidence_summary(ev),
            "attempts": ev.get("attempts") or [],
            "capability": row["capability"],
            "fields": [f.get("name") for f in (ev.get("fields") or [])][:20],
        }

    def audit(self, limit: int = 30) -> dict:
        """0.30：审计面板也改读交接账本。

        旧实现读 sqlite ``audit`` 表，而那个库在 0.28 已删除，面板会一直空着。
        字段沿用前台表格的契约；``reason=ok`` 说的是「这条交接已成功落账」，
        不是「取到了数」——0.27 后平台不代取，账本里也没有 query 原文
        （ledger 硬约束：不存用户数据）。
        """
        rows = [r for r in ledgermod.read(self.ledger_path)
                if not r.get("_corrupt")]
        items = [{"id": r.get("ref", ""), "ts": r.get("at", ""),
                  "action": r.get("action", ""), "reason": "ok",
                  "dataset_id": r.get("dataset_id", ""), "query": "",
                  "charged": 0.0, "level": r.get("level", ""),
                  "billable": bool(r.get("billable")),
                  "bill_basis": r.get("bill_basis", "")}
                 for r in list(reversed(rows))[:max(1, limit)]]
        return {"ok": True, "items": items, "source": "runtime/ledger.jsonl",
                "note": "0.27 后平台不代取：账本只记交接，不含查询原文与取值金额；"
                        "旧 sqlite 审计表已在 0.28 删除。"}

    # ---------------- onboarding ----------------

    def do_probe(self, body: dict) -> dict:
        url = str(body.get("endpoint_url") or "").strip()
        api_key = str(body.get("api_key") or "").strip() or None
        result = probemod.probe(
            url,
            api_key=api_key,
            allow_private=bool(body.get("allow_private", False)),
        )
        token = hashlib.sha256(
            (url + (api_key or "") + catalog.now_iso()).encode("utf-8")
        ).hexdigest()[:14]
        with self.lock:
            self.drafts[token] = result
        out = result.to_dict()
        out["draft_token"] = token
        out["credential_masked"] = self.mask(api_key)
        return out

    def do_install(self, body: dict) -> dict:
        """已下线（0.27）：平台不再安装「由自己持有并刷新」的数据源。

        安装动作的产物是一份 source.json + 一次全量落库，即平台持有源方
        数据。这与「只指路」冲突：源方的数据应该留在源方，接入契约交到
        agent 手里。恢复条件见 listing_connect 的说明。
        """
        raise PermissionError(
            "snapshot_channel_off: 平台已停止安装自有数据源（不再落库）。"
            "若是想把一个新源上架，请走目录条目：填接入档案（form / gate / "
            "endpoint / MCP / 计费口径），由 agent 直接连源方。"
        )

    def check(self, dataset_id: str, body: dict) -> dict:
        with self.lock:
            src, _ = self._find(dataset_id)
            row = self._rows().get(dataset_id)
            creds = self.credentials()
            res = catalog.check_dataset(
                src,
                row,
                creds.get(dataset_id, {}).get("api_key"),
                allow_private=bool(body.get("allow_private", False)),
            )
            res["credential_configured"] = bool(creds.get(dataset_id, {}).get("api_key"))
            self.health[dataset_id] = res
            self._save_health()
        return res

    def check_all(self, body: dict) -> dict:
        with self.lock:
            out = []
            for p in sorted(self.sources.glob("*/source.json")):
                ds_id = p.parent.name
                try:
                    out.append(self.check(ds_id, body))
                except Exception as exc:  # noqa: BLE001
                    out.append({"ok": False, "dataset_id": ds_id, "error": str(exc)})
        return {"ok": True, "items": out, "total": len(out)}

    def rebuild(self) -> dict:
        with self.lock:
            built = self.platform.build()
            self._rows_cache = None      # 索引库变了，签名跟着变，显式丢掉更省事
            return {
                "ok": True,
                "build": built,
                "overview": catalog.overview(self._catalog_rows(), self.platform),
            }

    def toggle(self, dataset_id: str, body: dict) -> dict:
        enabled = bool(body.get("enabled", True))
        with self.lock:
            src, path = self._find(dataset_id)
            src.setdefault("catalog", {})["enabled"] = enabled
            path.write_text(json.dumps(src, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            built = self.platform.build()
        return {"ok": True, "dataset_id": dataset_id, "enabled": enabled,
                "build": {k: built[k] for k in ("datasets", "cells", "claim_mismatches")}}

    def refresh(self, dataset_id: str, body: dict) -> dict:
        """Re-pull the upstream snapshot, keep the operator's catalog decisions."""
        with self.lock:
            src, path = self._find(dataset_id)
            endpoint = (src.get("connection") or {}).get("endpoint") or src["source_url"]
            creds = self.credentials()
            result = probemod.probe(
                endpoint,
                api_key=creds.get(dataset_id, {}).get("api_key"),
                allow_private=bool(body.get("allow_private", False)),
            )
            fresh = result.draft
            keep = ("dataset_id", "name", "domain", "license", "update_frequency",
                    "representative_queries", "access", "evidence_policy", "catalog")
            for key in keep:
                fresh[key] = src[key]
            fresh["source_url"] = endpoint
            fresh["connection"] = src.get("connection", fresh["connection"])
            fresh["connection"]["last_checked"] = catalog.now_iso()
            fresh["connection"]["last_error"] = ""
            (path.parent / fresh["raw_file"]).write_bytes(result.raw_bytes)
            old_raw = src["raw_file"]
            if old_raw != fresh["raw_file"] and (path.parent / old_raw).is_file():
                (path.parent / old_raw).unlink()
            path.write_text(json.dumps(fresh, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            built = self.platform.build()
            self.health[dataset_id] = catalog.check_dataset(
                load(path), self._rows().get(dataset_id),
                creds.get(dataset_id, {}).get("api_key"),
                allow_private=bool(body.get("allow_private", False)),
            )
            self._save_health()
        return {
            "ok": True, "dataset_id": dataset_id,
            "new_sha256": fresh["sha256"], "records": fresh["coverage"]["declared_records"],
            "build": {k: built[k] for k in ("datasets", "cells", "claim_mismatches")},
        }

    def query(self, body: dict) -> dict:
        with self.lock:
            return self.platform.answer(
                str(body.get("query") or ""),
                dataset_id=body.get("dataset_id") or None,
                payment_token=body.get("payment_token") or None,
            )

    def grant(self, body: dict) -> dict:
        with self.lock:
            token = self.platform.grant(str(body.get("invoice_id") or ""), str(body.get("dataset_id") or ""))
        return {"ok": True, "token": token, "mode": self.payments.mode,
                "sandbox": self.payments.is_sandbox}

    # ---------------- router ----------------

    def dispatch(
        self, method: str, path: str, query: dict, body: dict,
        headers=None, raw_body: bytes | None = None,
    ) -> tuple[int, dict]:
        parts = [p for p in path.split("/") if p]
        try:
            # 控制台心跳是生命周期信号，必须公开且先于任何鉴权/支付重载
            if path.startswith("/api/console/"):
                return self.console_dispatch(method, path, body)
            # CLI pay-config writes the same file; make it effective without a restart
            if path.startswith("/api/pay/") and not (
                path == "/api/pay/config" and method == "POST"
            ):
                self.payments.reload()
            # auth endpoints are public by design
            if method == "GET" and path == "/api/auth/status":
                return _json_response(200, self.auth_status(headers or {}, query.get("secure")))
            if method == "POST" and path == "/api/auth/login":
                return self.auth_login(body, headers or {}, str(query.get("_client_ip") or "local"),
                                       bool(query.get("secure")))
            if method == "POST" and path == "/api/auth/logout":
                return self.auth_logout(headers or {}, bool(query.get("secure")))
            # signed payment callback is authenticated by its HMAC, not a session
            if method == "POST" and path == "/api/pay/webhook":
                return self.pay_webhook(raw_body or b"{}", headers or {})

            if method == "GET" and path == "/api/overview":
                return _json_response(200, self.overview())
            if method == "GET" and path == "/api/categories":
                return _json_response(200, {"ok": True, "items": catalog.CATEGORIES})
            if method == "GET" and path == "/api/api-doc":
                return _json_response(200, {"ok": True, "items": API_DOC,
                                            "base_url": self.payments.base_url})
            if method == "GET" and path == "/api/datasets":
                return _json_response(200, self.datasets(query))
            if method == "GET" and path == "/api/listings":
                return _json_response(200, self.listings(query))
            if method == "POST" and path == "/api/listings/publish-batch":
                return _json_response(200, self.listing_publish_batch(body))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "listings"]:
                item = self._listing(urllib.parse.unquote(parts[2]))
                return _json_response(200, listingmod.detail(item))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "pitfalls"]:
                return _json_response(200, self.pitfall_detail(urllib.parse.unquote(parts[2])))
            if method == "POST" and path == "/api/discover":
                return _json_response(200, self.discover(body))
            if method == "GET" and path == "/api/audit":
                return _json_response(200, self.audit(int(query.get("limit", 30))))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "datasets"]:
                return _json_response(200, self.dataset_detail(urllib.parse.unquote(parts[2])))
            if method == "POST" and path == "/api/datasets/probe":
                return _json_response(200, self.do_probe(body))
            if method == "POST" and path == "/api/datasets":
                return _json_response(201, self.do_install(body))
            if method == "POST" and path == "/api/check-all":
                return _json_response(200, self.check_all(body))
            if method == "POST" and path == "/api/rebuild":
                return _json_response(200, self.rebuild())
            if method == "POST" and path == "/api/query":
                # 0.27：这条路会走 route → proof → 门禁 → 取值，即平台代取。
                return _json_response(410, {
                    "ok": False,
                    "reason": "snapshot_channel_off",
                    "detail": ("已下线「查询演练」：它走的是 route → 证据 → 门禁 → 取值，"
                               "即平台替 agent 取数。"),
                    "NEXT": "打开一个目录条目看接入档案；agent 侧用 dp_discover → dp_usage。",
                })
            if method == "POST" and path == "/api/grant":
                return _json_response(200, self.grant(body))

            # payment channel
            if path == "/api/pay/config" and method == "GET":
                return _json_response(200, self.payments.public_config())
            if path == "/api/pay/config" and method == "POST":
                return _json_response(200, self.payments.update(body))
            if method == "GET" and len(parts) == 4 and parts[:3] == ["api", "pay", "invoice"]:
                return _json_response(200, self.pay_invoice(urllib.parse.unquote(parts[3])))
            if method == "POST" and path == "/api/pay/confirm":
                return _json_response(200, self.pay_confirm(body))

            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "datasets"]:
                ds_id = urllib.parse.unquote(parts[2])
                action = parts[3]
                if action == "check":
                    return _json_response(200, self.check(ds_id, body))
                if action == "refresh":
                    # 刷新 = 从上游重拉快照，也就是平台又代取了一次。0.27 关。
                    return _json_response(410, {
                        "ok": False,
                        "reason": "snapshot_channel_off",
                        "detail": ("已下线「从上游重拉快照」：平台不再代取源方数据。"
                                   "已索引条目仅作为历史快照只读保留，不再刷新。"),
                    })
                if action == "toggle":
                    return _json_response(200, self.toggle(ds_id, body))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "listings"]:
                ds_id = urllib.parse.unquote(parts[2])
                if parts[3] in ("publish", "toggle"):
                    return _json_response(200, self.listing_publish(ds_id, body))
                if parts[3] == "check":
                    return _json_response(200, self.listing_check(ds_id))
                if parts[3] == "connect":
                    # 0.27：平台默认只指路，不再把源方数据拉进自己的快照库。
                    # 这里保留一个明确的 410，而不是 404——老书签/脚本会看到
                    # 「这条通道被关掉了」，而不是「接口拼错了」。
                    return _json_response(410, {
                        "ok": False,
                        "reason": "snapshot_channel_off",
                        "detail": ("已下线「一键接入为真实快照」：平台不再拉取源方数据入库、"
                                   "不再代 agent 取数。"),
                        "NEXT": ("在详情页把接入档案（form / gate / endpoint / MCP）交给 agent，"
                                 "由 agent 直接连源方；平台只记一条使用账。"),
                    })
                if parts[3] == "harvest":
                    return _json_response(200, self.listing_harvest(ds_id))
            return _json_response(404, {"ok": False, "reason": "not_found", "path": path})
        except KeyError as exc:
            return _json_response(404, {"ok": False, "reason": "not_found", "detail": str(exc)})
        except (ValueError, FileNotFoundError) as exc:
            return _json_response(400, {"ok": False, "reason": "bad_request", "detail": str(exc)})
        except PermissionError as exc:
            return _json_response(403, {"ok": False, "reason": "forbidden", "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return _json_response(
                500, {"ok": False, "reason": "internal_error", "detail": f"{type(exc).__name__}: {exc}"}
            )


STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/logo.svg": ("logo.svg", "image/svg+xml"),
    "/pay.js": ("pay.js", "application/javascript; charset=utf-8"),
}
PUBLIC_API = {
    "/api/auth/status", "/api/auth/login", "/api/auth/logout",
    "/api/pay/webhook", "/api/pay/confirm",
    "/api/console/status", "/api/console/heartbeat", "/api/console/leave",
    "/api/console/stop",
}


# 客户端在响应写完前离开时（刷新、切页、关标签、切网），wfile.write 会抛
# ConnectionAbortedError（Windows 10053）/ ConnectionResetError（10054）/
# BrokenPipeError。这不是服务端故障——请求已经处理完，只是没人收结果；但
# socketserver 默认会把整段 traceback 打到 stderr，控制台每轮询一次就刷几十行，
# 真故障反而被埋掉。同族异常统一静默，只留一条计数摘要。
CLIENT_GONE_TOTAL = 0


def _client_gone(exc: BaseException | None) -> bool:
    """这条异常是不是「客户端走了」，而不是服务端出错。"""
    return isinstance(exc, (ConnectionError, TimeoutError))


def _note_client_gone(path: str = "") -> None:
    """计数并偶尔报一行——静默不等于装看不见，断开次数本身是有用的信号。"""
    global CLIENT_GONE_TOTAL
    CLIENT_GONE_TOTAL += 1
    n = CLIENT_GONE_TOTAL
    if n == 1 or n % 200 == 0:
        sys.stderr.write("  ! 客户端提前断开 %d 次（最近 %s），已静默忽略\n"
                         % (n, path or "-"))
        sys.stderr.flush()


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "DatasetPlazz/0.4"

    @property
    def service(self) -> AdminService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def secure(self) -> bool:
        return bool(getattr(self.server, "secure", False))

    def log_message(self, fmt, *args):  # keep the console readable
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def _send(self, status: int, payload: bytes, ctype: str, extra=None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
            for item in extra or []:
                if isinstance(item, (tuple, list)):
                    self.send_header(str(item[0]), str(item[1]))
                else:
                    self.send_header("Set-Cookie", str(item))
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionError, TimeoutError):
            # 响应没送达：连接已经没有对端，继续写只会二次报错。标记关闭，
            # 让 handle_one_request 正常收尾；不往外抛，也就不会走到
            # socketserver.handle_error 打 traceback。
            self.close_connection = True
            _note_client_gone(self.path or "")

    def handle_one_request(self) -> None:
        # _send 已经兜住写响应；这里再兜一次，因为 BaseHTTPRequestHandler
        # 收尾还会 self.wfile.flush()，客户端恰好在这时消失同样会抛。
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            self.close_connection = True
            _note_client_gone(getattr(self, "path", "") or "")

    def _json(self, status: int, obj: dict, cookies=None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", cookies)

    def _read_raw(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if path == "/pay" and method == "GET":
            html = self.service.pay_checkout(str(query.get("invoice_id") or ""))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path.startswith("/api/"):
            raw = b""
            body: dict = {}
            try:
                if method == "POST":
                    raw = self._read_raw()
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                    if not isinstance(body, dict):
                        body = {}
            except (ValueError, UnicodeDecodeError) as exc:
                self._json(400, {"ok": False, "reason": "bad_request", "detail": str(exc)})
                return

            is_public = path in PUBLIC_API or (
                method == "GET" and path.startswith("/api/pay/invoice/")
            )
            if not is_public:
                allowed, reason, _ = self.server.auth.check(self.headers, method)
                if not allowed:
                    self._json(401, {
                        "ok": False,
                        "reason": reason,
                        "hint": "先 POST /api/auth/login 拿会话；浏览器写操作还需 X-DP-CSRF 头",
                        "https": self.secure,
                    })
                    return

            q = dict(query)
            q["secure"] = "1" if self.secure else ""
            q["_client_ip"] = self.client_address[0] if self.client_address else "local"
            status, payload = self.service.dispatch(
                method, path, q, body, headers=self.headers, raw_body=raw
            )
            cookies = payload.pop("cookie_headers", []) if isinstance(payload, dict) else []
            self._json(status, payload, cookies)
            return

        entry = STATIC.get(path)
        if method == "GET" and entry:
            filename, ctype = entry
            fp = UI_DIR / filename
            if fp.is_file():
                raw = fp.read_bytes()
                if filename == "index.html":
                    token = self.service.console.token if self.service.console else None
                    raw = inject_console_token(raw, token)
                self._send(200, raw, ctype)
                return
        self._json(404, {"ok": False, "reason": "not_found", "path": path})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class AdminServer(ThreadingHTTPServer):
    """控制后台的 HTTP 服务。与裸 ThreadingHTTPServer 只差一件事：
    客户端断连不算服务端错误，不往 stderr 打 traceback。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if _client_gone(exc):
            _note_client_gone("")
            return
        super().handle_error(request, client_address)


def serve(
    host: str = "127.0.0.1", port: int = 8787, sources=None, db=None, runtime=None,
    auth: bool = True, password: str | None = None, https: bool = False,
    cert: str | None = None, key: str | None = None, listings=None,
    console: ConsoleSession | None = None,
) -> None:
    service = AdminService(sources=sources, db=db, runtime=runtime,
                           auth_enabled=auth, password=password, listings=listings,
                           console=console)
    httpd = AdminServer((host, port), AdminHandler)
    httpd.service = service
    httpd.auth = service.auth
    httpd.secure = bool(https)

    def _graceful_stop():
        timer = threading.Timer(0.3, httpd.shutdown)
        timer.daemon = True
        timer.start()

    service.set_stop_callback(_graceful_stop)
    if https:
        if cert and key:
            cert_path, key_path = Path(cert), Path(key)
            if not cert_path.is_file() or not key_path.is_file():
                raise FileNotFoundError("--cert / --key 指向的文件不存在")
        else:
            cert_path, key_path = tls.ensure_self_signed(
                Path(runtime or DEFAULT_RUNTIME) / "tls"
            )
        tls.wrap_server(httpd, cert_path, key_path)
    scheme = "https" if https else "http"
    service.set_base_url(f"{scheme}://{host}:{port}")
    print(f"Dataset Plazz 控制后台: {scheme}://{host}:{port}/")
    print(f"  sources = {service.sources}")
    print(f"  index   = {service.db}")
    print(f"  runtime = {service.runtime}  (凭证不写入公开契约)")
    print(f"  listings= {service.listings_path}  ({service.listings_count()} 条目录条目)")
    print(f"  鉴权    = {'开启' if auth else '关闭（仅离线调试）'}  "
          f"HTTPS={'开启（自签证书）' if https else '关闭'}")
    if auth and service.auth.source == "runtime/admin_password.txt":
        print(f"  管理员密码 = {service.auth.password_hint}")
        print(f"               （也可读 {service.runtime / 'admin_password.txt'}）")
    print(f"  支付通道 = {service.payments.mode} / {service.payments.config['provider']}")
    # 目录行预热：首页要的是 3.5 万条排好的行，冷启动一次 47s。放到后台线程，
    # 别让第一个页面请求去吃这段时间；有落盘缓存时这里只是一次反序列化。
    threading.Thread(target=_warm_rows, args=(service,), daemon=True,
                     name="catalog-warm").start()

    if console is not None:
        print(f"  控制台联动 = 已绑定（会话 {console.token[:8]}；"
              f"心跳停 {console.idle:g}s / 页面离开 {console.leave_grace:g}s 后自动退出）")
        threading.Thread(target=console_lifetime, args=(console, httpd.shutdown),
                         daemon=True, name="console-lifetime").start()
    else:
        print("  控制台联动 = 关闭（常驻后端，Ctrl+C 停止）")

    # 巡检保鲜（路牌模式）：启动即查新鲜度，数据旧于阈值立即巡检；
    # 运行中每满 DP_PATROL_INTERVAL_H 小时再触发。DP_PATROL_OFF=1 关闭。
    if os.environ.get("DP_PATROL_OFF", "").strip() != "1":
        try:
            interval_h = float(os.environ.get("DP_PATROL_INTERVAL_H", "24"))
        except ValueError:
            interval_h = 24.0
        try:
            startup_age_h = float(os.environ.get("DP_PATROL_STARTUP_MAX_AGE_H", str(interval_h)))
        except ValueError:
            startup_age_h = interval_h
        threading.Thread(target=_patrol_loop,
                         args=(service, interval_h, startup_age_h),
                         daemon=True, name="patrol-freshness").start()
        print(f"  巡检保鲜 = 开启（启动时数据旧于 {startup_age_h:g}h 立即巡检；"
              f"运行中每满 {interval_h:g}h 再巡检；DP_PATROL_OFF=1 可关）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if console is not None:
            console.stop_event.set()
        httpd.server_close()
        service.platform.close()


def _warm_rows(service: "AdminService") -> None:
    """后台预热目录行；失败只报一行，绝不影响服务本身。"""
    try:
        secs = service.warm_catalog_rows()
    except Exception as exc:  # noqa: BLE001 - 预热是加速件，不能拖垮后台
        print(f"[catalog] 预热失败（不影响服务）: {type(exc).__name__}: {exc}",
              flush=True)
        return
    if secs >= 1.0:
        print(f"[catalog] 目录行缓存就绪（{secs:.1f}s）", flush=True)


def _patrol_age_seconds(service: "AdminService") -> float | None:
    """货架上最近一次巡检距今的秒数；从未巡检过返回 None。

    以 listings 中最大的 probe.checked_at（UTC ISO）为准——不加新状态文件，
    巡检记录本身就是唯一事实源。解析失败的时间戳跳过，不污染判定。
    """
    latest: datetime | None = None
    try:
        for it in listingmod.load(service.listings_path):
            ca = (it.get("probe") or {}).get("checked_at")
            if not isinstance(ca, str) or not ca:
                continue
            try:
                t = datetime.fromisoformat(ca)
            except ValueError:
                continue  # 垃圾时间戳：跳过，不影响其余条目判定
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if latest is None or t > latest:
                latest = t
    except Exception:  # noqa: BLE001 - 读不了货架按"从未巡检"处理
        return None
    if latest is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - latest).total_seconds())


def _patrol_loop(service: "AdminService", interval_h: float = 24.0,
                 startup_max_age_h: float = 24.0, tick_s: float = 300.0) -> None:
    """巡检保鲜线程（路牌模式的背书来源）。

    规则（操作员定稿）：
      1) 后端启动时检查巡检数据年龄——从未巡检、或距上次巡检超过
         startup_max_age_h 小时，立即跑一轮（后台线程，不阻塞服务启动）；
      2) 运行中每当巡检数据年龄超过 interval_h 小时，自动再触发一轮。
    由此保证：后端每次被启动后，货架背书状态都是保鲜的；长期运行也不会过期。
    巡检失败绝不影响服务本身。
    """
    interval = max(0.1, float(interval_h)) * 3600.0
    startup_max = max(0.1, float(startup_max_age_h)) * 3600.0
    first = True
    while True:
        age = _patrol_age_seconds(service)
        threshold = startup_max if first else interval
        due = age is None or age >= threshold
        if due:
            if age is None:
                reason = "启动保鲜巡检（货架上还没有巡检记录）"
            else:
                when = "启动保鲜巡检" if first else "运行保鲜巡检"
                reason = f"{when}（距上次巡检 {age / 3600.0:.1f}h ≥ 阈值 {threshold / 3600.0:g}h）"
            try:
                res = service.patrol_all()
                print(f"[patrol] {reason}: {json.dumps(res, ensure_ascii=False)}", flush=True)
            except Exception as exc:  # noqa: BLE001 - 巡检失败绝不能拖垮后台
                print(f"[patrol] {reason} failed: {type(exc).__name__}: {exc}", flush=True)
        elif first:
            print(f"[patrol] 启动免检：上次巡检距今 {age / 3600.0:.1f}h "
                  f"（< {startup_max_age_h:g}h 保鲜窗口）", flush=True)
        first = False
        time.sleep(max(60.0, tick_s))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="datasetplazz-admin", description="Dataset Plazz control plane")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--sources", default=str(DEFAULT_SOURCES))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    ap.add_argument("--listings", default=str(DEFAULT_LISTINGS))
    ap.add_argument("--no-auth", dest="auth", action="store_false", default=True,
                    help="关闭登录鉴权（仅离线本机调试）")
    ap.add_argument("--password", default=None, help="指定管理员密码（默认自动生成）")
    ap.add_argument("--https", action="store_true", help="启用 TLS（自动生成 localhost 自签证书）")
    ap.add_argument("--cert", default="", help="CA 签发的证书 PEM 路径")
    ap.add_argument("--key", default="", help="证书私钥 PEM 路径")
    ap.add_argument("--console-token", default="",
                    help="绑定控制台会话；页面心跳停止后后端自行退出")
    ap.add_argument("--console-idle", type=float, default=20.0)
    ap.add_argument("--console-boot-grace", type=float, default=120.0)
    args = ap.parse_args(argv)
    console = ConsoleSession(args.console_token, args.console_idle,
                             args.console_boot_grace) if args.console_token else None
    serve(args.host, args.port, args.sources, args.db, args.runtime,
          auth=args.auth, password=args.password, https=args.https,
          cert=args.cert or None, key=args.key or None, listings=args.listings,
          console=console)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
