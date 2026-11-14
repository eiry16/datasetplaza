"""Payment / authorization channel: 402 -> checkout -> signed receipt -> token.

Two modes, both honest about what they are:

* ``sandbox``  (default) - a LOCAL, reachable checkout page. No money moves.
  The invoice link is no longer a dead ``*.example.invalid`` placeholder: an
  agent can open it, confirm, and obtain a real (short-lived) access token.
* ``external`` - the invoice link points at an operator-configured payment
  provider. The only way to mint a token is ``POST /api/pay/webhook`` with a
  valid HMAC-SHA256 signed receipt. A bare ``grant()`` is refused.

This module does NOT pretend to integrate with WeChat Pay / Alipay / Stripe.
Those need merchant credentials and a server-side order API. What is provided
here is the complete, verifiable *interface* a real processor plugs into:
per-invoice checkout URL, amount/currency binding, timestamped signed webhook,
nonce replay protection, and invoice-bound tokens. Configure
``checkout_url_template`` + ``webhook_secret`` in runtime/payments.json (or via
``datasetplazz pay-config``) to switch it on.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULTS = {
    "mode": "sandbox",
    "provider": "local-sandbox",
    "checkout_url_template": "",
    "webhook_secret": "",
    "token_ttl_seconds": 900,
    "callback_path": "/api/pay/webhook",
}
MODES = {"sandbox", "external"}
WRITABLE = ("mode", "provider", "checkout_url_template", "webhook_secret", "token_ttl_seconds")
RECEIPT_STATUSES = {"paid", "authorized", "succeeded", "success"}
SIGNATURE_WINDOW = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaymentGateway:
    def __init__(self, runtime=None, base_url: str = "http://127.0.0.1:8787", config=None):
        self.runtime = Path(runtime) if runtime else None
        self.path = self.runtime / "payments.json" if self.runtime else None
        self.base_url = str(base_url).rstrip("/")
        self.config = dict(DEFAULTS)
        if self.path and self.path.is_file():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.config.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except (OSError, json.JSONDecodeError):
                pass
        env_mode = os.environ.get("DATASETPLAZZ_PAY_MODE")
        env_secret = os.environ.get("DATASETPLAZZ_PAY_SECRET")
        env_url = os.environ.get("DATASETPLAZZ_PAY_CHECKOUT_URL")
        if env_mode:
            self.config["mode"] = env_mode
        if env_secret:
            self.config["webhook_secret"] = env_secret
        if env_url:
            self.config["checkout_url_template"] = env_url
        if config:
            self.config.update({k: v for k, v in config.items() if k in DEFAULTS})
        if self.config["mode"] not in MODES:
            self.config["mode"] = "sandbox"
        self._nonces: dict[str, float] = {}
        self.lock = threading.RLock()

    # ---------------- configuration ----------------

    @property
    def mode(self) -> str:
        return self.config["mode"]

    @property
    def is_external(self) -> bool:
        return self.mode == "external"

    @property
    def is_sandbox(self) -> bool:
        return self.mode == "sandbox"

    @property
    def secret(self) -> str:
        return str(self.config.get("webhook_secret") or "")

    @property
    def webhook_url(self) -> str:
        return self.base_url + str(self.config.get("callback_path") or "")

    @property
    def token_ttl(self) -> int:
        try:
            return max(60, int(self.config["token_ttl_seconds"]))
        except (TypeError, ValueError):
            return 900

    def public_config(self) -> dict:
        """Never exposes webhook_secret, not even masked."""
        return {
            "ok": True,
            "mode": self.mode,
            "provider": self.config["provider"],
            "sandbox": self.is_sandbox,
            "checkout_template_configured": bool(self.config["checkout_url_template"]),
            "webhook_secret_configured": bool(self.secret),
            "webhook_url": self.webhook_url,
            "token_ttl_seconds": self.token_ttl,
            "callback_path": self.config["callback_path"],
            "signature": "HMAC-SHA256(secret, f'{timestamp}.{nonce}.' + raw_body)",
            "required_headers": ["X-DP-Timestamp", "X-DP-Nonce", "X-DP-Signature"],
            "note": (
                "external 模式：只有验签通过的 webhook 才能签发 token；平台不接触资金"
                if self.is_external else
                "sandbox 模式：本地模拟收银台，不产生真实扣款"
            ),
        }

    def reload(self) -> dict:
        """Pick up runtime/payments.json written by the CLI while the server runs."""
        if not self.path or not self.path.is_file():
            return self.public_config()
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.public_config()
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
        for key, env_name in (
            ("mode", "DATASETPLAZZ_PAY_MODE"),
            ("webhook_secret", "DATASETPLAZZ_PAY_SECRET"),
            ("checkout_url_template", "DATASETPLAZZ_PAY_CHECKOUT_URL"),
        ):
            if os.environ.get(env_name):
                merged[key] = os.environ[env_name]
        if merged["mode"] not in MODES:
            merged["mode"] = "sandbox"
        self.config = merged
        return self.public_config()

    def update(self, body: dict) -> dict:
        """Operator-configured switch. Validates before persisting."""
        incoming = {k: body[k] for k in WRITABLE if k in body}
        merged = {**self.config, **incoming}
        mode = merged.get("mode")
        if mode not in MODES:
            raise ValueError("mode 只能是 sandbox 或 external")
        if mode == "external":
            template = str(merged.get("checkout_url_template") or "")
            if "{invoice_id}" not in template or not template.startswith("https://"):
                raise ValueError(
                    "external 模式需要 https 的 checkout_url_template，且必须含 {invoice_id}"
                )
            if len(str(merged.get("webhook_secret") or "")) < 16:
                raise ValueError("external 模式需要至少 16 字符的 webhook_secret")
        try:
            merged["token_ttl_seconds"] = max(60, int(merged["token_ttl_seconds"]))
        except (TypeError, ValueError):
            raise ValueError("token_ttl_seconds 必须是整数")
        self.config = {k: merged.get(k, DEFAULTS[k]) for k in DEFAULTS}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return self.public_config()

    # ---------------- invoice links ----------------

    def invoice_links(self, invoice: dict) -> dict:
        """Attach a reachable, invoice-specific checkout/authorization link."""
        iid = invoice["invoice_id"]
        declared_payment = invoice.get("payment_url", "")
        declared_auth = invoice.get("auth_url", "")
        fields = {
            "invoice_id": iid,
            "dataset_id": invoice["dataset_id"],
            "amount": (invoice.get("price") or {}).get("amount", 0),
            "currency": (invoice.get("price") or {}).get("currency", "CNY"),
            "access_level": invoice["access_level"],
        }
        if self.is_external:
            template = str(self.config["checkout_url_template"])
            try:
                link = template.format(**fields)
            except (KeyError, IndexError) as exc:
                raise ValueError(f"checkout_url_template 含未知占位符: {exc}")
            payment_url = link
            auth_url = link
            provider = str(self.config["provider"] or "external-gateway")
        else:
            payment_url = f"{self.base_url}/pay?invoice_id={iid}"
            auth_url = payment_url
            provider = "local-sandbox"
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.token_ttl)
        return {
            "payment_url": payment_url,
            "auth_url": auth_url,
            "declared_payment_url": declared_payment,
            "declared_auth_url": declared_auth,
            "payment_provider": provider,
            "payment_mode": self.mode,
            "sandbox": self.is_sandbox,
            "expires_at": expires.isoformat(timespec="seconds"),
        }

    # ---------------- signed webhook ----------------

    def sign(self, raw_body: bytes, timestamp: int, nonce: str, secret: str | None = None) -> str:
        key = secret if secret is not None else self.secret
        message = f"{timestamp}.{nonce}.".encode("ascii") + raw_body
        return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()

    def verify(self, raw_body: bytes, headers) -> tuple[bool, str, dict | None]:
        """Validate a provider callback. Returns (ok, reason, payload)."""
        if not self.secret:
            return False, "webhook_secret_not_configured", None
        ts_raw = headers.get("X-DP-Timestamp") or ""
        nonce = headers.get("X-DP-Nonce") or ""
        signature = headers.get("X-DP-Signature") or ""
        if not ts_raw or not nonce or not signature:
            return False, "missing_signature_headers", None
        try:
            ts = int(ts_raw)
        except ValueError:
            return False, "bad_timestamp", None
        if abs(time.time() - ts) > SIGNATURE_WINDOW:
            return False, "timestamp_out_of_window", None
        expected = self.sign(raw_body, ts, nonce)
        if not hmac.compare_digest(expected, str(signature)):
            return False, "bad_signature", None
        with self.lock:
            now = time.time()
            for key, exp in list(self._nonces.items()):
                if exp < now:
                    self._nonces.pop(key, None)
            if nonce in self._nonces:
                return False, "nonce_replayed", None
            self._nonces[nonce] = now + SIGNATURE_WINDOW
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, "bad_json", None
        if not isinstance(payload, dict):
            return False, "bad_payload", None
        if str(payload.get("status", "")).lower() not in RECEIPT_STATUSES:
            return False, "receipt_not_settled", None
        payload["_verified"] = True
        payload["_receipt_id"] = "rcpt_" + secrets.token_hex(8)
        payload["_receipt_nonce"] = nonce
        return True, "ok", payload


def checkout_html(invoice: dict, gateway: PaymentGateway) -> str:
    """Local sandbox checkout / authorization page. Reachable, clearly marked."""
    price = invoice.get("price") or {}
    sandbox = gateway.is_sandbox
    kind = "支付" if invoice.get("access_level") == "paid" else "授权"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dataset Plazz · 收银台</title>
<link rel="stylesheet" href="/style.css"></head>
<body data-theme="light">
<div class="pay-wrap">
  <div class="panel pay-card">
    <div class="pay-head"><img src="/logo.svg" width="34" height="34" alt="">
      <div><h1>Dataset Plazz</h1><p class="muted">凭证核销 · {esc(kind)}确认</p></div></div>
    <div class="note {'info' if sandbox else ''}" id="mode-note">
      {'沙盒模拟收银台：不会产生真实扣款，仅用于验证 402 → 付款 → 取值 闭环。'
       if sandbox else '外部支付网关模式：请在支付服务商处完成付款，回调验签后由 agent 重试查询。'}
    </div>
    <div class="kv" style="margin:18px 0">
      <div><div class="k">发票号</div><div class="v mono" id="f-invoice">{esc(invoice['invoice_id'])}</div></div>
      <div><div class="k">数据集</div><div class="v mono" id="f-dataset">{esc(invoice['dataset_id'])}</div></div>
      <div><div class="k">金额</div><div class="v" id="f-amount">¥{esc(price.get('amount', 0))} {esc(price.get('currency','CNY'))}</div></div>
      <div><div class="k">通道</div><div class="v" id="f-provider">{esc(invoice.get('payment_provider',''))}</div></div>
      <div><div class="k">有效期</div><div class="v mono" id="f-expires">{esc(invoice.get('expires_at',''))}</div></div>
      <div><div class="k">证据</div><div class="v">已在付款前免费提供，可回查 citation 与快照哈希</div></div>
    </div>
    <p class="muted" style="font-size:12px">空命中不计费；本发票只对该数据集、该快照有效。</p>
    <button class="btn primary" id="btn-confirm" {'' if sandbox else 'disabled'}>确认{esc(kind)}并签发凭证</button>
    <pre class="code hidden" id="pay-result" style="margin-top:16px"></pre>
  </div>
</div>
<script src="/pay.js"></script>
</body></html>"""


def esc(value) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )
