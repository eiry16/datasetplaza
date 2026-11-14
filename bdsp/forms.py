"""接入形态分类与档案模板（source access forms）。

问题：244 条目录条目里 228 条只有机械推导的最小档案，等于「平台知道有这个源，
但 agent 不知道该怎么连」。上一版把补齐推给「收录侧」，这是错的——**发布方
一定公开了入口形态**，只是需要一次实测把它归到有限几种形态里。

两个正交的轴，缺一不可：

    形态 form —— 这个源怎么把数据交出来（文件 / 门户检索 / 统计接口 / MCP /
                  Agent 卡 / 可信空间 / 只有网页）
    准入 gate —— 取它之前要过什么（开放 / 注册 / 授权 / 付费 / 反爬闸门 /
                  本机不可验证）

「Wind 是 web_only + paid」「BeaconMFG 是 stat_api + open（有配额）」
「data.europa.eu 是 portal_api + open」——一个坐标就说清了 agent 该怎么动手。

每个 form 有一张必填表：**新源上线时按形态填这张表，就得到一份可用档案**。
缺字段不是「未登记」，而是拿到具体一条：portal_api 缺 endpoint，agent 就
不知道往哪发检索请求。
"""
from __future__ import annotations

# ── 形态 ────────────────────────────────────────────────────────────────
FORMS: dict[str, dict] = {
    "file": {
        "label": "直链文件",
        "agent_action": "按 URL 下载整表，再本地筛选/聚合",
        "required": ["endpoint", "format"],
        # encoding 只对文本表有意义（xlsx/pdf/zip 没有编码），进字段说明即可
        "recommended": ["size_bytes"],
        "fields": {
            "endpoint": "文件直链（单个资源的下载地址）",
            "format": "csv / json / xlsx / pdf / zip / geojson / rdf / 空间与二进制格式",
            "size_bytes": "文件大小（大表要提醒超时与内存）",
            "sheet": "xlsx/xls 的 sheet 名或序号",
            "encoding": "文本编码（中文表常见 GBK/GB18030）",
        },
    },
    "portal_api": {
        "label": "门户检索 API",
        "agent_action": "先按关键词检索拿到该数据集的 id/slug，再按 id 取数",
        # 必填只到「能按关键词检索」为止：拿到检索入口后，agent 本来就要先
        # 搜一遍才知道该取哪张表。record_lookup 是加速项，不是通行证。
        "required": ["endpoint", "query_param", "response_path"],
        "recommended": ["record_lookup", "pagination"],
        "fields": {
            "endpoint": "检索端点",
            "query_param": "关键词参数名（q / where / fulltext / $where）",
            "record_lookup": "按 id/slug 取单表记录的端点模板",
            "response_path": "结果数组在响应里的位置（如 result.results）",
            "pagination": "分页参数与上限（limit/offset、rows/start、page_size）",
        },
    },
    "stat_api": {
        "label": "专用统计接口",
        # 「通常不需要先检索」对 BeaconMFG 这类带检索词参数的专用接口不成立：
        # 它就是 POST /v1/search 直接查。写成「按参数取值」两种都覆盖。
        "agent_action": "按接口参数直接取值（指标码 / 表码 / 资源路径 / 检索词），不必先去门户找数据集",
        "required": ["endpoint", "params_note"],
        "recommended": ["response_path", "pagination"],
        "fields": {
            "endpoint": "接口基址（含必需的路径段）",
            "params_note": "必填参数的含义（如 indicator/country/date、flowRef/key）",
            "response_path": "数据数组位置（如 [1] 是记录数组）",
            "pagination": "分页参数与上限（per_page/page、start/rows）",
        },
    },
    "mcp": {
        "label": "MCP 服务",
        "agent_action": "挂载该 MCP，按工具名直接调用（平台不代调）",
        "required": ["mcp.name"],
        "fields": {
            "mcp.name": "服务名",
            "mcp.command": "启动命令（npx/uvx/二进制）",
            "mcp.args": "启动参数",
            "mcp.url": "远程 MCP 地址（HTTP 传输时）",
            "mcp.tools": "工具清单（agent 用它决定调哪个）",
        },
    },
    "agent_card": {
        "label": "Agent 卡 / llms.txt",
        "agent_action": "读卡片，按卡片声明的能力与协议调用",
        "required": ["card_url"],
        "fields": {"card_url": "agent-card.json / llms.txt 的位置",
                   "capabilities": "卡片里声明的能力"},
    },
    "trusted_space": {
        "label": "可信数据空间",
        "agent_action": "先入空间（身份与合约），再按空间内接口取数",
        "required": ["space_url", "onboarding"],
        "fields": {"space_url": "空间入口", "onboarding": "入空间流程（身份、合约、审批）",
                   "connector": "连接器/客户端要求"},
    },
    "web_only": {
        "label": "只有网页（人工下载）",
        "agent_action": "把落地页与下载路径交给用户或浏览器工具，不能声称能自动取",
        "required": ["landing_url", "how_to"],
        "fields": {"landing_url": "数据集/门户口页", "how_to": "页面上怎么点到下载",
                   "formats": "页面提供的导出格式"},
    },
    "gated": {
        "label": "有接口但被闸门挡住",
        "agent_action": "先解决闸门（换 UA / 登录 / 申请 key），再按原接口取",
        "required": ["landing_url", "blocker"],
        "fields": {"landing_url": "门户首页", "blocker": "挡住的形态（HTTP 码/人机验证/登录墙）",
                   "pass_note": "已知怎么过（浏览器 UA、注册、申请）"},
    },
    "unverified": {
        "label": "本机不可验证",
        "agent_action": "照源方文档直连；平台的不可达只是这台机器出不去",
        "required": ["landing_url", "why"],
        "fields": {"landing_url": "门户页", "why": "不可达原因（DNS/超时/证书）",
                   "retry_note": "换网络或换节点后重试"},
    },
}

