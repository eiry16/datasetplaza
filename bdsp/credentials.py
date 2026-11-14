"""上游凭证登记：只放本地，不进货架、不进报告、不进 git。

为什么需要这一层
----------------
``auth`` 状态有两种完全不同的成因，混成一句「需要凭证」会误导运营：

* 真的要 token（BeaconMFG 的 ``identity_required``）→ 登记凭证后重测即可。
* WAF/CDN 按 IP 或 UA 拦截（Akamai / nginx / Azure App Gateway 的
  ``Access Denied`` 页）→ 登记任何凭证都没用，得换出口或换导出地址。

所以这里只管**第一类**：把凭证放在 ``runtime/credentials.json``（已被
.gitignore 忽略），按 host 命中时注入请求头；命中事实只记一个
``credential:<host>`` 标签，值绝不写进 evidence / listings / 报告。

配置格式::

    {
      "version": 1,
      "entries": [
        {"host": "api.example.org",
         "headers": {"Authorization": "Bearer ..."},
         "query": {"api_key": "..."},
         "note": "申请入口 / 到期时间",
         "enabled": true}
      ]
    }

也支持用环境变量 ``DATASET_PLAZZ_CREDENTIALS`` 指向另一个文件，便于在
服务器上挂载密钥而不落项目目录。
"""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "runtime" / "credentials.json"
ENV_PATH = "DATASET_PLAZZ_CREDENTIALS"
VERSION = 1


def path() -> Path:
    p = os.environ.get(ENV_PATH, "").strip()
    return Path(p) if p else DEFAULT_PATH


def load() -> list[dict]:
    """读凭证表。文件缺失/损坏一律当空表，不让实测链路因此崩掉。"""
    fp = path()
    if not fp.is_file():
        return []
    try:
        doc = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("host")]


def _matches(host: str, pattern: str) -> bool:
    p = (pattern or "").strip().lower().lstrip("*.")
    h = (host or "").lower()
    return bool(p) and (h == p or h.endswith("." + p))


def match(url: str) -> tuple[dict, dict, str]:
    """返回 (额外请求头, 额外查询参数, 可公开记录的标签)。

    标签只有 host，不含任何凭证值——它会被写进 evidence 的 attempts。
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    label = ""
    for e in load():
        if e.get("enabled") is False or not _matches(host, str(e.get("host", ""))):
            continue
        for k, v in (e.get("headers") or {}).items():
            if isinstance(k, str) and isinstance(v, str) and v:
                headers[k] = v
        for k, v in (e.get("query") or {}).items():
            if isinstance(k, str) and isinstance(v, str) and v:
                query[k] = v
        label = f"credential:{host}"
    return headers, query, label


def with_query(url: str, query: dict) -> str:
    if not query:
        return url
    p = urllib.parse.urlparse(url)
    pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    pairs.extend((k, v) for k, v in query.items())
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(pairs)))


def redacted() -> list[dict]:
    """给后台/报告看的清单：只有 host、启用状态、有没有值，没有值本身。"""
    return [{
        "host": str(e.get("host", "")),
        "enabled": e.get("enabled") is not False,
        "has_header": bool(e.get("headers")),
        "has_query": bool(e.get("query")),
        "note": str(e.get("note") or ""),
    } for e in load()]


def template(items: list[dict]) -> dict:
    """按当前实测状态生成「待登记」模板，运营填值即可，不需要写代码。

    只收 ``auth`` / ``upstream_blocked`` 的条目，并为每条写明申请入口线索
    与当前判定，避免有人把 WAF 拦截误当成缺 key 去瞎申请。
    """
    entries = []
    seen = set()
    for it in items:
        h = it.get("harvest") or {}
        if h.get("status") not in ("auth", "upstream_blocked"):
            continue
        url = h.get("endpoint") or it.get("data_url") or ""
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not host or host in seen:
            continue
        seen.add(host)
        waf = h.get("status") == "upstream_blocked"
        entries.append({
            "host": host,
            "headers": {},
            "query": {},
            "note": ("WAF/CDN 按 IP 或 UA 拦截，凭证无效；需换出口/换公开导出地址"
                     if waf else "向上游申请 API key / 匿名身份头后填入 headers"),
            "enabled": False,
            "_datasets": [it.get("dataset_id", "")],
        })
    return {"version": VERSION, "entries": entries,
            "_note": "填好 headers/query 后把 enabled 改为 true；本文件绝不入库"}
