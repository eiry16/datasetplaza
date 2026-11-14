"""平台（hub）目录：把货架条目挂回所属门户，并给出 agent 能直接执行的指路。

### 为什么要有这一层

货架上有 22 个平台条目（data.europa.eu、World Bank、Kaggle、国家平台…），
它们本身不含内容，真正的内容在它们下面的子集里。这层关系一直在
``listings.json`` 里，只是从未被字段化——于是 agent 只能看到「有个叫
data.europa.eu 的条目」，看不到它名下还挂着 135 条已采集的数据集。

### 两条关系必须分开

::

    provider_of    发布门户（按 host）：data.europa.eu 直接发布 63 条
    aggregator_of  采集来源（按 id 前缀）：hub 一共聚合 135 条

混在一起数字就会打架：说「欧盟门户有 135 条」是错的，另外 72 条来自
各国的 odwb.be / ws.cso.ie。所以平台卡永远给三个数：**架上 N / 可枚举 M /
能出证据 K**，不含糊成一团。

### 一条不可让步的约束

平台**永不出内容级证据**。21 个平台 ``data_url`` 全空、``fields`` 全空，
它们能做的只有「指路」。所以 ``hub_route`` 在 ``proof_available=false``
时绝不允许返回 ``answered``——那是把路牌冒充成答案。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import enrich
from . import taxonomy as tx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LINKS = ROOT / "listings" / "hub_links.json"
CROSSWALK = ROOT / "bdsp" / "data" / "hub_crosswalk.json"
CROSSWALK_FALLBACK = ROOT / "docs" / "hub_crosswalk.json"

# ── 挂载关系 ──────────────────────────────────────────────────────────
# aggregator（采集来源，按 id 前缀）≠ provider（发布门户，按 host）。
AGG_PREFIX = {
    "hub_data_europa_eu_14539a46": "eu_",
    "hub_world_bank_open_data_6fe09252": "wb_",
    "hub_our_world_in_data_5dd06765": "owid_",
    # 0.39：香港 3,885 条此前全是孤儿（0.38 报告里「两个 hub 都缺」是推测，
    # 实测世行已被认领）。host 匹配会先给出 provider_of，前缀只作兜底。
    "hub_data_gov_hk_open_data": "hk_",
}
# 门户换了 API host，但条目 source_url 仍指旧域名时的补充
EXTRA_HOSTS = {
    "hub_world_bank_open_data_6fe09252": {"api.worldbank.org"},
    # 0.39：香港条目的 source_url 落在 /sc-data/dataset/<slug>，host 就是
    # data.gov.hk；门户条目自己的 catalog_url 也是它，derive 按 host 认领。
    "hub_data_gov_hk_open_data": {"data.gov.hk"},
    # Eurostat 统计 API 在 ec.europa.eu；它属欧盟数据门户体系，不是独立平台。
    # 0.14 实测：货架上原本只有新接入的 Eurostat 条目用这个 host，不会把
    # 无关站点收成欧盟门户的子集。
    "hub_data_europa_eu_14539a46": {"ec.europa.eu"},
}
# 商城产品页：名下子集是镜像副本，引用前必须回源，不能算 provider_of
MIRROR_HUBS = {"ndsms_mirror_27f1d32e", "hub_be1f3e1d"}
# 不按 host 认领子集的平台：``ndsms_mirror_27f1d32e`` 是一条**数据产品**记录，
# 因为无 data_url 被判成 P，然后靠「host 相同」把 18 条同样指向聚合站首页的
# 无关条目收成自己的子集——把「同一网站的条目」误当成「这个产品的子集」。
NO_HOST_ADOPT = {"ndsms_mirror_27f1d32e"}

# 可枚举总量：实测值，不是估算。没实测就留空，不猜。
MEASURED = {
    "hub_data_europa_eu_14539a46": (
        1999905, "data.europa.eu hub search 返回 count（含资源级记录）；单关键词 water 即 72,437"),
    "hub_world_bank_open_data_6fe09252": (29544, "api.worldbank.org/v2/indicator total"),
    # 0.36 实测 CKAN package_search(sc) 的 total；全量 3,885 条已收进货架。
    "hub_data_gov_hk_open_data": (3885, "data.gov.hk/sc-data CKAN package_search total"),
    # 下限而非全量：3,098 个月度子图里只抽了 18 个（67,728 条 URL，均 3.8k）。
    # 写「百万级」听起来更唬人，但没有清点过就不写。
    "hub_be1f3e1d": (67628, "sitemap 抽样：18/3,098 个子图共 67,728 条 URL，"
                            "全量未逐图清点，此数为下限"),
}
# 目录 API 实测状态（本机 curl，2026-11-08）
API_STATE = {
    "hub_data_europa_eu_14539a46": ("200", "hub search API 直接可查"),
    "hub_world_bank_open_data_6fe09252": ("200", "indicator 列表 API 可枚举"),
    "hub_data_gov_hk_open_data": ("200", "CKAN package_search(sc) 免登录可枚举；"
                                        "0.36 实测 rows=1 返回 200"),
    # 有 API 但用不了：/internal-api/* 要鉴权（401），公开的 /api/dataset/search
    # 无论给什么参数都返回 422「非法操作」——需要签名头，不反查。
    # 真正能用的是 sitemap，所以 api_status 记 401 而不是 200。
    "hub_be1f3e1d": ("401", "内部接口需鉴权；公开检索接口恒 422（需签名头）；"
                           "可枚举通道是 sitemap_index.xml，不是 API"),
    "hub_kaggle_datasets_d76c2f9d": ("200", "无鉴权即返回 JSON 列表（与常识相反，实测如此）"),
    "hub_zenodo_d32b3fc5": ("301", "重定向且拒绝默认 UA，需跟随 + 浏览器 UA"),
    "hub_figshare_4d6abb6d": ("403", "拒绝非浏览器 UA"),
    "hub_hugging_face_datasets_a2862d13": ("000", "本机网络不可达"),
    "hub_hugging_face_datasets_server_173a8d16": ("000", "本机网络不可达"),
    "hub_data_gov_a0d07415": ("404", "CKAN API 路径已变，/api/3/action 与 /api/action 均 404"),
    "hub_openalex_e3ef6aee": ("200", "works API 可用，但对象是论文不是数据集"),
    "hub_ceic_5f088431": ("未测", "付费墙 + 拒绝 curl UA"),
    "hub_wind_ec5be846": ("未测", "付费墙，仅登录界面"),
    "hub_csmar_296f7c15": ("未测", "付费墙，仅登录界面"),
}

# 可执行查询模板：{q} 关键词、{n} 条数、{code} 平台原生主题码。
# 只给实测确认过的路径；给不准的 URL 比不给更糟——agent 会把 404 读成「没这数据」。
QUERY_TEMPLATES = {
    "hub_data_europa_eu_14539a46": {
        "keyword": "https://data.europa.eu/api/hub/search/search?q={q}&limit={n}",
        "note": "无 facet 参数，取回后按 categories[].id == {code} 客户端过滤；"
                "26% 的记录没有 categories，靠主题检索必然漏",
    },
    "hub_world_bank_open_data_6fe09252": {
        "facet": "https://api.worldbank.org/v2/topic/{topic_id}/indicator?format=json&per_page={n}",
        "note": "topic 可直接过滤，四家里最好用的",
    },
    "hub_data_gov_hk_open_data": {
        "keyword": "https://data.gov.hk/sc-data/api/3/action/package_search?q={q}&rows={n}",
        "note": "CKAN 标准接口，/sc-data/ 给简体中文标题；groups 是源方主题分组，"
                "资源直链在 result.results[].resources[]",
    },
    "hub_kaggle_datasets_d76c2f9d": {
        "keyword": "https://www.kaggle.com/api/v1/datasets/list?search={q}&pageSize={n}",
        "note": "tag 自带 datasetCount，可先看分面计数再决定钻哪支",
    },
}

# 平台卡的固定提醒：这一句永远跟着平台出现，防止 agent 把路牌当答案
HUB_PROMISE = "平台只指路，不出内容级证据；取值必须连源方。"


# ── 派生挂载关系（纯函数，可重算）──────────────────────────────────────
def derive(items: list[dict]) -> dict:
    """从目录条目推导 provider_of / aggregator_of / mirror_of 三条关系。

    一次遍历出结果，不依赖任何缓存文件；``listings/hub_links.json`` 只是
    这份结果的落盘快照，两者不一致时以本函数为准（采集脚本覆盖 listings
    后重跑 ``scripts/build_hub_links.py`` 即可）。
    """
    by_id = {i.get("dataset_id"): i for i in items}
    kinds = {i.get("dataset_id"): tx.kind_of(i) for i in items}
    hubs: dict[str, dict] = {}
    for i in items:
        ds_id = i.get("dataset_id") or ""
        if kinds.get(ds_id) == "D":
            continue
        url = i.get("catalog_url") or i.get("source_url") or ""
        host = tx.host_of(url)
        hosts = {host} if host else set()
        hosts |= EXTRA_HOSTS.get(ds_id, set())
        hubs[ds_id] = {
            "hub_id": ds_id,
            "name": i.get("name") or "",
            "kind": kinds.get(ds_id),
            "publisher": i.get("publisher") or "",
            "catalog_url": url,
            "hosts": sorted(h for h in hosts if h),
            "agg_prefix": AGG_PREFIX.get(ds_id, ""),
            "children": [],
        }

    relations: dict[str, dict] = {}
    for ds_id, item in by_id.items():
        if kinds.get(ds_id) != "D":
            continue
        child_host = tx.host_of(item.get("source_url") or item.get("catalog_url") or "")
        for hub_id, hub in hubs.items():
            rel = ""
            adopt = hub_id not in NO_HOST_ADOPT and child_host and child_host in hub["hosts"]
            if hub_id in MIRROR_HUBS and adopt:
                rel = "mirror_of"
            elif adopt:
                rel = "provider_of"
            elif hub["agg_prefix"] and ds_id.startswith(hub["agg_prefix"]):
                rel = "aggregator_of"
            if not rel:
                continue
            # 一个条目同时满足两条关系时，provider_of 更能说明"谁发布的"
            prev = relations.get(ds_id)
            if prev and prev["relation"] == "provider_of" and rel == "aggregator_of":
                continue
            relations[ds_id] = {"parent_hub": hub_id, "relation": rel}
            hub["children"].append({"dataset_id": ds_id, "relation": rel})

    for hub_id, hub in hubs.items():
        kids = hub["children"]
        hub["shelf_children"] = len(kids)
        hub["provider_children"] = sum(1 for k in kids if k["relation"] == "provider_of")
        hub["agg_children"] = sum(1 for k in kids if k["relation"] == "aggregator_of")
        hub["mirror_children"] = sum(1 for k in kids if k["relation"] == "mirror_of")
        enum = MEASURED.get(hub_id)
        hub["enumerable_total"] = enum[0] if enum else None
        hub["enum_note"] = enum[1] if enum else ""
        api = API_STATE.get(hub_id, ("未测", ""))
        hub["api_status"], hub["api_note"] = api
        hub["state"] = hub_state(hub)
        del hub["children"]  # 子集明细走 relations，避免双份数据

    return {
        "schema": "dataset-plaza-hub-links/1",
        "algorithm": "provider_of=host(catalog_url) 匹配 source_url 的 host；"
                     "aggregator_of=dataset_id 前缀；mirror_of=商城镜像页",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            # 两个口径都记：`items` 是去重后的条目数（by_id），`input` 是喂进来
            # 的长度。`load_links()` 拿货架列表比对时必须跟 `input` 比——3.5 万
            # 条里总有几个没有 dataset_id 的，去重后少 76 条，比 `items` 永远
            # 不等，于是每次调用都白重算一遍（实测 3.5s）。
            "items": len(by_id),
            "input": len(items),
            "datasets": sum(1 for v in kinds.values() if v == "D"),
            "hubs": len(hubs),
            "linked": len(relations),
            "orphan_datasets": sum(1 for v in kinds.values() if v == "D") - len(relations),
        },
        "relations": relations,
        "hubs": hubs,
    }


def hub_state(hub: dict) -> str:
    """四态 + 一种细分：决定平台卡能承诺到什么程度。

    has_children    架上有子集 → 可穿透（但只到已采集的那部分）
    mirror_only     架上的子集**全是镜像副本** → 可穿透，但必须回源核验
    enumerable      目录 API 实测 200 → 可按需拉取，别预抓
    blocked         API 存在但被 UA/鉴权/路径挡住 → 先补探测规则
    unenumerable    付费墙，只有登录界面 → 只能人工指路

    ``mirror_only`` 是 0.7 加的一档：聚合站的子集不是它发布的，把它和
    「平台自己发布的子集」并成同一个 has_children，等于用镜像给平台背书。
    """
    if hub.get("shelf_children"):
        if hub.get("mirror_children") == hub["shelf_children"]:
            return "mirror_only"
        return "has_children"
    status = hub.get("api_status")
    if status == "200":
        return "enumerable"
    if status in ("301", "403", "404", "000", "401"):
        return "blocked"
    return "unenumerable"


STATE_LABELS = {
    "has_children": "可穿透（架上有已采集子集）",
    "mirror_only": "可穿透（架上仅有镜像副本，须回源核验）",
    "enumerable": "可枚举未上架（按需拉取，别预抓）",
    "blocked": "可枚举但被挡（先补探测规则）",
    "unenumerable": "不可枚举（付费墙/仅登录）",
}


def load_links(items: list[dict] | None = None, path: Path | None = None) -> dict:
    """读落盘快照；缺失或过期（条目数对不上）时实时重算。

    宁可多算一次，也不返回与实际货架不符的挂载关系——那是会写进平台卡的
    数字，错一个就整页失真。
    """
    if items is None:
        from . import listings as listingmod
        items = listingmod.load()
    p = Path(path or DEFAULT_LINKS)
    try:
        snap = json.loads(p.read_text(encoding="utf-8"))
        counts = snap.get("counts", {})
        if counts.get("input", counts.get("items")) == len(items):
            return snap
    except (OSError, json.JSONDecodeError):
        pass
    return derive(items)


# ── crosswalk（平台原生分类 ↔ 我们的门类）──────────────────────────────
def load_crosswalk() -> dict:
    for p in (CROSSWALK, CROSSWALK_FALLBACK):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


CONF_RANK = {"exact": 0, "broad": 1, "weak": 2}
# 实测命中少于这个数的映射等于没有：DCAT 的 INTR（国际事务）全库只 2 条，
# 把它当「贸易数据的入口」指过去，agent 只会空手而归。
MIN_EFFECTIVE_N = 5

# ── DCAT-AP 的 13 个「主题」里有 2 个根本不是主题 ─────────────────────
# 这不是推测，是 135 条欧盟条目实测出来的：
#   GOVE  26 条里 21 条内容与我们门类判定不一致（81%）。看名称全是
#         「Namur - Statistics」「IPA04 - Structural Business Statistics」
#         「Ireland's Tax Statistics」——它描述的是**发布方是公共机构**。
#   REGI  7 条内容五花八门：老年健康路径、泥沙输运、高程模型、市政决议、
#         伦敦技能供给。它描述的是**数据按地区粒度发布**，不是「内容是城市」。
# 这与之前发现的 `domain` 采集桶是同一个错误：把来源/粒度当成内容。
# 处理方式：保留在平台页里展示（那是平台说的），但不映射到我们的门类，
# 也不计入一致率——否则主题轴会被两个 facet 污染，一致率被拉到 56%。
NATIVE_FACETS = {
    "GOVE": {"facet": "publisher_type", "zh": "发布方类型（公共机构）"},
    "REGI": {"facet": "spatial_granularity", "zh": "地域粒度（按地区统计）"},
}


def native_themes_for(crosswalk: dict, l1: str) -> list[dict]:
    """某门类在平台原生词表里的位置，带 exact/broad/weak 置信与可执行查法。

    exact 才能给「确定能筛到」的承诺；broad 必须带告警——DCAT 的 SOCI 一条
    就横跨民生/治理/教育 139 条，当成主题筛会漏一大半。**排序按置信度**，
    并剔掉实测命中过少的映射：一个只有 2 条命中的主题不是入口，是陷阱。
    facet 型主题（见 ``NATIVE_FACETS``）直接跳过——它们不是内容主题。
    """
    out: list[dict] = []
    for code, meta in (crosswalk.get("dcat_ap") or {}).items():
        if code in NATIVE_FACETS:
            continue
        conf = (meta.get("conf") or {}).get(l1)
        if not conf:
            continue
        n = meta.get("measured_n")
        if conf != "exact" and isinstance(n, int) and n < MIN_EFFECTIVE_N:
            continue
        out.append({
            "platform": "data.europa.eu",
            "platform_id": "hub_data_europa_eu_14539a46",
            "scheme": "DCAT-AP data-theme",
            "code": code,
            "conf": conf,
            "measured_n": n,
            "note": meta.get("note", ""),
        })
    for topic_id, meta in (crosswalk.get("world_bank") or {}).items():
        ours = meta.get("ours") or []
        if l1 not in ours:
            continue
        conf = (meta.get("conf") or {}).get(l1) or "broad"
        out.append({
            "platform": "World Bank",
            "platform_id": "hub_world_bank_open_data_6fe09252",
            "scheme": "WB Topic",
            "code": topic_id,
            "topic": meta.get("topic"),
            "conf": conf,
            "measured_n": meta.get("indicators"),
            "note": "",
        })
    out.sort(key=lambda n: (CONF_RANK.get(n["conf"], 9),
                            -(n["measured_n"] or 0)))
    return out


# 用户问法 ≠ 条目名称。中文提问多是口语词（生猪、胃镜、快递），条目名是
# 英文或公文名，两套词面几乎不重叠——只用条目规则去匹配提问，会把「生猪
# 价格」判成无主题。所以问法词表单独一份，顺序即优先级，具体词先于泛词。
_QUERY_HINTS: list[tuple[str, str]] = [
    ("T07", r"生猪|猪肉|猪价|牛羊|鸡蛋|饲料|粮食|小麦|玉米|大豆|蔬菜|水果|水产|渔民|农户|耕地|农作物"),
    ("T06", r"癌症|肿瘤|胃癌|肺癌|肝癌|糖尿病|高血压|心血管|门诊|住院|医保|疫苗|传染病|死亡率|预期寿命|精神卫生"),
    ("T05", r"高考|中考|招生|升学|学位|大学|中小学|教师|课程|培训|技能|识字|文盲|毕业生|入学率"),
    ("T11", r"铁路|高铁|公路|地铁|公交|航班|机场|港口|快递|冷链|物流|货运|通勤|拥堵"),
    ("T09", r"pm2\.?5|雾霾|空气质量|碳排放|碳中和|污水|垃圾分类|生态|物种|濒危"),
    ("T10", r"台风|暴雨|洪水|干旱|地震|气象|气温|降水|灾情|预警"),
    ("T08", r"光伏|风电|核电|水电|用电量|电价|装机|煤电|储能|能效"),
    ("T04", r"人口|老龄化|出生率|就业|失业|工资|社保|养老金|低保|房价|租金|户籍|常住"),
    ("T03", r"利率|汇率|股价|债券|税收|增值税|企业名录|工商|注册资本|财报|信贷"),
    ("T02", r"进出口|出口|进口|贸易|关税|跨境电商|商圈|零售|批发"),
    ("T01", r"gdp|国内生产总值|地区生产总值|生产总值|cpi|物价|通胀|经济增速|人均收入|生产率"),
    ("T13", r"供水|自来水|燃气|供热|供暖|垃圾清运|市政|绿化|公园"),
    ("T12", r"钢铁|水泥|化工|矿山|煤炭|机械|装备|制造|工业|安全生产"),
    ("T14", r"大模型|语料|标注|对话数据|问答对|图像识别|目标检测|知识图谱|人工智能|\bai\b"),
    ("T15", r"法规|政策|条例|监管|处罚|执法|行政许可|政务"),
]
_QUERY_HINTS = [(c, re.compile(p, re.I)) for c, p in _QUERY_HINTS]


def topic_of(query: str) -> dict | None:
    """把用户问题映射到一个门类。纯规则，命中词一起返回，方便复核。

    先跑问法词表，再跑条目名规则——「生猪价格统计」若先撞上「统计」会被判
    成宏观经济，而它显然是农产品价格。具体词必须优先于泛词。

    只做一次，不猜二义：映射不出就返回 None，把门类清单交给 agent 自己选。
    """
    q = (query or "").strip()
    if not q:
        return None
    for code, pat in _QUERY_HINTS:
        m = pat.search(q)
        if m:
            return {"l1": code, "l1_name": tx.L1_NAME[code],
                    "matched": m.group(0), "confidence": "high", "via": "query_hint"}
    for code, pat in tx.L1_RULES:
        m = pat.search(q)
        if m:
            return {"l1": code, "l1_name": tx.L1_NAME[code],
                    "matched": m.group(0), "confidence": "high", "via": "name_rule"}
    low = q.lower()
    for code, pat in tx.WEAK.items():
        m = pat.search(low)
        if m:
            return {"l1": code, "l1_name": tx.L1_NAME[code],
                    "matched": m.group(0), "confidence": "medium", "via": "weak_rule"}
    return None


def topic_menu() -> list[dict]:
    """全部门类（15 个，约 200 字）。无法映射时的兜底：交还给 agent 的语义能力。"""
    return [{"l1": c, "name": tx.L1_NAME[c], "scope": tx.L1_DEF[c]}
            for c in tx.L1_ORDER if c != "T00"]


# ── 平台清单与详情 ────────────────────────────────────────────────────
def hub_list(items: list[dict] | None = None) -> dict:
    snap = load_links(items)
    hubs = sorted(snap["hubs"].values(),
                  key=lambda h: (-(h["shelf_children"] or 0), h["hub_id"]))
    rows = [{
        "hub_id": h["hub_id"],
        "name": h["name"],
        "entity_kind": h["kind"],
        "state": h["state"],
        "state_label": STATE_LABELS[h["state"]],
        "shelf": h["shelf_children"],
        "provider": h["provider_children"],
        "aggregated": h["agg_children"],
        # 镜像子集单列：state=mirror_only 时，架上这些全是副本，不能当平台自有内容
        "mirrored": h["mirror_children"],
        "enumerable": h["enumerable_total"],
        "api": h["api_status"],
    } for h in hubs]
    return {
        "ok": True,
        "total": len(rows),
        "with_children": sum(1 for r in rows if r["shelf"]),
        "items": rows,
        "promise": HUB_PROMISE,
        "NEXT": "dp_hub(hub_id) 看单个门户的分类机制与架上子集；要按主题找数据用 dp_hub_route。",
    }


def _is_facet(hub_id: str, code: str) -> bool:
    """该原生码在**这个平台**上是不是 facet 型（描述来源/粒度，不是内容）。

    只有 DCAT-AP 的 GOVE / REGI 有此问题，世行 Topic 没有——所以按平台判定，
    不能全局拉黑：万一将来别的平台用 GOVE 表示真·政务数据。
    """
    return (hub_id == "hub_data_europa_eu_14539a46"
            and code in NATIVE_FACETS)


def _native_crosswalk(crosswalk: dict, hub_id: str, code: str) -> tuple[list[str], str]:
    """平台原生主题码 → 我们的门类，带 exact/broad 置信（没有映射就返回空）。"""
    tables = []
    if hub_id == "hub_world_bank_open_data_6fe09252":
        tables = [crosswalk.get("world_bank") or {}]
    elif hub_id == "hub_data_europa_eu_14539a46":
        tables = [crosswalk.get("dcat_ap") or {}]
    else:
        tables = [crosswalk.get("dcat_ap") or {}, crosswalk.get("world_bank") or {}]
    for table in tables:
        meta = table.get(code) or {}
        ours = meta.get("ours") or []
        if ours:
            conf = meta.get("conf") or {}
            best = sorted(ours, key=lambda x: 0 if conf.get(x) == "exact" else 1)[0]
            return ours, conf.get(best, "broad")
    return [], ""


def hub_detail(hub_id: str, items: list[dict] | None = None,
               crosswalk: dict | None = None, limit: int = 20) -> dict:
    """平台详情页：先讲它自己怎么分类，再列我们架上采到的子集。

    分组用**平台自己的主题词表**（EU 的 DCAT-AP data-theme、世行的 Topic），
    这是 ``scripts/enrich_hub_metadata.py`` 从源方 API 抓来的声明，不是我们
    的判断。子集缺主题时单独归到「未标主题」，绝不按我们的门类去补——那是
    把未验证的标签冒充分类结果。

    覆盖率低于一半时不假装能原生分组（``native_grouping=false``），改按我们
    的门类分组并说明原因：编一个只有一半成员的分组树，agent 会以为另一半不存在。
    """
    snap = load_links(items)
    hub = (snap.get("hubs") or {}).get(hub_id)
    if not hub:
        return {"ok": False, "reason": "unknown_hub", "hub_id": hub_id,
                "NEXT": "dp_hub 不带参数可列出全部平台。"}
    from . import listings as listingmod
    items = items if items is not None else listingmod.load()
    by_id = {i.get("dataset_id"): i for i in items}

    cw = crosswalk if crosswalk is not None else load_crosswalk()
    kids = [{"dataset_id": k, **v} for k, v in snap["relations"].items()
            if v["parent_hub"] == hub_id]
    groups: dict[str, list[dict]] = {}
    native_groups: dict[str, dict] = {}
    code_stat: dict[str, dict] = {}
    indexed = labeled = compared = agree = 0
    for k in kids:
        item = by_id.get(k["dataset_id"]) or {}
        cls = tx.classify(item)
        proof = bool(item.get("linked_dataset_id"))
        indexed += 1 if proof else 0
        row = {
            "dataset_id": k["dataset_id"],
            "name": item.get("name") or "",
            "relation": k["relation"],
            "l1": cls["l1"],
            "l1_name": cls["l1_name"],
            "sub_name": cls["sub_name"],
            "form": cls["form"],
            "proof_available": proof,
        }
        groups.setdefault(cls["l1_name"], []).append(row)
        labels = enrich.native_labels(item)
        if labels:
            labeled += 1
        else:
            labels = [""]
        item_mapped: set[str] = set()
        for code in labels:
            slot = native_groups.setdefault(code, {"code": code, "label": "", "items": []})
            if not slot["label"]:
                slot["label"] = next((c.get("en") or "" for c in enrich.categories_of(item)
                                      if (c.get("id") or "") == code), "")
            slot["items"].append(row)
            if not code or _is_facet(hub_id, code):
                continue  # facet 型主题不映射门类、不计入一致率
            ours = set(_native_crosswalk(cw, hub_id, code)[0])
            item_mapped |= ours
            st = code_stat.setdefault(code, {"agree": 0, "total": 0})
            if ours:
                st["total"] += 1
                st["agree"] += 1 if cls["l1"] in ours else 0
        # 一致率只统计两侧都有值的条目——缺主题的没有可比性，算进去会压低它
        if item_mapped:
            compared += 1
            agree += 1 if cls["l1"] in item_mapped else 0
    diverge = compared - agree

    native = (cw.get("platforms") or [])
    mine = next((p for p in native if p.get("platform_id") == hub_id), {})
    ratio = round(labeled / len(kids), 3) if kids else 0.0
    use_native = bool(kids) and ratio >= 0.5

    def _ng(code: str, slot: dict, with_items: bool = True) -> dict:
        ours, conf = _native_crosswalk(cw, hub_id, code) if code else ([], "")
        out = {
            "code": code or "unlabelled",
            "label": slot["label"] or ("未标主题" if not code else code),
            "count": len(slot["items"]),
        }
        if with_items:
            out["items"] = [{k: r[k] for k in ("dataset_id", "name", "relation",
                                               "proof_available")}
                            for r in slot["items"][:min(limit, 3)]]
        if code and _is_facet(hub_id, code):
            meta = NATIVE_FACETS[code]
            out["kind"] = "facet"
            out["facet"] = meta["facet"]
            out["facet_label"] = meta["zh"]
            out["ours"] = []
            out["crosswalk_conf"] = ""
            out["note"] = "该主题描述的是来源/粒度，不是内容；只能用来定位，不能当分类结果"
        elif code:
            out["kind"] = "topic"
            out["ours"] = ours
            out["crosswalk_conf"] = conf
            st = code_stat.get(code) or {}
            if st.get("total"):
                out["agreement"] = round(st["agree"] / st["total"], 2)
        return out

    native_rows = sorted(native_groups.values(),
                         key=lambda s: (s["code"] == "", -len(s["items"])))
    # 原生分组为主时，我们的门类只留计数——两套明细都回会让平台页膨胀 4 倍，
    # 而 agent 此时只看原生这套；需要我们的门类可以改看 dp_index。
    own_groups = [
        {"l1_name": name, "count": len(v), **({} if use_native else {"items": v[:limit]})}
        for name, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]
    return {
        "ok": True,
        "hub": {k: v for k, v in hub.items() if k != "children"},
        "state_label": STATE_LABELS[hub["state"]],
        "three_numbers": {
            "shelf": hub["shelf_children"],
            "enumerable": hub["enumerable_total"],
            "proof": indexed,
            "note": "架上=已采集并入库的条目；可枚举=源方目录实测总量；"
                    "可出证据=已建字段级索引（只有这时才谈得上回答）",
        },
        "native_taxonomy": mine.get("taxonomy") or {},
        "native_measured": mine.get("measured") or {},
        "native_grouping": use_native,
        "native_coverage": {"labelled": labeled, "total": len(kids), "ratio": ratio},
        "native_vs_ours": {
            "compared": compared, "agree": agree, "diverge": diverge,
            "agreement_rate": round(agree / compared, 3) if compared else 0.0,
            "note": "原生主题与我们门类的一致率。不一致的多是 GOVE 这类"
                    "「发布方是政府」的主题——它描述发布方身份，不是数据内容；"
                    "所以原生主题只能用来定位，不能当分类结果引用。",
        } if compared else {},
        "native_grouping_note": (
            f"按平台自己的主题分组（{labeled}/{len(kids)} 条有原生主题）。"
            "同一子集可属多个主题，故各组合计大于总数。"
            if use_native else
            f"原生主题只覆盖 {labeled}/{len(kids)} 条，不足一半，故改按我们的门类分组；"
            "缺主题的条目不会按我们的门类补猜。"),
        # 只给前 5 个组列样例条目：平台页是「浏览入口」，不是清单导出。
        # 12 个组都给明细会让一次调用膨胀到 1 万字以上，而 agent 真正要的是
        # 「哪个主题有多少」；要看某组明细应改调 dp_index(门类, 子类)。
        "native_groups": ([_ng(s["code"], s, with_items=(idx < 5))
                           for idx, s in enumerate(native_rows)] if use_native else []),
        "native_groups_note": ("items 只列前 5 个组的样例；其余组看 count，"
                               "明细走 dp_index。" if use_native else ""),
        "groups": own_groups,
        "grouping_basis": "native" if use_native else "ours",
        "promise": HUB_PROMISE,
        "NEXT": "子集里有可执行 URL 的直接连源方取数（先 dp_pitfalls 看前面的人踩过什么）；"
                "proof_available 在本平台恒为 false——平台不持有内容，不出证据包。",
    }


# ── 三态指路 ──────────────────────────────────────────────────────────
def hub_route(query: str, items: list[dict] | None = None,
              crosswalk: dict | None = None, route_fn=None) -> dict:
    """问题 → 主题门类 → 平台原生位置 / 可执行查询 → 三态结论。

    =========  ==============================================
    answered   索引里**真的命中了内容**，去 dp_proof 拿证据
               （仅当平台持有快照通道时可出现；0.27 起该通道默认关闭，
               因此实际只会返回 pointed / none）
    pointed    没有证据，但指得到路（平台 facet 或架上条目）
    none       两处都没有；给门类清单，不编造
    =========  ==============================================

    ``proof_available`` 的判据是「这个问题在索引里有内容命中」，**不是**
    「这个门类里存在已索引数据集」。后者会造成危险的假阳性：架上有 1 条
    儿童死亡率的索引数据，胃癌影像就会被判成 answered——命中的是同一个
    门类，内容却毫无关系。所以必须传 ``route_fn`` 真跑一次检索，没跑就
    只能给 pointed。

    硬规则（测试覆盖）：``proof_available=false`` 时 ``state`` 不得为
    ``answered``；任一计划项走关键词必须带 ``warn``；``conf=broad`` 必须带
    ``warn``。给个假 facet 比不给更糟——agent 会把空结果读成"世上没这数据"。
    """
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "reason": "empty_query", "NEXT": "给出至少一个关键词。"}
    from . import listings as listingmod
    items = items if items is not None else listingmod.load()
    cw = crosswalk if crosswalk is not None else load_crosswalk()

    topic = topic_of(q)
    if not topic:
        return {
            "ok": True, "query": q, "state": "none", "channel": "",
            "proof_available": False, "reason": "no_topic_match",
            "shelf": {"datasets": 0, "indexed": 0, "dataset_ids": []},
            "topics": topic_menu(),
            "warn": "问题没有映射到任何门类。请从 topics 里选定一个，用 "
                    "dp_index(domain=门类码) 看子类；不要拿原句去盲目检索。",
            "NEXT": "dp_index() 列出全部门类 → dp_index(domain) 看子类族卡。",
        }

    l1 = topic["l1"]
    shelf = [i for i in items
             if tx.kind_of(i) == "D" and tx.classify(i)["l1"] == l1]
    indexed = [i for i in shelf if i.get("linked_dataset_id")]
    natives = native_themes_for(cw, l1)
    warnings: list[str] = []

    # ── 证据判定：只认真的内容命中 ──
    evidence: list[dict] = []
    if route_fn and indexed:
        # route 返回的是**索引库**里的 dataset_id，即条目的 linked_dataset_id，
        # 不是条目的 dataset_id——两者只在自建同源时相同。
        allowed = {i["linked_dataset_id"] for i in indexed}
        try:
            res = route_fn(q) or {}
            evidence = [c for c in (res.get("candidates") or [])
                        if c.get("has_content_hit") and c.get("dataset_id") in allowed]
        except Exception:  # noqa: BLE001 - 检索失败绝不允许升格为 answered
            evidence = []
    proof_available = bool(evidence)
    if indexed and not route_fn:
        warnings.append(
            f"该门类挂着 {len(indexed)} 条历史快照，但平台已停止代取与内容检索，"
            "不据此声称能回答——按 pointed 里的源方入口自取。")

    # ── 平台侧计划：facet 能筛就 facet，否则关键词并告警 ──
    # 没有可执行 URL 的平台给门户入口而不是空串：空串会被 agent 读成"没路"，
    # 其实路是有的，只是要在界面上手点。
    portal = {i.get("dataset_id"): (i.get("catalog_url") or i.get("source_url") or "")
              for i in items}
    plan: list[dict] = []
    for n in natives[:4]:
        tpl = QUERY_TEMPLATES.get(n["platform_id"], {})
        if n["conf"] == "exact" and tpl.get("facet"):
            url = tpl["facet"].format(topic_id=n["code"], n=50)
            ch = "facet"
        elif tpl.get("keyword"):
            url = tpl["keyword"].format(q=q, n=50)
            ch = "keyword"
        else:
            url, ch = portal.get(n["platform_id"], ""), "manual"
        plan.append({**n, "channel": ch, "executable": url,
                     **({"channel_note": "该平台无公开检索 API，需在门户界面人工检索"}
                        if ch == "manual" else {}),
                     **({"coverage_hint": f"该主题下 {n['measured_n']} 个指标，"
                                          f"均为国家/地区·年度汇总口径，"
                                          f"不含记录级/影像级数据"}
                        if n["platform"] == "World Bank" and n.get("measured_n") else {})})
    chans = {p["channel"] for p in plan if p["channel"] != "manual"}
    channel = next(iter(chans)) if len(chans) == 1 else ("mixed" if chans else "")
    # 平台通道的告警单独收集：answered 时它们是"备选路"的瑕疵，不能和
    # 「索引里确有命中」这个结论混在一起说，否则 agent 会以为证据也不可靠。
    plan_warnings: list[str] = []
    kw_items = [p for p in plan if p["channel"] == "keyword"]
    if kw_items:
        # 两种情况必须分开说：有精确主题但平台不支持 facet 过滤（欧盟 hub），
        # 与根本没有精确主题（T10/T12/T14）。混成一句会让 agent 以为
        # 「TRAN 不是交通主题」，反而去怀疑映射表。
        exact_kw = [p for p in kw_items if p["conf"] == "exact"]
        loose_kw = [p for p in kw_items if p["conf"] != "exact"]
        if exact_kw:
            plan_warnings.append(
                "主题有精确对应（"
                + "、".join(f"{p['platform']}:{p['code']}" for p in exact_kw[:2])
                + "），但该平台检索 API 不支持按主题过滤，需取回后按主题字段客户端筛；"
                "结果完整度不保证。")
        if loose_kw:
            plan_warnings.append(
                "平台原生词表里没有精确主题（"
                + "、".join(f"{p['platform']}:{p['code']}" for p in loose_kw[:2])
                + "），只能关键词检索；空结果不代表世上没有这份数据。")
    broad = [n for n in natives if n["conf"] == "broad"]
    if broad:
        plan_warnings.append(
            "泛化映射：" + "、".join(f"{n['platform']}:{n['code']}" for n in broad[:3])
            + " 横跨多个门类，必须叠加关键词再筛。")
    if shelf and not indexed:
        plan_warnings.append(
            f"架上有 {len(shelf)} 条该门类条目，但都未建索引：只能指路，不能出证据。")

    shelf_block = {
        "datasets": len(shelf),
        "indexed": len(indexed),
        "dataset_ids": [i["dataset_id"] for i in (indexed or shelf)][:5],
    }

    if proof_available:
        return {
            "ok": True, "query": q, "state": "answered", "channel": "shelf",
            "topic": topic, "proof_available": True, "shelf": shelf_block,
            "evidence": [{"dataset_id": c["dataset_id"], "name": c.get("name", ""),
                          "hit_count": c.get("hit_count"), "score": c.get("score")}
                         for c in evidence[:5]],
            "plan": plan,
            "warn": " ".join(warnings),
            "plan_note": " ".join(plan_warnings) if plan_warnings else "",
            "NEXT": "dp_proof(query, dataset_id) 拿免费证据（命中数/字段口径/快照哈希）；"
                    "零命中不付费。",
        }

    warnings.extend(plan_warnings)
    state = "pointed" if (plan or shelf) else "none"
    if state == "none":
        warnings.append(f"「{topic['l1_name']}」既无已索引数据、也无平台原生主题对应，"
                        "不要编造答案。")
    return {
        "ok": True, "query": q, "state": state, "channel": channel,
        "topic": topic, "proof_available": False, "shelf": shelf_block,
        "plan": plan,
        "warn": " ".join(warnings),
        "NEXT": ("按 plan[].executable 直接查源方；引用时须说明来源是平台、未经本平台复核。"
                 if plan else
                 "dp_index(domain) 看该类条目，或换个说法重试。")
                + " 取数前 dp_pitfalls(dataset_id) 读坑。",
        **({"topics": topic_menu()} if state == "none" else {}),
    }