# ── 准入 ────────────────────────────────────────────────────────────────
GATES: dict[str, dict] = {
    "open": {"label": "开放取用",
             "auth_kind": "none", "pricing": {"model": "free", "note": "开放取用"},
             "next_step": "direct"},
    "registered": {"label": "注册后取用",
                   "auth_kind": "token", "pricing": {"model": "free-with-registration",
                                                     "note": "注册后可取，通常免费"},
                   "next_step": "register"},
    "authorized": {"label": "需书面授权",
                   "auth_kind": "authorization",
                   "pricing": {"model": "authorization",
                               "note": "需源方书面授权，条款由源方定"},
                   "next_step": "request_authorization"},
    "paid": {"label": "需付费",
             "auth_kind": "account",
             "pricing": {"model": "subscription",
                         "note": "源方商业订阅；平台不代销、不代收、不代持凭证"},
             "next_step": "contact_source"},
    "waf": {"label": "反爬闸门",
            "auth_kind": "none", "pricing": {"model": "free", "note": "公开但设有访问闸门"},
            "next_step": "use_browser_ua"},
    "unreachable": {"label": "本机不可达",
                    "auth_kind": "unknown", "pricing": {"model": "unknown", "note": ""},
                    "next_step": "retry_elsewhere"},
}

# 门户软件族 → portal_api；统计接口族 → stat_api
PORTAL_FAMILIES = {"ckan", "opendatasoft_v2", "socrata", "dkan", "dataverse",
                   "dcat_hub", "data_gouv", "arcgis", "csw", "oai_pmh", "sparql"}
STAT_FAMILIES = {"worldbank_v2", "openalex", "eurostat", "pxstat", "cbs_odata",
                 "genesis", "comtrade", "arxiv", "invenio", "figshare", "kaggle",
                 "nbs_easyquery", "owid_csv", "statcube", "un_data", "fred",
                 "sdmx"}

# 文件形态的扩展名 → 规范化格式名
# 文件格式表：源方声明的格式名 → 归一值。香港那批里出现过的空间格式
# （gml/geotiff/geopackage/fgdb/shp）与二进制快照（jpeg/png/obj/max）都要
# 认，否则档案只能说「这是一份文件、但不知道是什么文件」——那种档案 agent
# 拿到手仍然动不了。
FILE_FORMATS = {"csv": "csv", "tsv": "tsv", "json": "json", "jsonld": "jsonld",
                "geojson": "geojson", "xlsx": "xlsx", "xls": "xls", "zip": "zip",
                "pdf": "pdf", "xml": "xml", "rdf": "rdf", "ttl": "ttl",
                "parquet": "parquet", "shp": "shp", "kmz": "kmz", "kml": "kml",
                "nc": "nc", "gz": "gz", "tar": "tar", "tier": "txt",
                "txt": "txt", "gml": "gml", "fgdb": "fgdb", "rss": "rss",
                "geotiff": "geotiff", "tif": "geotiff", "tiff": "geotiff",
                "geopackage": "geopackage", "gpkg": "geopackage",
                "png": "png", "jpeg": "jpeg", "jpg": "jpeg",
                "nmea": "nmea", "las": "las", "laz": "laz", "obj": "obj",
                "max": "max", "rinex": "rinex", "rtcm": "rtcm", "docx": "docx"}

# 门户族的取单表端点模板：{id} 是该门户的 dataset id / slug
LOOKUP_TPL = {
    "ckan": "https://{host}/api/3/action/package_show?id={id}",
    "opendatasoft_v2": "https://{host}/api/explore/v2.1/catalog/datasets/{id}",
    "socrata": "https://{host}/api/views/{id}.json",
    "dkan": "https://{host}/api/1/dataset/{id}",
    "dataverse": "https://{host}/api/datasets/:persistentId/?persistentId={id}",
    "dcat_hub": "https://{host}/api/hub/search/datasets/{id}",
    "data_gouv": "https://{host}/api/1/datasets/{id}/",
    "arcgis": "https://{host}/arcgis/rest/services?f=json",
    "csw": "https://{host}/geonetwork/srv/eng/csw",
    "oai_pmh": "https://{host}/oai?verb=GetRecord&metadataPrefix=oai_dc&identifier={id}",
    "sparql": "https://{host}/sparql",
}


URL_FAMILY = [
    ("pxstat", r"PxStat"),
    ("opendatasoft_v2", r"/api/explore/v2\.1/|/explore/dataset/|/exports/"),
    ("ckan", r"/api/3/action/"),
    ("worldbank_v2", r"api\.worldbank\.org/v2/"),
    ("owid_csv", r"ourworldindata\.org/grapher/"),
    ("nbs_easyquery", r"easyquery\.htm"),
    ("comtrade", r"comtradeapi\.un\.org|comtradeplus\.un\.org"),
    ("sdmx", r"/api/dissemination/|sdmx-json|/rest/v2/dataflow"),
    ("genesis", r"genesisws/rest|/genesis/v1/"),
    ("eurostat", r"/eurostat/api/"),
    ("data_gouv", r"data\.gouv\.fr/api/1/"),
    ("dcat_hub", r"data\.europa\.eu/api/hub/"),
    ("socrata", r"/resource/[a-z0-9]{4}-[a-z0-9]{4}"),
]


def family_from_url(url: str) -> str:
    """从 URL 形态判接口族。

    站点探测会失败（WAF/本机出不去），而条目自带的 URL 往往已经把答案写在
    路径里——`ws.cso.ie/public/api.restful/PxStat.Data.Cube_API...` 就是
    PxStat 立方体接口。这时按 URL 判，比按站点判更准。
    """
    import re as _re
    for fam, pat in URL_FAMILY:
        if _re.search(pat, url or "", _re.I):
            return fam
    return ""


