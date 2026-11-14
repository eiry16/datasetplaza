"""Optional HTTPS for the local control plane.

The project stays dependency-free: certificates are produced by the OpenSSL
CLI already present on this machine (Git Bash / most dev boxes). Nothing is
downloaded, and no private key ever leaves runtime/tls/. A self-signed cert
will of course show a browser warning - for a real deployment, put your CA-
issued cert/key behind --cert / --key instead.
"""
from __future__ import annotations

import shutil
import ssl
import subprocess
from pathlib import Path


def ensure_self_signed(directory, force: bool = False) -> tuple[Path, Path]:
    """Create (or reuse) a localhost self-signed cert; return (cert, key)."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    cert, key = d / "localhost-cert.pem", d / "localhost-key.pem"
    if cert.is_file() and key.is_file() and not force:
        return cert, key
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "未找到 openssl，无法生成自签证书；请用 --cert / --key 传入 CA 签发的证书"
        )
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert),
        "-days", "365", "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not cert.is_file() or not key.is_file():
        raise RuntimeError(f"openssl 生成证书失败: {proc.stderr.strip()[:400]}")
    return cert, key


def wrap_server(httpd, cert, key):
    """Turn a bound HTTPServer socket into a TLS server socket, in place."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd
