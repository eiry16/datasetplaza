"""实测（harvest）：不信任何人的自述，自己把数据集打开看字段。

为什么非要实测
--------------
``capability.declare`` 在没有证据时只能按目录元数据推断——名称里写着
"Population" 就猜有地区与数量，名称里没写"评分"就不敢说没有。这有个根本
缺陷：**推断只能看见名字，看不见数据**。同一份叫 "Namur - Economy - Shops"
的数据，可能是门店名录（含地址、业态），也可能是几条经济指标；只有打开
看字段才知道。

发布方自己填能力卡也不可靠：他填的是"我希望你以为我有什么"，不是"我真的
有什么"。而且绝大多数源方根本不会来填。所以本模块只做一件事——把
``data_url`` 真的取下来，数清楚几行、几列、每列叫什么、样本值长什么样。

事实归 ``evidence``，判断归 ``capability.declare(evidence=...)``。
两者分开是为了可复核：agent 或运营拿到一张卡，能顺着 ``evidence.endpoint``
自己再打开一遍，核对说的是真的。

三条硬规矩
----------
1. **不猜端点。** 门户页不是数据集：返回 HTML 就记 ``html``。只有少数平台
   有公开的导出约定（Opendatasoft 的 ``/api/explore/v2.1/.../exports/json``、
   PxStat 的 ``/CSV/1.0/en``），这些写进 ``_rewrites`` 并在 evidence 里注明
   ``resolved_how``——「我是怎么找到这份数据的」，不能让结果看起来像凭空来的。
2. **不假装取到。** XLSX/ZIP/PDF、WMS/WFS 服务文档、401/403、超时，各记各的
   状态。最不能犯的错是把二进制当文本行解析出 ``paragraph_no/text`` 两个假
   字段——那会让一张卡看起来有内容，实际全是平台自己编的。
3. **不预判结论。** 这里不写 ``entities``/``measures``，只交字段与样本值。

关于「样本不代表全部」
----------------------
字段集合取自响应报文的键集合，样本值按全程等距抽样（只看前几行会把全球
面板判成「阿富汗」）。分页接口可能还有更多字段，某个字段也可能只在后段
数据里出现。所以 ``evidence.note`` 固定写明「字段取自本次响应的键集合」，
``capability`` 的措辞用「实测字段」而不是「仅含这些字段」。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import catalog, credentials

# 只看字段，不落快照，所以上限比接入探测宽：4 MB 会把一批正常的
# 市级统计表挡在门外，而我们必须真的打开看过才好意思说「没有」。
MAX_BYTES = 12 * 1024 * 1024
TIMEOUT = 8.0
# 探测逻辑的版本。解析规则一改（认二进制表格、区分 WAF、支持浏览器 UA
# 重试、登记凭证），旧结论就不能再用了：缓存要按版本失效。
# h-5 (0.14)：新增两道观测值门禁——value 列全空判 empty、JSON-stat 按顶层
# value 展开且空结果判 empty。旧缓存里可能有「假 ok」的能力卡，必须全量重测。
HARVEST_VERSION = "h-5"

# 有些上游只挡默认爬虫 UA，不挡普通浏览器。这是一次**合规的** UA 重试，
# 不是绕过鉴权：端点本身公开，响应体里明写着 Access Denied 而非 401。
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
}
_WAF_MARKERS = (b"access denied", b"403 forbidden", b"cloudflare",
                b"attention required", b"just a moment",
                b"microsoft-azure-application-gateway", b"<h1>403")
# 只看前几百行就够判形态了；记录数仍报真实条数（解析后的 len(records)）。
SAMPLE_RECORDS = 400
MAX_FIELDS = 60
MAX_CANDIDATES = 4

# 状态码是对外口径的一部分，不许含糊。每一种都对应一句人话。
STATUS_LABELS = {
    "ok": "已实测：取到结构化记录",
    "empty": "接口通但 0 条记录",
    "html": "返回网页而非数据",
    "service_doc": "返回地图/目录服务文档（元数据）",
    "binary": "返回 Excel/PDF/压缩包等二进制文件",
    "binary_nontable": "二进制容器里没有可还原的记录表（地图包/扫描 PDF 等）",
    "auth": "需要凭证（401/403）",
    "upstream_blocked": "上游 WAF/CDN 拒绝本次访问（不是凭证问题）",
    "no_public_endpoint": "已核实源站没有免登录的内容级取数端点",
    "portal_catalog_only": "门户本身不持有记录；已核实公开目录检索端点（见 portal_api_url）",
    "http_error": "上游返回错误状态",
    "timeout": "请求超时",
    "net": "网络不可达",
    "blocked": "安全策略拒绝（内网/非法协议）",
    "too_large": "响应超过体积上限",
    "unparsed": "响应不是可解析的结构化数据",
    "xml": "返回 XML 文档（尚未解析为记录）",
    "rdf": "返回 RDF/JSON-LD 元数据（不是记录表）",
    "no_url": "未提供取数地址",
}

# 名字像地理维度的列，样本多留几个值——地理轴只能从值里看出来。
_GEO_NAME = re.compile(
    r"countr|nation|\biso|city\b|town|ward|region|district|province|county|"
    r"municipal|borough|address|entity|commune|prefecture|\bstate\b|place|"
    r"latitude|longitude|\blat\b|\blon\b|\blng\b|\bgeo|\bcode\b|\biso",
    re.I,
)
_TIME_NAME = re.compile(
    r"^(year|date|time|period|month|quarter|week|day|obs_?time|"
    r"reference_?date|年份|年度|日期|时间|月份|季度)$",
    re.I,
)


class HarvestError(Exception):
    """一次取数尝试失败。``status`` 是枚举值，``note`` 给人看。"""

    def __init__(self, status: str, note: str, http_status: int = 0,
                 content_type: str = "", size: int = 0, raw: bytes | None = None):
        super().__init__(note)
        self.status = status
        self.note = note
        self.http_status = http_status
        self.content_type = content_type
        self.size = size
        # 只有 html 会带 body：后面要用页面里明写的下载链接再试一次，
        # 不必为此再发一次请求。
        self.raw = raw

    def as_dict(self, url: str, how: str) -> dict:
        return {"url": url, "how": how, "status": self.status,
                "http_status": self.http_status, "note": self.note}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short_url(url: str, limit: int = 56) -> str:
    s = re.sub(r"^https?://", "", url or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _local_path(url: str) -> Path | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        return Path(urllib.request.url2pathname(parsed.path))
    if not parsed.scheme and url:
        return Path(url)
    return None


def _is_public(host: str) -> bool:
    fn = getattr(catalog, "_is_public_host", None)
    return bool(fn(host)) if fn else True


def _get(url: str, timeout: float, allow_private: bool,
         headers: dict | None = None) -> tuple[bytes, dict]:
    """取字节。失败一律抛 HarvestError，状态分类在这里定。"""
    local = _local_path(url)
    if local is not None:
        if not local.is_file():
            raise HarvestError("net", f"本地文件不存在: {local}")
        raw = local.read_bytes()
        if len(raw) > MAX_BYTES:
            raise HarvestError("too_large", "文件超过体积上限", size=len(raw))
        return raw, {"scheme": "file", "http_status": 0,
                     "content_type": "", "bytes": len(raw), "host": "",
                     "last_modified": ""}

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HarvestError("blocked", f"不支持的协议: {parsed.scheme!r}")
    if not allow_private and not _is_public(parsed.hostname or ""):
        raise HarvestError("blocked", "安全策略拒绝内网/本地地址")

    extra, query, cred_label = credentials.match(url)
    if query:
        url = credentials.with_query(url, query)
    req_headers = {
        "User-Agent": "DatasetPlazz/0.11 (+capability harvest)",
        "Accept": "application/json, text/csv, application/x-ndjson, "
                  "application/geo+json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    req_headers.update(extra)          # 已登记的凭证，值只到这里为止
    req_headers.update(headers or {})  # 浏览器 UA 重试
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 分块读 + 总时长上限。socket timeout 只是「每次 read」的上限，
            # 服务器每 9 秒吐一个字节就能把一次取数无限续命——0.12 全量
            # 实测就是被这样一条请求卡了十几分钟。总 deadline 才是硬边界。
            started = time.monotonic()
            buf = bytearray()
            try:
                while len(buf) <= MAX_BYTES:
                    chunk = resp.read(min(65536, MAX_BYTES + 1 - len(buf)))
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if time.monotonic() - started > timeout:
                        raise HarvestError("timeout", "响应传输超过总时长上限")
                raw = bytes(buf)
            except http.client.IncompleteRead as exc:
                # chunked 响应中途断流是常事（大 CSV 尤其）。不当成致命异常：
                # 一次取数失败该产出一条失败 evidence，而不是让整批 238 条
                # 的实测脚本在 fut.result() 里崩掉、连能力卡都不落盘。
                raise HarvestError("net", f"响应传输中断：{str(exc)[:80]}") from exc
            except TimeoutError as exc:
                raise HarvestError("timeout", "响应传输超时") from exc
            except OSError as exc:
                raise HarvestError("net", f"读取响应失败：{str(exc)[:80]}") from exc
            meta = {
                "scheme": parsed.scheme,
                "http_status": resp.status,
                "content_type": (resp.headers.get("Content-Type") or "").split(";")[0].strip(),
                "bytes": len(raw),
                "host": parsed.hostname or "",
                "last_modified": resp.headers.get("Last-Modified") or "",
                "final_url": resp.geturl(),
                "credential": cred_label,
            }
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(512)
        except Exception:  # noqa: BLE001 - 错误响应读不动也不影响分类
            pass
        low = body.lower()
        waf = any(m in low for m in _WAF_MARKERS)
        if exc.code in (401, 403) and (exc.code == 401 or not waf):
            raise HarvestError("auth", f"HTTP {exc.code}：需要凭证", http_status=exc.code,
                               content_type=(exc.headers or {}).get("Content-Type", ""))
        if exc.code in (401, 403) and waf:
            server = next((m.decode("ascii", "replace") for m in _WAF_MARKERS if m in low), "")
            raise HarvestError("upstream_blocked",
                               f"HTTP {exc.code}：上游 WAF/CDN 拦截（{server}），不是缺凭证",
                               http_status=exc.code,
                               content_type=(exc.headers or {}).get("Content-Type", ""))
        raise HarvestError("http_error", f"HTTP {exc.code}：上游拒绝请求", http_status=exc.code)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, OSError)) and "timed out" in str(reason):
            raise HarvestError("timeout", "请求超时")
        if "timed out" in str(reason).lower():
            raise HarvestError("timeout", "请求超时")
        raise HarvestError("net", f"连接失败：{str(reason)[:80]}")
    except TimeoutError:
        raise HarvestError("timeout", "请求超时")

    if len(raw) > MAX_BYTES:
        raise HarvestError("too_large", f"响应超过 {MAX_BYTES // 1024 // 1024} MB 上限",
                           size=len(raw))
    return raw, meta


def _sniff(raw: bytes, content_type: str) -> str:
    """返回 body 的真实形态。按字节判，不信 Content-Type——门户常把 JSON
    标成 text/html，也常把 HTML 标成 text/plain。"""
    head = raw[:4096]
    if head[:4] == b"PK\x03\x04":
        return "binary"       # xlsx / docx / zip
    if head[:5] == b"%PDF-":
        return "binary"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "binary"       # xls / doc
    if head[:2] == b"\x1f\x8b":
        return "binary"       # gzip
    low = head.lower().lstrip()
    if low.startswith(b"@prefix") or b"text/turtle" in (content_type or "").encode():
        # Turtle/RDF 是「关于记录的描述」，不是记录表。按文本行读会得到几千
        # 行 ``ns1:…`` 垃圾字段。
        return "rdf"
    if low.startswith(b"<"):
        if (b"<wms_capabilities" in low or b"<wfs_capabilities" in low
                or b"<capabilities" in low or b"serviceexception" in low
                or b"<csw:" in low):
            return "service_doc"
        if b"<html" in low or low.startswith(b"<!doctype html") or b"<head" in low:
            return "html"
        if low.startswith(b"<?xml"):
            return "xml"
    if "html" in (content_type or "").lower():
        return "html"
    if "spreadsheet" in (content_type or "").lower() or "excel" in (content_type or "").lower():
        return "binary"
    return "data"


def _printable_ratio(raw: bytes) -> float:
    sample = raw[:8192]
    if not sample:
        return 0.0
    good = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126 or b >= 0xC2)
    return good / len(sample)


def _nested_rows(doc, depth: int = 0) -> list[dict]:
    """在嵌套 JSON 里找真正的记录数组。

    目录类 API 很少把记录放在顶层：data.europa.eu 是
    ``{"result": {"results": [...]}}``，Zenodo 是 ``{"hits": {"hits": [...]}}``。
    只检查顶层键会把这些「明明有 10 条数据集记录」的响应当成单个配置对象。
    但也不能见 list[dict] 就收——SDMX 的 ``references`` 里全是结构定义，
    所以优先找名字像记录的键，且必须有 ≥1 条带多个字段的字典。
    """
    if isinstance(doc, list):
        if doc and all(isinstance(x, dict) for x in doc[:5]):
            if any(len(x) >= 2 for x in doc[:5]):
                return doc
        return []
    if not isinstance(doc, dict) or depth >= 5:
        return []
    preferred = ("results", "hits", "records", "data", "items", "rows",
                 "datasets", "observations", "features")
    for key in preferred:
        if key in doc:
            found = _nested_rows(doc[key], depth + 1)
            if found:
                return found
    for value in doc.values():
        found = _nested_rows(value, depth + 1)
        if found:
            return found
    return []


def _is_meta_only(columns: list[str]) -> bool:
    """整份响应只有分页/状态字段，没有一条实际观测。

    世界银行对没有数据的指标会老实返回 ``[{"page":0,"total":0,...}, null]``。
    照单全收的话，这张能力卡会声称自己有 ``page / pages / per_page / total``
    六个字段——全是平台自己的取数参数，没有一个是数据。这比没有卡更坏。
    """
    meta_only = {
        "page", "pages", "perpage", "total", "sourceid", "lastupdated",
        "api", "status", "message", "error", "request", "query", "offset",
        "limit", "next", "url", "code", "count", "version", "timestamp",
    }
    if not columns:
        return False
    return all(re.sub(r"[^a-z0-9]", "", c.lower()) in meta_only for c in columns)


# ── 观测值门禁：HTTP 200 + 有记录行 ≠ 有数据 ──────────────────────────────
# 0.14 实测两个真实假阳性：
#   1. 世行 IE.PPI.WATR.CD 对 FRA 返回 66 行，value 全是 null，observe 报 ok；
#   2. Eurostat env_ww_con?geo=FRA 的 JSON-stat value 为空，但 extension 里
#      有 14 条注解对象，_nested_rows 把它们当记录，observe 报 ok/14。
# 两者都会发出「字段是看来的」probed 能力卡，比没有卡更坏。
_OBS_COLUMN = re.compile(r"^(value|val|obs_value|observation|数值|观测值)$", re.I)
_EMPTYISH = (None, "", "null", "None", "NA", "nan", "NaN", "N/A")
_JSONSTAT_MAX_ROWS = 20000


def _no_observations(records: list[dict]) -> bool:
    """记录行在、字段也在，但观测值列全空。单行预览另由 sample_only 管。"""
    from .probe import flatten  # 延迟导入：probe 反向依赖本模块

    if len(records) < 2:
        return False
    flat = [flatten(r) for r in records[:100]]
    cols = [c for c in flat[0] if _OBS_COLUMN.match(str(c))]
    if not cols:
        return False
    for col in cols:
        if any(r.get(col) not in _EMPTYISH for r in flat):
            return False
    return True


def _jsonstat_records(doc: dict) -> list[dict] | None:
    """JSON-stat（Eurostat）观测展开。``None`` 表示不是这种格式。

    ``value`` 是按维度线性下标存的稀疏字典，``dimension.*.category.index``
    给出每个位置的国别/年份/单位。只有这里的数据是真的；``extension`` 里的
    注解、链接、脚注一律不算记录。
    """
    if not isinstance(doc, dict):
        return None
    dim, size = doc.get("dimension"), doc.get("size")
    if not (isinstance(dim, dict) and isinstance(size, (list, tuple))
            and "value" in doc):
        return None
    dims = [str(x) for x in (doc.get("id") or list(dim))]
    try:
        sizes = [int(x) for x in size]
    except (TypeError, ValueError):
        return None
    if not dims or len(dims) != len(sizes):
        return None
    pos2label: list[dict[int, str]] = []
    for name in dims:
        cat = (dim.get(name) or {}).get("category") or {}
        idx, labs = cat.get("index"), cat.get("label") or {}
        table: dict[int, str] = {}
        if isinstance(idx, dict):
            for code, pos in idx.items():
                try:
                    table[int(pos)] = str(labs.get(code, code))
                except (TypeError, ValueError):
                    continue
        elif isinstance(idx, list):
            for pos, code in enumerate(idx):
                table[pos] = str(labs.get(code, code))
        pos2label.append(table)
    values = doc.get("value")
    if isinstance(values, dict):
        items: list = list(values.items())
    elif isinstance(values, list):
        items = list(enumerate(values))
    else:
        return []
    out: list[dict] = []
    for key, val in items:
        if val is None:
            continue
        try:
            flat = int(key)
        except (TypeError, ValueError):
            continue
        coords: list[int] = []
        rem = flat
        for s in reversed(sizes):
            coords.append(rem % s if s else 0)
            rem //= s if s else 1
        coords.reverse()
        row = {name: pos2label[i].get(p, str(p))
               for i, (name, p) in enumerate(zip(dims, coords))}
        row["value"] = val
        out.append(row)
        if len(out) >= _JSONSTAT_MAX_ROWS:
            break
    return out


def build_evidence(records: list[dict], meta: dict, url: str,
                   how: str = "original", elapsed_ms: int = 0,
                   notes: list[str] | None = None) -> dict:
    """从已解析的记录造 evidence。纯函数，不发请求。

    probe.py 也调它（新接入时本地已有一份解析结果，不必再取一次），所以这里
    只依赖记录本身，不假设调用者是谁。
    """
    from .probe import flatten, _infer_type  # 延迟导入：probe 反向依赖本模块

    n = len(records)
    # 抽样要跨全程，不能只取前几行。OWID 的 Entity 列前 12 行全是
    # "Afghanistan"——按前 N 行取样，地理轴会被判成「阿富汗」，而它其实是
    # 全球面板。等距抽样才能看见这份数据真正覆盖的范围。
    if n <= SAMPLE_RECORDS:
        picked = records
    else:
        step = (n - 1) / (SAMPLE_RECORDS - 1)
        idx = sorted({int(round(i * step)) for i in range(SAMPLE_RECORDS)})
        picked = [records[i] for i in idx]
    flat = [flatten(r) for r in picked]

    columns: list[str] = []
    for row in flat:
        for key in row:
            if key not in columns:
                columns.append(key)
                if len(columns) >= MAX_FIELDS:
                    break

    fields: list[dict] = []
    samples: dict[str, list[str]] = {}
    for col in columns:
        vals = [str(r.get(col, "") or "") for r in flat]
        nonempty = [v for v in vals if v not in ("", "null", "None")]
        geoish = bool(_GEO_NAME.search(col))
        keep = nonempty[:12] if geoish else nonempty[:2]
        if keep:
            samples[col] = [v[:48] for v in keep]
        fields.append({
            "name": col,
            "type": _infer_type(nonempty[:200]),
            "nonnull": len(nonempty),
            "distinct": len(set(nonempty[:200])),
            "synthetic": col.startswith("__"),
        })

    time_fields = [c for c in columns if _TIME_NAME.match(c)]
    shape = "tabular" if len(columns) <= 12 else "wide"
    if len(columns) == 2 and {"paragraph_no", "text"} <= set(columns):
        shape = "text"

    # 单行 / preview 端点只是样本，不是全库。UN Comtrade 的预览地址写死了
    # reporterCode=156&period=2022&partnerCode=276，实测只得到 1 行，却据此
    # 生成过「覆盖联合国商品贸易统计数据库」的能力声明。标出来，让能力卡
    # 和路牌都能说清「这只是一行样本」。
    sample_only = (len(records) <= 1 or "preview" in (url or "").lower())
    base_note = ("字段取自本次响应的键集合，样本值全程等距抽 %d 行；"
                 "分页接口可能还有更多字段" % min(len(records), SAMPLE_RECORDS))
    if sample_only:
        base_note += "；注意：本次只取到单行/预览样本，字段不代表全库覆盖范围"
    ev = {
        "status": "ok",
        "harvest_version": HARVEST_VERSION,
        "endpoint": url,
        "resolved_how": how,
        "http_status": meta.get("http_status", 0),
        "content_type": meta.get("content_type", ""),
        "bytes": meta.get("bytes", 0),
        "records": len(records),
        "shape": shape,
        "field_count": len(columns),
        "fields": fields,
        "samples": samples,
        "time_fields": time_fields,
        "probed_at": _now(),
        "elapsed_ms": elapsed_ms,
        "note": base_note,
    }
    if meta.get("credential"):
        ev["credential"] = meta["credential"]  # 只记 host 标签，绝不记值
    if sample_only:
        # 只在为真时落盘：绝大多数 evidence 不该为一个 False 多带一个键，
        # 契约（sources/<id>/source.json）里也不该出现容易被误读的 'sample' 字样。
        ev["sample_only"] = True
    if notes:
        ev["note"] = "；".join(notes) + "。" + ev["note"]
    return ev


def observe(url: str, timeout: float = TIMEOUT, allow_private: bool = False,
            how: str = "original", headers: dict | None = None) -> dict:
    """取一次数并造 evidence。失败抛 HarvestError（带状态码）。"""
    from . import probe

    started = time.perf_counter()
    raw, meta = _get(url, timeout, allow_private, headers=headers)
    kind = _sniff(raw, meta.get("content_type", ""))
    if kind == "binary":
        from . import tables  # 延迟导入：只在真的拿到文件时才加载容器解析

        parsed = tables.parse(raw, meta.get("content_type", ""))
        if not parsed["ok"]:
            raise HarvestError(
                "binary_nontable",
                f"{parsed['container']}：{parsed['note']}"[:180],
                http_status=meta.get("http_status", 0),
                content_type=meta.get("content_type", ""), size=len(raw))
        info = [f"从 {parsed['container']} 容器解出表格：{parsed['note']}"]
        if parsed.get("archive_files"):
            info.append("包内文件：" + "、".join(parsed["archive_files"][:6]))
        return build_evidence(
            parsed["records"], meta, url, how=how,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            notes=info,
        )
    if kind == "service_doc":
        raise HarvestError("service_doc", "返回 WMS/WFS/CSW 服务文档，里面是图层目录不是记录",
                           http_status=meta.get("http_status", 0),
                           content_type=meta.get("content_type", ""), size=len(raw))
    if kind == "rdf":
        raise HarvestError("rdf", "返回 RDF/Turtle/JSON-LD 元数据，描述的是记录之间的关系，不是记录表",
                           http_status=meta.get("http_status", 0),
                           content_type=meta.get("content_type", ""), size=len(raw))
    if kind == "html":
        raise HarvestError("html", "返回 HTML 网页，不是结构化数据",
                           http_status=meta.get("http_status", 0),
                           content_type=meta.get("content_type", ""), size=len(raw),
                           raw=raw)

    notes: list[str] = []
    if kind == "xml":
        # XML 仍可能是数据（Atom/RSS/RDF），交给 parse_records 试一次
        notes.append("响应为 XML，可能有嵌套结构")
    if _printable_ratio(raw) < 0.85:
        raise HarvestError("unparsed", "响应不是可解析的文本或结构化数据",
                           http_status=meta.get("http_status", 0), size=len(raw))

    # 单个 JSON 对象（没有记录数组）不是数据集。前端页面配置、i18n 资源、
    # 单个状态回包都会长成这样——{"src":…,"sizes":…,"type":…} 被当成
    # 「一条记录、三个字段」收下，比接不进来更坏。
    stripped = raw.lstrip()[:1]
    if stripped in (b"{", b"["):
        try:
            doc = json.loads(raw.decode("utf-8-sig", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            doc = None
        if isinstance(doc, dict):
            # JSON-stat 必须在通用嵌套扫描之前判：它的 extension 注解是
            # list[dict]，会被 _nested_rows 当成目录记录，把空结果报成 ok。
            jsonstat = _jsonstat_records(doc)
            if jsonstat is not None:
                if not jsonstat:
                    raise HarvestError(
                        "empty", "JSON-stat 响应的 value 为空，没有任何观测值",
                        http_status=meta.get("http_status", 0), size=len(raw))
                notes.append(f"按 JSON-stat 的 dimension/value 展开为 {len(jsonstat)} 条观测")
                return build_evidence(
                    jsonstat, meta, url, how=how,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    notes=notes,
                )
            has_rows = any(
                isinstance(v, list) and any(isinstance(x, dict) for x in v[:5])
                for v in doc.values()
            )
            rdfish = any(str(k).startswith("@") or "://" in str(k) for k in doc)
            if rdfish:
                raise HarvestError("rdf", "返回 JSON-LD 元数据（键是 RDF 谓词，不是字段名）",
                                   http_status=meta.get("http_status", 0), size=len(raw))
            if not has_rows:
                nested = _nested_rows(doc)
                if nested:
                    notes.append("从响应的嵌套记录数组解析（目录类 API 的常见形态）")
                    return build_evidence(
                        nested, meta, url, how=how,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        notes=notes,
                    )
                raise HarvestError("unparsed", "返回单个 JSON 对象，没有记录数组",
                                   http_status=meta.get("http_status", 0), size=len(raw))
        elif isinstance(doc, list) and len(doc) == 1 and isinstance(doc[0], dict):
            only = doc[0]
            if len(only) <= 8 and any(isinstance(v, dict) for v in only.values()):
                raise HarvestError(
                    "unparsed",
                    "只有一个 JSON 对象且含嵌套结构，像是页面配置而不是记录集",
                    http_status=meta.get("http_status", 0), size=len(raw))

    try:
        records, fmt, _raw_file = probe.parse_records(raw, meta)
    except ValueError as exc:
        msg = str(exc)
        status = "html" if "HTML" in msg else "unparsed"
        raise HarvestError(status, msg[:120], http_status=meta.get("http_status", 0),
                           size=len(raw)) from exc

    if not records:
        raise HarvestError("empty", "解析成功但没有记录",
                           http_status=meta.get("http_status", 0), size=len(raw))
    if _no_observations(records):
        raise HarvestError(
            "empty", "接口返回了记录行，但观测值列全为空（该指标对该地区无数据）",
            http_status=meta.get("http_status", 0), size=len(raw))
    if fmt != "lines":
        cols: list[str] = []
        for row in records[:20]:
            for key in probe.flatten(row):
                if key not in cols:
                    cols.append(key)
        if cols and sum(1 for c in cols if c.startswith("@") or "://" in c) >= max(2, len(cols) // 2):
            raise HarvestError("rdf", "记录是 JSON-LD 资源（@id/@type），不是数据行",
                               http_status=meta.get("http_status", 0), size=len(raw))
        if _is_meta_only(cols):
            raise HarvestError(
                "empty",
                "接口只返回分页/状态信息，没有实际观测（例如该指标本身无数据）",
                http_status=meta.get("http_status", 0), size=len(raw))
    if fmt == "lines":
        if kind == "xml":
            # XML（Atom/RDF/CSW）里有记录，但键在标签里，不是「一行一段」。
            # 硬按文本行接入会造出 paragraph_no/text 两个假字段。
            raise HarvestError("xml", "XML 文档，记录藏在标签结构里；需按 XML 解析，暂按文本接入会造假字段",
                               http_status=meta.get("http_status", 0), size=len(raw))
        long_line = max((len(str(v)) for r in records[:5] for v in r.values()), default=0)
        if len(records) < 3 and long_line > 2000:
            raise HarvestError("unparsed", "全文只有一两个超长片段，不是记录型数据",
                               size=len(raw))
        notes.append("按文本行接入（每行一段）")

    return build_evidence(
        records, meta, url, how=how,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        notes=notes,
    )


# ── 端点改写：只写公开的、可复核的导出约定 ───────────────────────────

def _rewrites(url: str) -> list[tuple[str, str]]:
    """同一份数据在平台约定下的直取地址。不做任何页面内容猜测。"""
    out: list[tuple[str, str]] = []
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    base = f"{parsed.scheme}://{parsed.netloc}"

    # Opendatasoft（odwb.be / data.leicester.gov.uk / opendata.potsdam.de …）
    # 两个入口都认：网页 ``/explore/dataset/<slug>/`` 与接口
    # ``/api/explore/v2.1/catalog/datasets/<slug>/exports/<fmt>``。后者常写着
    # ``exports/xlsx``——同一份数据明明有 JSON 形态，没必要因为容器是 Excel
    # 就说「取不到」。
    ods = (re.search(r"/explore/dataset/([^/?#]+)", path)
           or re.search(r"/catalog/datasets/([^/?#]+)", path))
    if ods:
        slug = ods.group(1)
        out.append((f"{base}/api/explore/v2.1/catalog/datasets/{slug}/exports/json?limit=200",
                    "platform:opendatasoft"))

    # CSO PxStat：同一张表另有 CSV 形态；XLSX/PX 只是容器不同
    m = re.search(r"/PxStat\.Data\.Cube_API\.ReadDataset/([^/]+)/", path)
    if m and "/CSV/" not in path:
        out.append((f"{base}/public/api.restful/PxStat.Data.Cube_API.ReadDataset/"
                    f"{m.group(1)}/CSV/1.0/en", "platform:pxstat-csv"))

    # 世界银行：不给 per_page 时默认页很小，容易只拿到空观测
    if host == "api.worldbank.org" and "per_page" not in url:
        sep = "&" if "?" in url else "?"
        out.append((f"{url}{sep}per_page=100", "platform:worldbank-pagesize"))

    # data.gouv.fr 的 dataset 页 → 资源列表 API
    m = re.search(r"^/api/1/datasets/([0-9a-f-]{8,})/?$", path)
    if host.endswith("data.gouv.fr") and m:
        out.append((f"{base}/api/1/datasets/{m.group(1)}", "platform:datagouv-meta"))

    return out


def _data_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """门户页里明写的下载链接。只认同站、且后缀就是数据格式的链接——
    这是页面自己给出的入口，不是我们猜的接口。"""
    base = urllib.parse.urlparse(base_url)
    base_host = (base.hostname or "").lower()
    root = ".".join(base_host.split(".")[-2:]) if base_host else ""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href in re.findall(r'href=["\']([^"\']{3,300})["\']', html):
        h = href.replace("&amp;", "&").strip()
        if h.startswith(("javascript:", "#", "mailto:")):
            continue
        absolute = urllib.parse.urljoin(base_url, h)
        p = urllib.parse.urlparse(absolute)
        host = (p.hostname or "").lower()
        if not host or not (host == base_host or host.endswith("." + root)):
            continue
        if not re.search(r"\.(csv|json|ndjson|geojson)(\?|$)", p.path, re.I):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append((absolute, "page:download-link"))
    out.sort(key=lambda x: 0 if re.search(r"\.csv", x[0], re.I) else 1)
    return out[:2]


def _attempts(url: str, timeout: float, allow_private: bool):
    """按顺序产出 (候选地址, 来源说明)。先原地址，再平台约定。"""
    yield url, "original"
    for cand, how in _rewrites(url):
        if cand != url:
            yield cand, how


def endpoint_of(item: dict) -> str:
    """这条目录条目有没有真正的取数地址。

    ``data_url`` 有就用。没有的话**不能**拿 ``source_url`` 顶替——那通常是
    门户首页（https://www.ndsms.cn/）。拿首页去探测，只会得到「这是网页」，
    然后把「目录没登记地址」误报成「该源不给数据」，两件事完全不同。

    只有 source_url 本身就像接口（``/api/``、``api.`` 开头、带查询参数、
    或以数据格式结尾）才当取数地址试。
    """
    du = (item.get("data_url") or "").strip()
    if du:
        return du
    su = (item.get("source_url") or "").strip()
    if not su:
        return ""
    p = urllib.parse.urlparse(su)
    looks_api = (
        "/api/" in p.path
        or (p.hostname or "").startswith("api.")
        or bool(p.query)
        or bool(re.search(r"\.(csv|json|ndjson|geojson|xlsx?|tsv)$", p.path, re.I))
    )
    return su if looks_api else ""


def resolve(item: dict, timeout: float = TIMEOUT, allow_private: bool = False) -> dict:
    """对一个目录条目做实测，返回 evidence（无论成功失败都有明确状态）。

    失败也要有信息量：``attempts`` 里逐条记下试过什么、各自什么状态，
    这样「这条为什么补不了卡」是可查的，不是一句「失败」。
    """
    url = endpoint_of(item)
    if not url:
        # 已经逐条核实过的结论，不再退化成「目录没填地址」。核实记录带时间戳
        # 与 attempts，想强制重查就清掉 endpoint_basis。门户另有一类：它本来
        # 就不持有内容级数据，取数地址写在 portal_api_url 而不是 data_url
        # ——后者一旦非空，kind_of 会把门户判成数据集，整棵 hub 挂载树会散。
        if item.get("endpoint_basis") in ("verified_no_public_endpoint",
                                          "portal_catalog_api_verified"):
            prev = dict(item.get("harvest") or {})
            prev.update({"harvest_version": HARVEST_VERSION, "endpoint": ""})
            prev.setdefault("status", STATUS_LABELS["no_public_endpoint"])
            prev.setdefault("note", STATUS_LABELS.get(prev["status"], prev["status"]))
            return prev
        src = (item.get("source_url") or "").strip()
        return {"status": "no_url", "harvest_version": HARVEST_VERSION,
                "endpoint": "", "attempts": [],
                "note": "目录只登记了来源页" + (f"（{_short_url(src)}）" if src else "")
                        + "，没有可直接取数的地址",
                "probed_at": _now()}

    attempts: list[dict] = []
    html_body: str = ""
    first_status = ""
    first_note = ""
    # 浏览器 UA 重试若真的取到了内容（哪怕是 PDF/空表），最终状态就该说
    # 「内容是什么」，而不是继续报第一次的「被 WAF 挡住」——那会让人以为
    # 端点完全不可达，实际上它只挡默认 UA。
    content_status = ""
    content_note = ""
    CONTENT_STATES = ("binary_nontable", "html", "empty", "unparsed", "xml",
                      "rdf", "service_doc", "no_public_endpoint")

    for cand, how in _attempts(url, timeout, allow_private):
        if len(attempts) >= MAX_CANDIDATES:
            break
        try:
            ev = observe(cand, timeout=timeout, allow_private=allow_private, how=how)
        except HarvestError as exc:
            attempts.append(exc.as_dict(cand, how))
            if not first_status:
                first_status, first_note = exc.status, exc.note
            if exc.status in CONTENT_STATES and (not content_note or how == "retry:browser-ua"):
                content_status, content_note = exc.status, exc.note
            if exc.status in ("blocked", "too_large"):
                # 安全策略与体积是硬边界，换地址也没用。auth / upstream_blocked
                # 不能提前断：换一个普通浏览器 UA 常常就能拿到公开数据。
                break
            if exc.status == "html" and how == "original" and exc.raw:
                html_body = exc.raw[:600_000].decode("utf-8", "replace")
            continue
        ev["attempts"] = attempts
        ev["sha256"] = ""      # 由调用方决定是否留痕（大批量时不存全量哈希）
        return ev

    # 401/403 之后再试一次普通浏览器 UA：不少公共数据门户只挡默认爬虫 UA，
    # 端点本身是公开的。结果写进 attempts，是「怎么拿到的」可复核。
    if first_status in ("auth", "upstream_blocked") and url.startswith(("http://", "https://")):
        try:
            ev = observe(url, timeout=timeout, allow_private=allow_private,
                         how="retry:browser-ua", headers=BROWSER_HEADERS)
        except HarvestError as exc:
            attempts.append(exc.as_dict(url, "retry:browser-ua"))
            if exc.status in CONTENT_STATES:
                content_status, content_note = exc.status, exc.note
        else:
            ev["attempts"] = attempts
            return ev

    # 全部失败：把网页里明写的下载链接再试一遍（页面自己给的入口）
    if html_body:
        for cand, how in _data_links(html_body, url):
            if len(attempts) >= MAX_CANDIDATES + 2:
                break
            try:
                ev = observe(cand, timeout=timeout, allow_private=allow_private, how=how)
            except HarvestError as exc:
                attempts.append(exc.as_dict(cand, how))
                continue
            ev["attempts"] = attempts
            return ev

    status = content_status or first_status or "unparsed"
    label = STATUS_LABELS.get(status, status)
    detail = content_note or first_note
    note = f"{label}：{detail}" if detail and detail != label else label
    if len(attempts) > 1:
        note += f"（共试 {len(attempts)} 个地址）"
    return {
        "status": status,
        "harvest_version": HARVEST_VERSION,
        "endpoint": url,
        "attempts": attempts,
        "note": note,
        "probed_at": _now(),
    }


def sha256_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def summarize(ev: dict) -> str:
    """一句话状态，给报告和后台列表用。"""
    st = ev.get("status", "")
    if st == "ok":
        return f"实测 {ev.get('records', 0)} 行 × {ev.get('field_count', 0)} 字段"
    return STATUS_LABELS.get(st, st)


def dumps(ev: dict) -> str:
    return json.dumps(ev, ensure_ascii=False, sort_keys=True)
