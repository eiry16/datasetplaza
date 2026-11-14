"""接入档案（connection profile）：这个源怎么用，写清楚，交给 agent 自己去取。

平台的角色边界在这里落定（0.22）：

    平台指路 —— 声明哪个源有你要的东西、怎么连、要什么凭证、限额多少；
    agent 跑腿 —— 拿凭证、发请求、翻页、清洗、判精度。

所以档案只描述**怎么用**，绝不代取：

* 平台不持有用户凭证，不代发请求，不落源方数据快照；
* 档案里的 endpoint / method / headers / body 都是**源方公开的接入契约**，
  不是平台自己的代理地址；
* 源方精度、口径、字段语义由源方负责；平台只把已知的坑写在 notes 里
  提醒调用方，不做加工，也不因此否决这个源。

档案来源分三级（basis），必须如实标注：

    source    源方文档/README 里明写的接入方式
    operator  操作员按实测补的（有探测记录支撑）
    derived   从目录字段（data_url / access_level / mcp_hint）机械推导的
              最小档案——只有入口和鉴权类型，没有请求示例，因为不编造

`derive()` 的硬规矩：**拿不准就留空**。宁可给一个只有 url 的档案，
也不能凭想象写一个 curl 让 agent 照着撞墙。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import forms as formsmod

PROFILE_VERSION = "prof-2"

# 协议：决定 agent 拿到档案后调什么。
PROTOCOLS = ("http-json", "http-file", "mcp", "sdk", "download", "portal", "unknown")

# 鉴权类型：决定 agent 要去哪一步拿凭证。
AUTH_KINDS = ("none", "api-key", "token", "partner-token", "oauth2",
              "account", "authorization", "unknown")

_AUTH_LABEL = {
    "none": "无需凭证",
    "api-key": "API Key（源方签发）",
    "token": "访问令牌（源方签发）",
    "partner-token": "合作方令牌（需先成为 partner）",
    "oauth2": "OAuth 2.0 授权",
    "account": "源方账号（登录后使用）",
    "authorization": "书面授权 / 机构订阅",
    "unknown": "未核实",
}

# access_level → 档案里的鉴权类型。这是机械映射，不是猜测：
# 目录已经声明了「需鉴权/需授权/需付费」，档案要把它翻译成 agent 能执行的步骤。
_LEVEL_AUTH = {
    "open": "none",
    "registered": "token",
    "authorized": "authorization",
    "paid": "account",
}

_LEVEL_PRICING = {
    "open": {"model": "free", "note": "开放取用"},
    "registered": {"model": "free-with-registration", "note": "注册后可取，通常免费"},
    "authorized": {"model": "authorization", "note": "需源方书面授权，价格与条款由源方定"},
    "paid": {"model": "subscription", "note": "源方商业订阅；平台不代销、不代收、不代持凭证"},
}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _guess_protocol(item: dict) -> str:
    """从**已有字段**推协议，不猜语义。"""
    hint = str(item.get("mcp_hint") or "")
    dev = item.get("developer") or {}
    contact = str(dev.get("contact") or "") if isinstance(dev, dict) else ""
    if "mcp" in hint.lower() or "mcp" in contact.lower():
        return "mcp"
    url = str(item.get("data_url") or item.get("portal_api_url") or "")
    low = url.lower()
    if not low:
        return "portal" if item.get("source_url") else "unknown"
    if low.endswith((".csv", ".tsv", ".xlsx", ".xls", ".json", ".zip", ".parquet")):
        return "http-file"
    if "api" in low or low.rstrip("/").endswith(("/search", "/query", "/v1", "/v2")):
        return "http-json"
    return "portal"


def derive(item: dict) -> dict:
    """无手写档案时的最小可用档案：只写能从目录字段**确定**的东西。

    故意不生成请求示例、不写 header 名、不写字段语义——那些需要源方文档。
    空着比编造强：agent 看到 example 为空，会去读 docs；看到假的 curl，
    会照着撞墙并把失败怪到平台头上。
    """
    lvl = str(item.get("access_level") or "").strip() or "open"
    proto = _guess_protocol(item)
    endpoint = str(item.get("data_url") or item.get("portal_api_url") or "").strip()
    docs = str(item.get("source_url") or item.get("catalog_url") or "").strip()
    pricing = dict(_LEVEL_PRICING.get(lvl, {"model": "unknown", "note": ""}))
    price = item.get("price")
    if isinstance(price, dict) and price:
        pricing["declared"] = price
    return {
        "version": PROFILE_VERSION,
        "protocol": proto,
        "endpoint": endpoint,
        "method": "GET" if proto in ("http-file", "portal") else "",
        "auth": {
            "kind": _LEVEL_AUTH.get(lvl, "unknown"),
            "label": _AUTH_LABEL.get(_LEVEL_AUTH.get(lvl, "unknown"), "未核实"),
            # 推导档案不知道源方的申请入口，就指向首页让 agent 自己找；
            # 编一个 /signup 出来只会 404。
            "how_to_get": ("去源方站点按注册/申请流程办，平台不代管账号"
                           if lvl != "open" else ""),
            "apply_at": docs if lvl != "open" else "",
        },
        "docs": docs,
        "limits": {},
        "pricing": pricing,
        "request": {},
        "examples": {},
        "fields_note": "",
        "notes": "",
        # 付费/授权源的受理入口就是源方站点本身：这不是猜，是只有那里能收钱。
        # 不填会让每条付费候选都带一条「缺 buy_url」的校验错误，反而把真正
        # 需要人看的问题淹掉。
        **({"buy_url": docs} if lvl in ("paid", "authorized") and docs else {}),
        "basis": "derived",
        "updated_at": _today(),
    }


def _mcp_config(mcp: dict) -> dict:
    return {"mcpServers": {str(mcp.get("name") or "source"): {
        "command": mcp.get("command", ""),
        "args": mcp.get("args") or [],
        "env": mcp.get("env") or {},
    }}}


def _mcp_first(prof: dict) -> dict:
    """源方给了 MCP，取数方式就由 MCP 定义——平台不再转录 HTTP 参数。

    0.23 曾把 BeaconMFG 判成 ``stat_api``，理由是「MCP 只是 HTTP 检索接口的
    便捷封装」，并在档案里写全了 ``POST /v1/anon/bootstrap``、``cid``、
    ``X-Beacon-Op``、body 参数和 offset 上限。0.26 审计认定这是越界：

    * agent 会照着这份 HTTP 说明书手搓，绕过源方 MCP——而 MCP 里的参数名是
      ``query``，档案写的是 ``q``。照抄的结果是 MCP 静默忽略关键词、按 city
      扫出 3000 条不相关企业（2026-11-09 实测，见 reports/platform_role_audit.md）；
    * 检索怎么拼、翻页怎么走、结果怎么组织（连锁折叠、district 补全）本来就是
      源方 MCP 的职责，平台抄一份只会抄旧、抄错，还替源方背了锅。

    所以：有 ``mcp`` 块就归一成 ``form=mcp / protocol=mcp``，endpoint 写启动
    方式，``request`` / ``params_note`` / ``response_path`` / ``pagination``
    全部移除；HTTP 直连只留一个指针（endpoint + docs），不写参数、不给 curl。
    """
    mcp = prof.get("mcp")
    if not isinstance(mcp, dict) or not (mcp.get("command") or mcp.get("url")):
        return prof
    out = dict(prof)
    launch = " ".join([str(mcp.get("command") or "")]
                      + [str(a) for a in (mcp.get("args") or [])]).strip()
    out["version"] = PROFILE_VERSION
    out["form"] = "mcp"
    out["protocol"] = "mcp"
    out["endpoint"] = str(mcp.get("url") or launch)
    out["method"] = ""
    http_ep = str(prof.get("endpoint") or "")
    # 只在原档案确实是一条 HTTP 入口时才留指针。已经归一过的档案（endpoint
    # 是启动命令）再跑一遍不能把 alternate_http 覆盖成 npx —— 归一化必须幂等。
    if http_ep.startswith(("http://", "https://")):
        out["alternate_http"] = {
            "endpoint": http_ep,
            "method": prof.get("method", ""),
            "docs": prof.get("docs", ""),
            "note": ("源方另提供 HTTP 接口；参数、鉴权、翻页与结果组织以源方的 "
                     "MCP 与文档为准，平台不转录、也不建议绕开 MCP 直连。"),
        }
    for k in ("request", "params_note", "response_path", "pagination",
              "query_param"):
        out.pop(k, None)
    auth = dict(out.get("auth") or {})
    if auth:
        auth["how_to_get"] = ("由源方 MCP 自行完成身份签发与传递；平台不代管凭证、"
                              "不代取")
        if auth.get("kind") in (None, "", "none", "unknown"):
            auth.pop("apply_at", None)
        out["auth"] = auth
    return out


def _scope_evidence(prof: dict) -> None:
    """给探测证据里的 ``http_status`` 标清"这个状态码是谁的"。

    档案里最伤人的不是缺项，是**自相矛盾**：同一个条目上写着
    ``gate=unreachable``（本机两条路都不通），证据里却记着
    ``http_status=200``。0.28 全量回归实测 2 条（Sachsen 统计局、卢森堡
    STATEC）。查下来两个数都是真的，只是描述的不是同一个地址：200 来自
    **入口页/门户页**的探测，取数地址本身 DNS 失败或证书自签。

    两句都对，但不标范围，agent 只会读出一句「200 却又不通」，然后要么
    跳过本来能取的源，要么以为档案在瞎写。所以补一个 ``http_status_scope``
    和一句说明——平台不猜哪个对，只把每个数的适用范围写清。
    """
    ev = prof.get("evidence")
    if not isinstance(ev, dict) or not ev:
        return
    st = ev.get("http_status")
    if not st or prof.get("gate") != "unreachable":
        return
    try:
        ok = 200 <= int(st) < 400
    except (TypeError, ValueError):
        return
    if not ok:
        return
    ev = dict(ev)                       # 不就地改 item，档案是共用的
    ev["http_status_scope"] = "host_page"
    ev["http_status_note"] = (
        f"{st} 是入口页/门户页的响应；档案认定的取数地址本机不可达"
        "（原因见 why），两者不是同一个 URL，别把它读成「取数地址返回 200」")
    prof["evidence"] = ev


def build(item: dict) -> dict:
    """取条目自带的档案（数据源上线时登记），没有就推导一份并标明 derived。"""
    raw = item.get("usage")
    if isinstance(raw, dict) and raw:
        prof = dict(raw)
        prof.setdefault("version", PROFILE_VERSION)
        prof.setdefault("basis", "source")
        prof.setdefault("updated_at", str(item.get("collected_at") or _today())[:10])
        _scope_evidence(prof)
        # 补齐推导能给的、而手写档案常漏的两项：不给就由 agent 自己从
        # access/endpoint 推，容易推错。
        if not prof.get("auth"):
            prof["auth"] = derive(item)["auth"]
        if not prof.get("pricing"):
            prof["pricing"] = derive(item)["pricing"]
        # 有 MCP 就以 MCP 为主路径：这是平台与源方的分工边界，不是偏好。
        return _mcp_first(prof)
    return derive(item)


def validate(prof: dict) -> list[str]:
    """档案的结构校验。只查自洽性，不查内容对不对（那要回源）。"""
    errs: list[str] = []
    proto = prof.get("protocol")
    if proto not in PROTOCOLS:
        errs.append(f"protocol 非法：{proto!r}")
    auth = prof.get("auth") or {}
    kind = auth.get("kind")
    if kind not in AUTH_KINDS:
        errs.append(f"auth.kind 非法：{kind!r}")
    # unknown = 「没核实」，不等于「需要凭证」：不可达/被挡的源本来就没法判定，
    # 拿这条去卡它只会让每条都挂一条假错。
    if kind not in (None, "none", "unknown") \
            and not (auth.get("how_to_get") or auth.get("apply_at")):
        errs.append("需要凭证却没写怎么拿（auth.how_to_get / apply_at）")
    pricing = prof.get("pricing") or {}
    derived = prof.get("basis") == "derived"
    if (not derived and pricing.get("model") in ("subscription", "authorization")
            and not prof.get("buy_url")):
        errs.append("付费/授权源缺 buy_url：agent 拿不到受理入口")
    if proto == "http-json":
        ep = str(prof.get("endpoint") or "")
        if ep and not ep.startswith(("http://", "https://")):
            errs.append("http-json 档案的 endpoint 必须是完整 URL")
        # 推导档案只从 URL 猜出协议，拿不到 method；只有手写档案才要求写全。
        if not derived and ep and str(prof.get("method") or "").upper() not in ("GET", "POST"):
            errs.append("http-json 档案必须写明 method（GET/POST）")
    if proto == "mcp":
        mcp = prof.get("mcp") or {}
        if not (mcp.get("command") or mcp.get("url")):
            errs.append("mcp 档案缺启动方式（command 或 url）")
        if prof.get("request") or prof.get("params_note") or prof.get("pagination"):
            errs.append("mcp 档案不应转录源方的 HTTP 参数"
                        "（request / params_note / pagination）：那是 MCP 的职责")
    elif (prof.get("mcp") or {}).get("command"):
        # 有 MCP 却把形态写成 HTTP：agent 会照 HTTP 说明书手搓，绕过源方 MCP。
        errs.append(f"源方提供 MCP，档案形态却是 {prof.get('form') or '未定'}："
                    "应归一为 form=mcp，让源方 MCP 负责取数方式")
    # 形态必填项：缺哪一项直接报出来（运营待办），不用含糊的「未登记」
    for miss in formsmod.missing_fields(prof):
        errs.append(f"{prof.get('form') or '未定形态'} 缺 {miss}："
                    f"{formsmod.FORMS.get(prof.get('form') or '', {}).get('label', '')}"
                    f" 形态下 agent 拿不到这一项就无法动手")
    req = prof.get("request") or {}
    for h in (req.get("headers") or {}):
        # header 里出现真实凭证形态就是事故：档案是要公开给所有调用方的。
        if re.search(r"(sk-[A-Za-z0-9]{12,}|Bearer\s+[A-Za-z0-9._-]{20,})", str(h)):
            errs.append(f"request.headers 疑似含真实凭证：{h}")
    for h, v in (req.get("headers") or {}).items():
        if re.search(r"(sk-[A-Za-z0-9]{12,})", str(v)):
            errs.append(f"header {h} 的值疑似含真实凭证")
    return errs


def example_for(prof: dict) -> dict:
    """把结构化 request 渲染成可直接执行的示例。

    只在 request 有实质内容时渲染；否则返回空——不编造。
    """
    if prof.get("examples"):
        return dict(prof["examples"])
    mcp = prof.get("mcp") or {}
    if prof.get("protocol") == "mcp" and (mcp.get("command") or mcp.get("url")):
        # MCP 形态只给启动配置与工具名；参数由 agent 自己读 tools/list。
        out = {"mcp_config": _mcp_config(mcp)}
        if mcp.get("tools"):
            out["tools"] = list(mcp["tools"])[:12]
        return out
    req = prof.get("request") or {}
    ep = str(prof.get("endpoint") or "")
    method = str(prof.get("method") or "GET").upper()
    headers = dict(req.get("headers") or {})
    body = req.get("body")
    query = req.get("query")
    if not ep or (not headers and not body and not query):
        return {}
    out: dict = {}
    url = ep
    if query:
        # 不能直接 ``ep + "?" + urlencode(query)``：源方 endpoint 里往往已经带
        # 了探测用的同名参数（data.europa.eu 的 ``?q=data&limit=1``），再拼一次
        # 会得到 ``q=data&...&q=关键词`` 两个 q——agent 照着发就取错数。
        # 同名参数以档案里的 request.query 为准，其余原样保留。
        from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit
        parts = urlsplit(ep)
        keep = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k not in query]

        def _val(v: str) -> str:
            # 占位符（<关键词>）保留原样，让示例自己说明「这里要换」；
            # 真实值做百分号编码，避免中文直接进 URL。
            v = str(v)
            return v if ("<" in v or ">" in v) else quote(v, safe="")

        pairs = keep + [(k, _val(v)) for k, v in query.items()]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                          "&".join(f"{k}={v}" for k, v in pairs), parts.fragment))
    lines = [f"curl -sS -X {method} '{url}'"]
    for k, v in headers.items():
        lines.append(f"  -H '{k}: {v}'")
    if body:
        import json as _json
        # request.headers 里往往已经写了 Content-Type，再补一行就是两条重复
        # 头（curl 都会发出去）。只在没写的时候补。
        if not any(k.lower() == "content-type" for k in headers):
            lines.append("  -H 'Content-Type: application/json'")
        lines.append(f"  -d '{_json.dumps(body, ensure_ascii=False)}'")
    out["curl"] = " \\\n".join(lines)
    return out


def brief(prof: dict) -> dict:
    """路牌上的档案摘要：够 agent 判断「我能不能连、要几步」。

    完整调用示例不进候选卡（一次 8 条候选 × 示例 = 白烧几千 token）；
    要细节的走 dp_usage / dp_card。这里只回答三个问题：什么协议、
    要不要凭证、一次能拿多少。
    """
    auth = prof.get("auth") or {}
    limits = dict(prof.get("limits") or {})
    spec = formsmod.FORMS.get(str(prof.get("form") or ""), {})
    out = {
        "form": prof.get("form", ""),
        "form_label": spec.get("label", ""),
        "gate": prof.get("gate", ""),
        "agent_action": spec.get("agent_action", ""),
        "protocol": prof.get("protocol", "unknown"),
        "endpoint": prof.get("endpoint", ""),
        "method": prof.get("method", ""),
        "auth_kind": auth.get("kind", "unknown"),
        "auth_label": auth.get("label", ""),
        "needs_credential": auth.get("kind") not in (None, "", "none", "unknown"),
        "how_to_get": str(auth.get("how_to_get") or "")[:200],
        "limits": limits,
        "pricing": prof.get("pricing") or {},
        "docs": prof.get("docs", ""),
        "basis": prof.get("basis", "probe"),
        "detail_tool": "dp_usage",
    }
    for k in ("landing_url", "direct_url", "blocker", "space_url", "card_url"):
        if prof.get(k):
            out[k] = prof[k]
    mcp = prof.get("mcp") or {}
    if mcp:
        out["mcp"] = {"command": mcp.get("command", ""),
                      "args": mcp.get("args") or [],
                      "tools": (mcp.get("tools") or [])[:8]}
    if prof.get("buy_url"):
        out["buy_url"] = prof["buy_url"]
    return out


def handoff_view(prof: dict) -> dict:
    """给 agent 的完整档案视图，补一句「谁负责什么」。

    这句话是这一版的核心：agent 读了不该以为平台会替它取数。
    """
    auth = dict(prof.get("auth") or {})
    spec = formsmod.FORMS.get(str(prof.get("form") or ""), {})
    view = {
        "form": prof.get("form", ""),
        "form_label": spec.get("label", ""),
        "gate": prof.get("gate", ""),
        "agent_action": spec.get("agent_action", ""),
        "required_fields": spec.get("required", []),
        "protocol": prof.get("protocol", "unknown"),
        "endpoint": prof.get("endpoint", ""),
        "method": prof.get("method", ""),
        "auth": auth,
        "request": prof.get("request") or {},
        "examples": example_for(prof),
        "limits": prof.get("limits") or {},
        "pricing": prof.get("pricing") or {},
        "docs": prof.get("docs", ""),
        "notes": prof.get("notes", ""),
        "fields_note": prof.get("fields_note", ""),
        "basis": prof.get("basis", "derived"),
        "updated_at": prof.get("updated_at", ""),
        "responsibility": {
            "platform": ["指路", "档案校验", "使用记账（供源方对账）"],
            "caller": ["取凭证", "发请求", "翻页", "清洗", "核精度与口径"],
            "platform_fetches_data": False,
        },
    }
    for k in ("landing_url", "how_to", "formats", "blocker", "pass_note",
              "space_url", "onboarding", "card_url", "why", "retry_note",
              "direct_url", "direct_note", "record_lookup", "query_param",
              "response_path", "pagination", "params_note", "format",
              "size_bytes", "sheet", "encoding", "entry_id", "alternate_http"):
        if prof.get(k):
            view[k] = prof[k]
    if prof.get("mcp"):
        view["mcp"] = prof["mcp"]
        # 源方有 MCP 时，取数方式（参数名、翻页、结果怎么折）归源方，不归平台。
        view["responsibility"]["source"] = [
            "MCP 工具的参数与检索策略", "翻页与结果组织", "字段口径与精度"]
    if prof.get("buy_url"):
        view["buy_url"] = prof["buy_url"]
    return view
