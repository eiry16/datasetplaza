"""Access gate: proof is free, value is paid, zero hits are never charged.

Order matters. The no-hit check comes FIRST, so a paid dataset can never
present a payment link for a question it cannot answer.

Tokens are stateless HMAC-signed receipts bound to (invoice_id, dataset_id)
with an expiry, using a secret shared via runtime/token_secret.txt. That means
a token minted by the admin console's webhook also works in the MCP server
process - without a shared in-memory dict and without trusting the caller.

In ``external`` payment mode, grant() refuses any unsigned receipt. Only a
webhook that passed HMAC verification in payments.PaymentGateway can mint a
token. In ``sandbox`` mode, the local (clearly-labelled) checkout page may do
so to exercise the loop end to end.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from .errors import fail, ok
from .payments import PaymentGateway

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_TTL = 900


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Gate:
    def __init__(self, payments: PaymentGateway | None = None, runtime=None):
        self.payments = payments or PaymentGateway(runtime=runtime)
        self.runtime = Path(runtime or _ROOT / "runtime")
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.invoice_dir = self.runtime / "invoices"
        self.invoice_dir.mkdir(parents=True, exist_ok=True)
        self.secret_path = self.runtime / "token_secret.txt"
        self._secret = self._load_secret()
        self._invoices: dict[str, dict] = {}

    # ---------------- shared secret (cross-process) ----------------

    def _load_secret(self) -> str:
        if self.secret_path.is_file():
            value = self.secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_hex(32)
        try:
            self.secret_path.write_text(value + "\n", encoding="utf-8")
        except OSError:
            pass
        if self.secret_path.is_file():
            on_disk = self.secret_path.read_text(encoding="utf-8").strip()
            if on_disk:
                return on_disk
        return value

    def _signature(self, body: str) -> str:
        return hmac.new(
            self._secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()[:32]

    def _mint(self, invoice_id: str, dataset_id: str) -> str:
        ttl = self.payments.token_ttl
        payload = {
            "invoice_id": invoice_id,
            "dataset_id": dataset_id,
            "exp": int(time.time()) + ttl,
            "n": secrets.token_hex(6),
        }
        body = _b64e(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        return "bdsp_" + body + "." + self._signature(body)

    def _decode(self, token: str | None, dataset_id: str) -> dict | None:
        if not token or not token.startswith("bdsp_"):
            return None
        try:
            body, sig = token[5:].split(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._signature(body)):
            return None
        try:
            payload = json.loads(_b64d(body).decode("utf-8"))
        except Exception:  # noqa: BLE001 - any decode failure is an invalid token
            return None
        if payload.get("dataset_id") != dataset_id:
            return None
        if float(payload.get("exp", 0)) <= time.time():
            return None
        return payload

    # ---------------- invoices ----------------

    def _persist_invoice(self, invoice: dict) -> None:
        self._invoices[invoice["invoice_id"]] = invoice
        try:
            path = self.invoice_dir / (invoice["invoice_id"] + ".json")
            path.write_text(
                json.dumps(invoice, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            files = sorted(self.invoice_dir.glob("inv_*.json"), key=lambda p: p.stat().st_mtime)
            for old in files[:-200]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    def get_invoice(self, invoice_id: str) -> dict | None:
        if invoice_id in self._invoices:
            return self._invoices[invoice_id]
        if not invoice_id.startswith("inv_") or "/" in invoice_id or "\\" in invoice_id:
            return None
        path = self.invoice_dir / (invoice_id + ".json")
        try:
            invoice = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        self._invoices[invoice_id] = invoice
        return invoice

    def make_invoice(self, ds, query: str) -> dict:
        seed = f"{ds['dataset_id']}|{query}|{ds['sha256']}"
        invoice_id = "inv_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        acc = json.loads(ds["access_json"])
        invoice = {
            "invoice_id": invoice_id,
            "dataset_id": ds["dataset_id"],
            "query": query,
            "access_level": ds["access_level"],
            "price": acc.get("price", {}),
            "payment_url": acc.get("payment_url", ""),
            "auth_url": acc.get("auth_url", ""),
        }
        invoice.update(self.payments.invoice_links(invoice))
        self._persist_invoice(invoice)
        return invoice

    # ---------------- grant / authorization ----------------

    def grant(
        self,
        invoice_id: str,
        dataset_id: str,
        receipt: dict | None = None,
        con=None,
    ) -> str:
        """Mint a token after a settled payment/authorization receipt.

        sandbox : local simulated callback is allowed (explicitly labelled).
        external: receipt MUST carry the server-injected ``_verified`` flag
                  and match the invoice's dataset, amount, currency and hash.
        """
        if self.payments.is_external:
            self._require_receipt(invoice_id, dataset_id, receipt, con)
        return self._mint(invoice_id, dataset_id)

    def _require_receipt(self, invoice_id, dataset_id, receipt, con) -> None:
        if not receipt or receipt.get("_verified") is not True:
            raise PermissionError("receipt_required: external 模式必须由验签 webhook 签发凭证")
        if receipt.get("invoice_id") != invoice_id or receipt.get("dataset_id") != dataset_id:
            raise PermissionError("receipt_binding_mismatch")
        if con is None:
            raise PermissionError("index_missing: 无法复核发票")
        ds = con.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
        if ds is None:
            raise PermissionError("dataset_not_found")
        expected = self.make_invoice(ds, str(receipt.get("query") or ""))["invoice_id"]
        if expected != invoice_id:
            raise PermissionError("invoice_mismatch: 发票与该查询/快照不匹配")
        acc = json.loads(ds["access_json"])
        price = acc.get("price", {})
        if ds["access_level"] == "paid":
            try:
                amount = float(receipt.get("amount"))
            except (TypeError, ValueError):
                raise PermissionError("receipt_amount_invalid")
            if abs(amount - float(price.get("amount", 0))) > 1e-9:
                raise PermissionError("receipt_amount_mismatch")
            if str(receipt.get("currency") or "") != str(price.get("currency", "CNY")):
                raise PermissionError("receipt_currency_mismatch")

    def _valid(self, token: str | None, dataset_id: str) -> bool:
        return self._decode(token, dataset_id) is not None

    def is_authorized(self, token: str | None, dataset_id: str) -> bool:
        """Public check used by citation verification (no charge)."""
        return self._valid(token, dataset_id)

    # ---------------- gate ----------------

    def check(
        self,
        con,
        ds,
        proof: dict,
        payment_token: str | None = None,
    ) -> dict:
        ds_id = ds["dataset_id"]
        if proof.get("hit_count", 0) == 0:
            return fail("no_hit", dataset_id=ds_id)

        level = ds["access_level"]
        invoice = self.make_invoice(ds, proof["query"])
        if level == "open":
            return ok(gate="allowed", dataset_id=ds_id, charged=0.0, invoice=invoice)

        if level in ("registered", "authorized"):
            if self._valid(payment_token, ds_id):
                return ok(gate="allowed", dataset_id=ds_id, charged=0.0, invoice=invoice)
            return fail(
                "needs_auth",
                dataset_id=ds_id,
                auth_url=invoice["auth_url"],
                invoice=invoice,
            )

        if level == "paid":
            amount = float(invoice["price"].get("amount", 0))
            if self._valid(payment_token, ds_id):
                return ok(
                    gate="allowed",
                    dataset_id=ds_id,
                    charged=amount,
                    currency=invoice["price"].get("currency", "CNY"),
                    invoice=invoice,
                )
            return fail(
                "payment_required",
                dataset_id=ds_id,
                payment_url=invoice["payment_url"],
                price=invoice["price"],
                invoice=invoice,
            )

        return fail("not_allowed", dataset_id=ds_id)
