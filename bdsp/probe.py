"""Zero-input onboarding: operator supplies URL (+ optional apikey), machine
fills the rest of the manifest.

The point is not "generate a plausible description". Every auto-filled value
must be *measured* from the bytes actually returned by the endpoint: field
names/types from real rows, record count from real rows, sha256 from the
pinned snapshot, representative queries from strings that really occur in
the indexed cells. Anything that cannot be measured is left empty or marked
"未声明" and offered as a one-click override -- never invented.

Operator inputs (2):
    endpoint_url  数据集开放给平台的 URL
    api_key       凭证（可选，若源要求）
Everything else is auto-filled; 6 values are exposed as optional overrides.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import catalog
from .contract import SCHEMA, validate
from .ingest import flatten
from .text import terms_of

MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 20000
TIMEOUT = 8.0

# The only two things a human has to type.
OPERATOR_INPUTS = [
    {
        "key": "endpoint_url",
        "label": "数据集访问地址",
        "type": "url",
        "required": True,
        "placeholder": "https://data.example.org/v1/records.json  或  file:///C:/data/rows.csv",
        "hint": "返回 JSON / CSV / NDJSON / 文本行的接口地址；HTML 页面不是数据集，会被拒绝",
    },
    {
        "key": "api_key",
        "label": "API Key（可选）",
        "type": "password",
        "required": False,
        "placeholder": "源方提供的凭证；仅存本地 runtime/credentials.json，绝不写入公开契约",
        "hint": "以 Bearer + X-API-Key 两种头同时发送；平台代持，不向终端用户暴露",
    },
]

OVERRIDES = [
    {"key": "name", "label": "数据集名称", "type": "text", "hint": "默认由 URL 推断，建议改成业务名称"},
    {"key": "catalog.category", "label": "数据分类", "type": "select", "options": catalog.CATEGORIES, "hint": "按 URL / 字段关键词自动推断"},
    {"key": "access.level", "label": "访问级别", "type": "select", "options": [
        {"id": "open", "label": "开放"},
        {"id": "registered", "label": "需鉴权"},
        {"id": "authorized", "label": "需授权"},
        {"id": "paid", "label": "需付费"},
    ], "hint": "默认开放（平台代持凭证）；付费/鉴权会生成占位链接，需接入真实通道"},
    {"key": "access.price.amount", "label": "单次价格（元）", "type": "number", "hint": "仅访问级别=需付费时生效"},
    {"key": "catalog.developer.name", "label": "开发者 / 提供方", "type": "text", "hint": "默认取域名或发布方"},
    {"key": "catalog.developer.contact", "label": "开发者联系方式", "type": "text", "hint": "机器无法推断，允许留空"},
    {"key": "representative_queries", "label": "代表性问题", "type": "lines", "hint": "每行一条；默认取自索引中真实出现的可检索词，建议改成真实问法"},
]

_CJK = re.compile(r"[\u4e00-\u9fff]")
_TOKEN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{3,24}")

_CATEGORY_RULES = [
    ("production", ("生产", "产线", "设备", "传感器", "iot", "mes", "scada", "sensor", "manufactur", "supply_chain")),
    ("research", ("科研", "论文", "实验", "评测", "基线", "arxiv", "doi", "benchmark", "experiment", "baseline")),
    ("commercial", ("商业", "交易", "订单", "供应商", "客户", "commerce", "order", "sales", "customer", "vendor", "invoice", "price")),
    ("public", ("公共", "政务", "统计", "人口", "经济", "gov", "public", "open", "census", "worldbank", "statistic", "nda", "gdp", "indicator", "macro")),
]

_FIELD_HINTS = [
    (r"(date|year|年份|日期|月份|month)", "统计日期 / 年份", "year"),
    (r"(gdp|国内生产总值|产值)", "国内生产总值", ""),
    (r"(value|数值|金额|amount)", "观测数值", ""),
    (r"(usd|dollar|美元|人民币|cny)", "金额（货币单位由源方口径决定）", "currency"),
    (r"(country|国家|地区|city|城市|region)", "国家 / 地区 / 城市", ""),
    (r"(indicator|指标|code|代码|iso)", "指标 / 分类代码", ""),
    (r"(rate|ratio|率|coverage|覆盖率)", "比率 / 覆盖率", "ratio"),
    (r"(ms|latency|耗时|duration)", "耗时", "ms"),
    (r"(count|hits|数量|次数|num)", "计数", "count"),
    (r"(query|问题|关键词|keyword)", "查询原句 / 关键词", ""),
    (r"(label|标签|type|类型|status|状态)", "分类标签 / 状态", ""),
    (r"(name|名称|title|标题)", "名称 / 标题", ""),
    (r"(desc|description|说明|描述|text|正文)", "文本描述 / 正文", ""),
    (r"(id|uuid|key|编号)", "标识", ""),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean(text: str, limit: int = 80) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()[:limit]


def _local_path(url: str) -> Path | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        return Path(urllib.request.url2pathname(parsed.path))
    if not parsed.scheme and url:
        return Path(url)
    return None


def fetch(url: str, api_key: str | None, timeout: float, allow_private: bool) -> tuple[bytes, dict]:
    """Read the endpoint. Returns (raw_bytes, probe_meta)."""
    local = _local_path(url)
    if local is not None:
        if not local.is_file():
            raise ValueError(f"本地文件不存在: {local}")
        raw = local.read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError(f"文件超过 {MAX_BYTES // 1024 // 1024} MB 上限")
        return raw, {
            "scheme": "file", "http_status": 0, "content_type": "application/octet-stream",
            "bytes": len(raw), "elapsed_ms": 0.0, "host": "", "last_modified": "",
        }
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"不支持的协议: {parsed.scheme!r}（只接受 http/https/file）")
    if not allow_private and not catalog._is_public_host(parsed.hostname or ""):
        raise ValueError("安全策略拒绝内网/本地地址；如确需接入请勾选「允许内网地址」")
    headers = {
        "User-Agent": "DatasetPlazz/0.2 (+onboarding probe)",
        "Accept": "application/json, text/csv, application/x-ndjson, text/plain, */*",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    import time

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)
            meta = {
                "scheme": parsed.scheme,
                "http_status": resp.status,
                "content_type": (resp.headers.get("Content-Type") or "").split(";")[0].strip(),
                "bytes": len(raw),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "host": parsed.hostname or "",
                "last_modified": resp.headers.get("Last-Modified") or "",
            }
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(f"HTTP {exc.code}：凭证未通过，请检查 API Key") from exc
        raise ValueError(f"HTTP {exc.code}：上游拒绝请求") from exc
    if len(raw) > MAX_BYTES:
        raise ValueError(f"响应超过 {MAX_BYTES // 1024 // 1024} MB 上限；请改用分页接口或本地快照")
    return raw, meta


def _decode(raw: bytes) -> str:
    # 双重 BOM 真实存在（`\xef\xbb\xbf\xef\xbb\xbfScheme;...`）：
    # 只剥一层会让第一个列名带着 \ufeff，按列名判能力时认不出来。
    return raw.decode("utf-8-sig", errors="replace").lstrip("\ufeff")


def _json_records(doc) -> tuple[list[dict], str]:
    if isinstance(doc, list):
        # World Bank style: [metadata_dict, [rows...]]. Prefer the largest
        # nested record array over the metadata wrapper.
        nested: list[dict] = []
        for el in doc:
            if isinstance(el, list):
                cand = [x for x in el if isinstance(x, dict)]
                if len(cand) > len(nested):
                    nested = cand
        direct = [x for x in doc if isinstance(x, dict)]
        return (nested if len(nested) > len(direct) else direct), "json"
    if isinstance(doc, dict):
        best_key, best = "", []
        for key, val in doc.items():
            if isinstance(val, list) and len(val) > len(best):
                sample = [x for x in val[:20] if isinstance(x, dict)]
                if sample:
                    best_key, best = str(key), [x for x in val if isinstance(x, dict)]
        if best_key:
            return best, "json"
        if any(isinstance(v, (str, int, float, bool)) for v in doc.values()):
            return [doc], "json"
    raise ValueError("JSON 响应里找不到记录数组（list of objects）")


def parse_records(raw: bytes, meta: dict) -> tuple[list[dict], str, str]:
    """Return (records, ingest_format, raw_file)."""
    text = _decode(raw)
    stripped = text.lstrip()
    ctype = meta.get("content_type", "")
    if "html" in ctype or stripped[:200].lower().startswith(("<!doctype html", "<html")):
        raise ValueError(
            "该地址返回的是 HTML 页面，不是结构化数据集。"
            "本平台只接入主动开放的数据接口（JSON / CSV / NDJSON / 文本行），不爬取网页。"
        )
    # 1) JSON
    if stripped.startswith(("{", "[")) or "json" in ctype:
        try:
            doc = json.loads(text)
            records, fmt = _json_records(doc)
            return records, fmt, "raw.json"
        except (json.JSONDecodeError, ValueError) as exc:
            if stripped.startswith(("{", "[")):
                raise ValueError(f"JSON 解析失败: {exc}") from exc
    # 2) NDJSON
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        try:
            parsed = [json.loads(ln) for ln in lines[:50]]
            if all(isinstance(x, dict) for x in parsed):
                all_rows = [json.loads(ln) for ln in lines]
                return [r for r in all_rows if isinstance(r, dict)], "json", "raw.json"
        except json.JSONDecodeError:
            pass
    # 3) CSV / TSV
    # 分隔符不能只认逗号：欧洲开放数据大量用分号（``Type;Année;Total``），
    # 只看逗号会把它判成「文本行」，于是造出 paragraph_no/text 两个假字段，
    # 一张本来就是表格的数据集被记成「语料」。按表头行里出现最多的候选符
    # 来定，逗号仍然优先（平手时取逗号）。
    if lines:
        counts = {d: lines[0].count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=lambda d: (counts[d], d == ","))
        delimiter = best if counts[best] > 0 else ","
    else:
        delimiter = ","
    if lines and delimiter in lines[0]:
        try:
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            rows = [dict(r) for r in reader]
            if rows and any(v for row in rows[:5] for v in row.values()):
                return rows, "csv", "raw.csv"
        except csv.Error:
            pass
    # 4) plain text lines
    return [{"paragraph_no": str(i), "text": ln.strip()} for i, ln in enumerate(lines, 1) if ln.strip()], "lines", "raw.txt"


def _infer_type(values: list[str]) -> str:
    vals = [v for v in values if v not in ("", "null", "None")]
    if not vals:
        return "string"
    if all(v.lower() in ("true", "false") for v in vals):
        return "boolean"
    if all(re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", v or "") for v in vals):
        return "number"
    if all(v.startswith("[") and v.endswith("]") for v in vals):
        return "array"
    return "string"


def _field_meta(name: str, values: list[str]) -> dict:
    low = name.lower()
    desc, unit = "", ""
    for pattern, d, u in _FIELD_HINTS:
        if re.search(pattern, low):
            desc, unit = d, u
            break
    if not desc:
        desc = f"字段 {name}（按样本值自动推断，建议人工补充业务口径）"
    return {
        "name": name,
        "type": _infer_type(values),
        "description": desc,
        "unit": unit,
        "auto": True,
        "sample": _clean(next((v for v in values if v), ""), 60),
    }


def _choose_row_key(records: list[dict], flat_rows: list[dict], columns: list[str]) -> tuple[list[str], bool]:
    priority = ("id", "uuid", "code", "iso", "date", "year", "query", "key", "编号", "年份")
    candidates = sorted(
        columns,
        key=lambda c: (0 if any(p in c.lower() for p in priority) else 1, columns.index(c)),
    )
    for col in candidates:
        vals = [r.get(col, "") for r in flat_rows]
        if vals and all(v for v in vals) and len(set(vals)) == len(vals):
            return [col], False
    return ["__row_no"], True


_BOILERPLATE = ("建议人工", "自动推断", "平台生成", "字段说明", "数据集记录")


def _representative_queries(flat_rows: list[dict], field_meta: dict, limit: int = 3) -> list[str]:
    """Pick phrases that literally occur in indexed cells, so the claim audit passes.

    Real values outrank auto-generated field descriptions: a query like
    "个人养老金" is useful, "建议人工补充业务口径" is not.
    """
    best: dict[str, float] = {}
    freq: dict[str, int] = {}

    def add(tok: str, weight: float) -> None:
        if len(tok) < 3 or tok.isdigit():
            return
        if _CJK.search(tok) and len(tok) > 12:
            tok = tok[:12]  # still a literal substring of the indexed cell
        if len(tok) > 16 and not _CJK.search(tok):
            tok = tok[:16]
        best[tok] = max(best.get(tok, 0.0), weight)
        freq[tok] = min(freq.get(tok, 0) + 1, 5)

    for row in flat_rows[:80]:
        for col, val in row.items():
            if not val:
                continue
            fm = field_meta[col]
            descriptive = fm["type"] == "string" and any(
                k in fm["description"]
                for k in ("文本", "查询", "名称", "描述", "关键词", "正文", "标题", "原句")
            )
            weight = 4.0 if descriptive else (2.0 if fm["type"] == "string" else 1.0)
            if _CJK.search(str(val)):
                weight += 1.0
            for tok in _TOKEN.findall(str(val)):
                add(tok, weight)
    # Field descriptions are a weak fallback; skip the machine's own boilerplate.
    for fm in field_meta.values():
        for tok in _TOKEN.findall(fm["description"]):
            if not any(b in tok for b in _BOILERPLATE):
                add(tok, 0.5)
    scored = {t: best[t] * 10 + freq[t] for t in best}
    ranked = sorted(scored, key=lambda t: (-scored[t], -len(t)))
    picked: list[str] = []
    for tok in ranked:
        if any(tok in p or p in tok for p in picked):
            continue
        picked.append(tok)
        if len(picked) >= limit:
            break
    while len(picked) < 2:
        picked.append("数据集记录" if not picked else "字段说明")
    return picked[:5]


def _infer_category(url: str, name: str, columns: list[str]) -> tuple[str, str]:
    haystack = (url + " " + name + " " + " ".join(columns)).lower()
    for cid, words in _CATEGORY_RULES:
        hit = next((w for w in words if w in haystack), None)
        if hit:
            return cid, f"URL / 字段名命中关键词「{hit}」"
    return "other", "未命中分类关键词，默认归为「其他」，建议人工复核"


def _infer_publisher(url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return "本地文件", "project", ""
    base = re.sub(r"^(www|api|data)\.", "", host)
    level = "market"
    if base.endswith(".gov.cn") or ".gov." in base or base.endswith(".gov"):
        level = "government"
    elif base.endswith(".edu") or base.endswith(".edu.cn") or "arxiv" in base:
        level = "research"
    elif base.endswith(".org") or "worldbank" in base or "un.org" in base:
        level = "international"
    homepage = f"{parsed.scheme}://{host}" if parsed.scheme in ("http", "https") else ""
    return base, level, homepage


@dataclass
class ProbeResult:
    draft: dict
    raw_bytes: bytes
    raw_file: str
    meta: dict
    warnings: list[str] = field(default_factory=list)
    auto_filled: list[dict] = field(default_factory=list)
    field_preview: list[dict] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)
    api_key: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "probe": self.meta,
            "draft": self.draft,
            "auto_filled": self.auto_filled,
            "operator_inputs": OPERATOR_INPUTS,
            "overrides": OVERRIDES,
            "warnings": self.warnings,
            "field_count": len(self.draft["fields"]),
            "record_count": self.draft["coverage"]["declared_records"],
            # Preview-only samples: deliberately NOT written into the public
            # manifest, so a paid dataset cannot leak values through its
            # own field dictionary.
            "field_preview": getattr(self, "field_preview", []),
        }


def probe(
    url: str,
    api_key: str | None = None,
    timeout: float = TIMEOUT,
    allow_private: bool = False,
) -> ProbeResult:
    url = (url or "").strip()
    if not url:
        raise ValueError("endpoint_url 不能为空")
    raw, meta = fetch(url, api_key, timeout, allow_private)
    records, fmt, raw_file = parse_records(raw, meta)
    warnings: list[str] = []
    if not records:
        raise ValueError("接口返回了 0 条记录；无法建立字段级索引")
    if len(records) > MAX_RECORDS:
        warnings.append(f"记录数 {len(records)} 超过上限 {MAX_RECORDS}，只索引前 {MAX_RECORDS} 条")
        records = records[:MAX_RECORDS]

    flat_rows = [flatten(r) for r in records]
    columns: list[str] = []
    for row in flat_rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    # An API may answer with a pagination/status envelope instead of data
    # (e.g. World Bank returns [metadata, null] for indicators with no
    # observations). Indexing that as a "dataset" would be a false claim.
    # keys are normalized the same way as the column names below
    meta_only = {
        "page", "pages", "perpage", "total", "sourceid", "lastupdated",
        "api", "status", "message", "error", "request", "query", "offset",
        "limit", "next", "url", "code", "count",
    }
    if columns and all(re.sub(r"[^a-z0-9]", "", k.lower()) in meta_only for k in columns):
        raise ValueError(
            "接口只返回了分页/状态元数据，没有实际数据记录（例如 World Bank 对无观测值的指标返回 total=0）。"
            "为避免把空壳当成数据集，本次不接入；请换有数据的指标或源。"
        )
    row_key, synthetic = _choose_row_key(records, flat_rows, columns)
    if synthetic:
        for i, rec in enumerate(records, 1):
            rec["__row_no"] = str(i)
        flat_rows = [flatten(r) for r in records]
        columns.append("__row_no")
        warnings.append("源数据没有天然唯一键，平台自动生成 __row_no 作为行标识")

    field_meta = {}
    for col in columns:
        field_meta[col] = _field_meta(col, [r.get(col, "") for r in flat_rows[:200]])
    if synthetic:
        field_meta["__row_no"]["description"] = "平台生成的行号（源数据无天然唯一键）"
        field_meta["__row_no"]["sample"] = "1"

    # Normalize raw into a shape the existing ingest layer already understands.
    if fmt == "json":
        payload = {"rows": records}
        raw_out = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        record_path = "rows"
    elif fmt == "csv":
        # Normalize TSV/quoted variants into plain comma CSV, which is what
        # ingest.py's DictReader consumes. The hash is taken from THIS file.
        cols = list(records[0].keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow({k: rec.get(k, "") for k in cols})
        raw_out = buf.getvalue().encode("utf-8")
        record_path = ""
    else:
        raw_out = raw
        record_path = ""

    sha = _sha(raw_out)
    parsed = urllib.parse.urlparse(url)
    local = _local_path(url)
    tail = ""
    slug_prefix = (parsed.hostname or "").lower()
    if local is not None:
        tail = local.stem
        if tail.lower() in ("raw", "data", "rows", "index", "export"):
            tail = local.parent.name or tail
        slug_prefix = (local.parent.name or "").lower()
    else:
        parts = [p for p in parsed.path.split("/") if p and "?" not in p]
        tail = re.sub(r"\.[a-z0-9]{1,5}$", "", parts[-1], flags=re.I) if parts else (parsed.hostname or "dataset")
    publisher, pub_level, homepage = _infer_publisher(url)
    category, cat_reason = _infer_category(url, tail, columns)
    name = f"{publisher} · {tail}" if publisher and tail else (tail or publisher or "未命名数据集")
    slug_src = (
        (slug_prefix + "_" + tail) if slug_prefix != tail else slug_prefix
    ).lower() or tail.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug_src).strip("_")[:44] or "dataset"
    dataset_id = f"{slug}_{_sha(url.encode('utf-8'))[:6]}"
    version = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "-" + sha[:6]
    queries = _representative_queries(flat_rows, field_meta)

    # time range from any date-like column, if measurable
    date_cols = [c for c in columns if re.search(r"(date|year|年份|日期|month|月份)", c.lower())]
    dates = sorted({str(r.get(c, "")) for c in date_cols for r in flat_rows if r.get(c, "")})
    time_range = [dates[0], dates[-1]] if dates else [version[:10], version[:10]]

    fields = [
        {"name": f["name"], "type": f["type"], "description": f["description"], "unit": f["unit"]}
        for f in field_meta.values()
    ]
    draft = {
        "schema": SCHEMA,
        "dataset_id": dataset_id,
        "name": name,
        "publisher": publisher,
        "publisher_level": pub_level,
        "domain": [catalog.CATEGORY_LABELS[category]],
        "modality": "text" if fmt == "lines" else "structured",
        "update_frequency": "未声明（上游未提供更新频率）",
        "license": "未声明（需发布方补充许可信息）",
        "version": version,
        "source_url": url,
        "retrieved_at": _now(),
        "sha256": sha,
        "raw_file": raw_file,
        "row_key": row_key,
        "fields": fields,
        "ingest": {"format": fmt, "record_path": record_path, "row_key": row_key},
        "representative_queries": queries,
        "coverage": {
            "declared_records": len(records),
            "time_range": time_range,
            "entity_scope": f"{name}；{len(records)} 条记录 / {len(fields)} 个字段（由平台实测）",
            "notes": "记录数、字段、哈希均由接入探测实测，不采信发布方自报",
        },
        "access": {
            "level": "open",
            "price": {"currency": "CNY", "per": "query", "amount": 0},
            "auth_url": "",
            "payment_url": "",
            "free_preview_rows": 5,
            "notes": "平台代持凭证时默认开放；切到付费/鉴权会生成占位链接，需接真实通道",
        },
        "evidence_policy": {
            "proof_free": True,
            "no_hit_no_charge": True,
            "max_proof_chars": 160,
            "notes": "平台硬规则：付费前先给可核对证据，空命中不计费",
        },
        "commitment": {
            "snapshot_sha256": sha,
            "index_sha256": None,
            "merkle_root": None,
            "notes": "index_sha256 由 build 写入索引 meta 表",
        },
        "provenance": {
            "probe_http_status": meta.get("http_status", 0),
            "probe_content_type": meta.get("content_type", ""),
            "note": "由接入探测自动拉取并固定快照",
        },
        "catalog": {
            "category": category,
            "category_source": "auto",
            "category_reason": cat_reason,
            "developer": {
                "name": publisher,
                "org_type": pub_level,
                "homepage": homepage,
                "contact": "",
            },
            "tags": [],
            "added_at": _now(),
            "enabled": True,
            "notes": "由自动探测创建；标注为 auto 的值建议人工复核",
        },
        "connection": {
            "endpoint": url,
            "method": "GET",
            "auth_scheme": "bearer" if api_key else "none",
            "credential_required": bool(api_key),
            "content_type": meta.get("content_type", ""),
            "last_checked": _now(),
            "last_error": "",
        },
    }
    if meta.get("last_modified"):
        draft["update_frequency"] = f"上游 Last-Modified: {meta['last_modified']}"
    if "html" in meta.get("content_type", ""):
        warnings.append("上游声明为 HTML，已按文本行处理；建议改为结构化接口")

    auto = [
        {"key": "dataset_id", "value": dataset_id, "source": "URL + 哈希（保证唯一）"},
        {"key": "name", "value": name, "source": "URL 路径 / 域名推断"},
        {"key": "publisher / publisher_level", "value": f"{publisher} / {pub_level}", "source": "域名后缀规则"},
        {"key": "category", "value": catalog.CATEGORY_LABELS[category], "source": cat_reason},
        {"key": "developer", "value": publisher, "source": "域名；联系方式留空待人工补充"},
        {"key": "fields", "value": f"{len(fields)} 个字段", "source": "样本行 flatten + 类型/单位推断"},
        {"key": "row_key", "value": ", ".join(row_key), "source": "唯一性检验" if not synthetic else "源无唯一键，自动生成行号"},
        {"key": "representative_queries", "value": " / ".join(queries), "source": "取自索引中真实出现的可检索词"},
        {"key": "coverage.declared_records", "value": len(records), "source": "实测记录数"},
        {"key": "sha256 / version", "value": sha[:16] + "…", "source": "固定快照哈希 + 时间戳"},
        {"key": "access / evidence_policy", "value": "open / proof_free + no_hit_no_charge", "source": "平台默认契约与硬规则"},
        {"key": "ingest", "value": f"{fmt} · record_path={record_path or '(root)'}", "source": "响应格式自动识别"},
    ]
    violations = validate(draft)
    if violations:
        raise ValueError("自动生成的契约未通过校验：" + "; ".join(violations))

    # 新数据集一进来就自动补卡：卡不是让源方填的，是平台拿到这份响应之后
    # 自己看出来的。
    from . import capability as capability_mod
    from . import harvest

    evidence = harvest.build_evidence(
        records, meta, url, how="onboarding",
        elapsed_ms=int(meta.get("elapsed_ms") or 0),
    )
    draft["capability"] = capability_mod.declare(
        {"name": name, "domain": draft["domain"], "publisher": publisher}, evidence
    )
    # 样本值只用于当场判能力（Entity 列的值是国名、不是字段名，不看值就认
    # 不出地理轴），不写进契约：draft 会被复制进 sources/<id>/source.json，
    # 带上原始记录值既臃肿，也违背「契约不含样本」这条既有安全约束。能力卡
    # 已经用完整证据算完，结论不受影响；要复核样本值去 harvest 缓存。
    draft["harvest"] = {k: v for k, v in evidence.items() if k != "samples"}
    auto.append({"key": "capability", "value": draft["capability"]["what"],
                 "source": "按真实字段与样本值实测（非源方自述）"})
    violations = validate(draft)
    if violations:
        raise ValueError("补充实测证据后的契约未通过校验：" + "; ".join(violations))
    return ProbeResult(
        draft=draft,
        raw_bytes=raw_out,
        raw_file=raw_file,
        meta=meta,
        warnings=warnings,
        auto_filled=auto,
        field_preview=[
            {
                "name": f["name"],
                "type": f["type"],
                "unit": f["unit"],
                "description": f["description"],
                "sample": f["sample"],
            }
            for f in field_meta.values()
        ],
        bodies=[
            str(val).lower() + " " + field_meta[col]["description"].lower()
            for row in flat_rows[:200]
            for col, val in row.items()
            if val
        ][:3000],
        api_key=api_key,
    )


def apply_overrides(draft: dict, overrides: dict, bodies: list[str] | None = None) -> dict:
    """Whitelisted edits. A human-written representative query must actually
    have a content hit in the pinned snapshot, otherwise the platform would
    be publishing a declaration it cannot reproduce (claim_mismatch).
    """
    out = json.loads(json.dumps(draft, ensure_ascii=False))
    allowed = {
        "name": lambda v: out.__setitem__("name", _clean(str(v), 120) or out["name"]),
        "catalog.category": lambda v: out["catalog"].__setitem__(
            "category", v if v in catalog.CATEGORY_IDS else out["catalog"]["category"]
        ),
        "catalog.developer.name": lambda v: out["catalog"]["developer"].__setitem__("name", _clean(str(v), 80)),
        "catalog.developer.contact": lambda v: out["catalog"]["developer"].__setitem__("contact", _clean(str(v), 80)),
        "access.level": lambda v: out["access"].__setitem__("level", v),
        "access.price.amount": lambda v: out["access"]["price"].__setitem__("amount", float(v or 0)),
    }
    for key, fn in allowed.items():
        if key in (overrides or {}):
            fn(overrides[key])
    if "catalog.category" in (overrides or {}):
        out["catalog"]["category_source"] = "operator"
        out["catalog"]["category_reason"] = "操作员在接入页面人工指定"
    if "representative_queries" in (overrides or {}):
        raw = overrides["representative_queries"]
        lines = [x.strip() for x in (raw.splitlines() if isinstance(raw, str) else raw) if str(x).strip()]
        if not (2 <= len(lines) <= 5):
            raise ValueError("代表性问题需要 2–5 条")
        if bodies:
            bad = []
            for q in lines:
                terms, _ = terms_of(q)
                if not terms or any(
                    not any(t.lower() in b for b in bodies) for t in terms
                ):
                    bad.append(q)
            if bad:
                raise ValueError(
                    "这些代表性问题在该数据集的真实内容里没有命中，平台拒绝上架不实声明："
                    + "、".join(bad)
                )
        out["representative_queries"] = lines

    level = out["access"]["level"]
    if level == "paid":
        out["access"]["price"]["amount"] = float(out["access"]["price"].get("amount") or 0) or 0.01
        out["access"]["payment_url"] = (
            out["access"].get("payment_url")
            or f"https://pay.example.invalid/datasetplazz/{out['dataset_id']}"
        )
        out["evidence_policy"]["max_proof_chars"] = 80
    elif level in ("registered", "authorized"):
        out["access"]["price"]["amount"] = 0
        out["access"]["auth_url"] = (
            out["access"].get("auth_url")
            or f"https://auth.example.invalid/datasetplazz/{out['dataset_id']}"
        )
    else:
        out["access"]["price"]["amount"] = 0
    return out


def install(
    result: ProbeResult,
    draft: dict,
    sources_root: str | Path,
    credentials_path: str | Path,
) -> dict:
    """Write the pinned snapshot + public manifest + private credential."""
    sources_root = Path(sources_root)
    folder = sources_root / draft["dataset_id"]
    if folder.exists():
        raise ValueError(f"dataset_id 已存在: {draft['dataset_id']}")
    violations = validate(draft)
    if violations:
        raise ValueError("契约未通过校验：" + "; ".join(violations))
    folder.mkdir(parents=True, exist_ok=False)
    (folder / draft["raw_file"]).write_bytes(result.raw_bytes)
    (folder / "source.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if result.api_key:
        creds = {}
        p = Path(credentials_path)
        if p.exists():
            creds = json.loads(p.read_text(encoding="utf-8"))
        creds[draft["dataset_id"]] = {
            "api_key": result.api_key,
            "auth_scheme": draft["connection"]["auth_scheme"],
            "updated_at": _now(),
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(creds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "dataset_id": draft["dataset_id"], "dir": str(folder), "raw_bytes": len(result.raw_bytes)}