def resolve(item: dict, host_ev: dict | None = None,
            entry_ev: dict | None = None) -> dict:
    """按实测证据给条目定 (form, gate)。证据优先级：实测 > 目录声明。

    顺序不是随意的：先认 AI 层（有 MCP 卡就不用再谈接口），再认门户/统计
    接口（能检索比能下载强），再认直链文件，最后才落到「被挡」「不可验证」
    「只有网页」——后三种是**结论**，不能被前面的猜测覆盖。
    """
    h = host_ev or {}
    e = entry_ev or {}
    fam = str(h.get("family") or "")
    lvl = str(item.get("access_level") or "open")
    fmt = FILE_FORMATS.get(str(e.get("format") or "").lower(), "")
    st = e.get("status")
    ufam = family_from_url(str(item.get("data_url") or ""))

    if h.get("mcp"):
        form, why = "mcp", "站点声明了 MCP 服务"
    elif h.get("agent_card"):
        form, why = "agent_card", "站点提供 agent-card/llms.txt"
    elif h.get("trusted_space"):
        form, why = "trusted_space", "站点属于可信数据空间（需入空间）"
    elif fam in PORTAL_FAMILIES:
        form, why = "portal_api", h.get("note") or f"{fam} 门户接口实测可用"
    elif fam in STAT_FAMILIES:
        form, why = "stat_api", h.get("note") or f"{fam} 统计接口实测可用"
    elif ufam in PORTAL_FAMILIES:
        form, why = "portal_api", f"条目 URL 形态属 {ufam} 门户"
    elif ufam in STAT_FAMILIES:
        form, why = "stat_api", f"条目 URL 形态属 {ufam} 接口"
    elif fmt and (st in (200, 206) or not st):
        form, why = "file", f"data_url 直出 {fmt}"
    elif h.get("blocked"):
        form, why = "gated", f"接口被闸门挡住（{h['blocked']}）"
    elif h.get("unverifiable"):
        form, why = "unverified", "本机两条路都不通"
    else:
        form, why = "web_only", "只有可浏览的网页，未见机器接口"

    if h.get("blocked"):
        gate = "waf"
    elif h.get("unverifiable"):
        gate = "unreachable"
    else:
        gate = lvl if lvl in ("open", "registered", "authorized", "paid") else "open"

    return {"form": form, "gate": gate, "why": why,
            "host_family": fam, "entry_format": fmt,
            "probed": {"host": h.get("endpoint") or h.get("landing") or "",
                       "http_status": st if st is not None else h.get("http_status")}}


def template(form: str) -> dict:
    """新源上线时按形态要填的表——这就是「标准模版」。"""
    spec = FORMS.get(form)
    if not spec:
        return {}
    return {"form": form, "label": spec["label"],
            "agent_action": spec["agent_action"],
            "required": spec["required"], "fields": spec["fields"]}


def missing_fields(prof: dict) -> list[str]:
    """按形态查必填项。返回的是**具体缺哪一项**，可直接当运营待办。"""
    spec = FORMS.get(str(prof.get("form") or ""))
    if not spec:
        return [f"form 未定（{'/'.join(FORMS)}）"]
    miss = []
    for path in spec["required"]:
        cur = prof
        ok = True
        for seg in path.split("."):
            if isinstance(cur, dict) and cur.get(seg):
                cur = cur[seg]
            else:
                ok = False
                break
        if not ok:
            miss.append(path)
    return miss


def recommended_missing(prof: dict) -> list[str]:
    """加速项缺失：不影响能不能用，但会多绕一步（比如得先检索才知道表 id）。"""
    spec = FORMS.get(str(prof.get("form") or "")) or {}
    miss = []
    for path in spec.get("recommended") or []:
        cur, ok = prof, True
        for seg in path.split("."):
            if isinstance(cur, dict) and cur.get(seg):
                cur = cur[seg]
            else:
                ok = False
                break
        if not ok:
            miss.append(path)
    return miss


def taxonomy(profiles: list[dict]) -> dict:
    """按形态/准入统计覆盖情况。dp_stats 用它把「还差什么」变成可见的账。"""
    from collections import Counter
    forms = Counter(p.get("form") or "(未分类)" for p in profiles)
    gates = Counter(p.get("gate") or "(未分类)" for p in profiles)
    incomplete = Counter()
    for p in profiles:
        for f in missing_fields(p):
            incomplete[f"{p.get('form')}·{f}"] += 1
    weak = Counter()
    for p in profiles:
        for f in recommended_missing(p):
            weak[f"{p.get('form')}·{f}"] += 1
    return {
        "forms": {k: {"count": v, "label": FORMS.get(k, {}).get("label", "未分类"),
                      "agent_action": FORMS.get(k, {}).get("agent_action", "")}
                  for k, v in forms.most_common()},
        "gates": dict(gates.most_common()),
        "incomplete_fields": dict(incomplete.most_common(12)),
        "recommended_missing": dict(weak.most_common(8)),
        "complete": sum(1 for p in profiles if not missing_fields(p)),
        "total": len(profiles),
    }
