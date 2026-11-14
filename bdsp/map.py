"""分级地图：需求归类的单一出口（0.18 起主人定调的架构）。

分工写死在这里：

- **agent 理解需求**：agent 拿 ``describe()``（MCP ``dp_map``）看分级地图
  ——15 个主题大类 → 每类的子类——把用户需求**归到大类**，然后把结构化
  坐标（topics / subtopics / sectors / geo / year…）传给路由。理解用户
  说什么、属于哪个大类，是 agent（语言模型）的事，任何语言都一样。
- **平台给结构与验证**：地图只负责校验坐标合法（码存在、子类父类一致），
  然后在大类里用**名片**（capability 卡）排出能接住的数据集。
- **词面只是无 agent 的兜底**：控制台直连时 ``resolve_need()`` 把自然语言
  引导到地图节点。词面绑定在**地图节点**上（每节点一族多语言词面，有限
  集合，上限就是节点数），不再散落在业务代码里无限增长。日文、法文或任何
  语言，主路径都是 agent 归类——不补词表。
"""
from __future__ import annotations

import re

from . import sectors as secmod
from . import taxonomy as tx

MAP_VERSION = "map-3"    # 0.44：geo_axis 公开层级枚举与可达性，need 收 geo_level

# ── 污水语境（T13/S1301 节点词面，供侧 thematic 与度量清洗共用） ─────────
WASTEWATER_CONTEXT = re.compile(
    r"污水|废水|污泥|水污染|污水管网|污水处理厂|"
    r"\bwaste ?water\b|\bwastewater\b|\bsewage\b|\bsanitation\b|\buwwtd\b|"
    r"\bassainissement\b|\beaux us[eé]es\b|\b[ée]puration\b|"
    r"\bwater pollution\b|\beffluent\b", re.I)

# ── 需求侧引导词面：绑定地图节点，兜底专用 ─────────────────────────────
# key = 子类码（topic 由 SUB 表的父类自动带出）；value = 该节点的需求词面。
# 加新领域 = 在这里给对应节点补词面（或什么都不做，让 agent 走 dp_map）。
# 这是有限集合：上限是子类节点数，不是「世界上有多少词」。
NEED_HINTS: dict[str, str] = {
    # T01/S0101 总量核算（GDP）。0.43 之前 T01 一条词面都没有，于是
    # 「上海地区生产总值」解析出的主题是空的——能力比对里主体的 region
    # 又被当作范围轴（不算内容轴），最后被 shelf_gap 判成「货架上没有」，
    # 而分省表其实就在货架上。这是缺词面，不是缺数据。
    "S0101": (r"(?<![a-z])gdp(?![a-z])|国内生产总值|地区生产总值|生产总值|人均收入|"
              r"国民收入|\bnational account\b|\bproductivity\b"),
    # T13/S1301 供水与污水
    "S1301": WASTEWATER_CONTEXT.pattern,
    # T06/S0605 生命统计与死亡（儿童/婴儿死亡率，UN IGME 口径）
    "S0605": (r"儿童死亡|婴儿死亡|新生儿死亡|五岁以下死亡|"
              r"\bchild mortality\b|\bunder.?5 mortal|\binfant mortal"),
    # T06/S0601 疾病与专病（肺癌、胃癌、肿瘤…；单字「癌」汉语无卫生外语义）
    "S0601": r"癌|肿瘤|\bcancer\b|\btumor\b|\btumour\b",
    # T09/S0901 空气与排放 + S0905 气候与碳：「碳排放/温室气体」两边都可能
    # 落，两侧都认——OWID CO₂ 卡的署名判决是 S0901，只认 S0905 会把
    # 「各国二氧化碳排放量」的真匹配打成缺主题（0.18 回归实测）。
    "S0901": (r"大气排放|空气污染|污染物排放|尾气排放|碳排放|温室气体|"
              r"\bair pollution\b|\bair emission\b|\bcarbon emission\b|"
              r"\bgreenhouse gas\b"),
    "S0905": r"碳排放|温室气体|\bcarbon emission\b|\bgreenhouse gas\b",
    # T09/S0904 固废与资源
    "S0904": (r"塑料垃圾|固体废物|生活垃圾|垃圾处理|危险废物|"
              r"\b(waste|solid waste|plastic)\b"),
}

_HINTS_COMPILED = {c: re.compile(p, re.I) for c, p in NEED_HINTS.items()}


def resolve_need(text: str) -> dict:
    """兜底路径：把自然语言引导到地图节点。

    返回 {"topics": [T…], "subtopics": [S…], "hint_hits": [S…]}。
    topic 由子类的父类自动带出，不单独维护——地图上父子关系只有一份。
    """
    subs: list[str] = []
    for code, rx in _HINTS_COMPILED.items():
        if rx.search(text) and code not in subs:
            subs.append(code)
    topics: list[str] = []
    for c in subs:
        parent = tx.SUB[c][0]
        if parent not in topics:
            topics.append(parent)
    return {"topics": topics, "subtopics": subs, "hint_hits": list(subs)}


def validate_need(need: dict) -> tuple[dict, list[str]]:
    """校验 agent 传来的结构化坐标。返回 (清洗后的坐标, 错误列表)。

    只做**结构**校验：码存在、子类父类一致、年份是数。语义对不对题由
    match() 拿名片验证——地图不假装自己能理解语言。
    """
    errs: list[str] = []
    clean: dict = {}
    topics = [str(t).upper() for t in (need.get("topics") or [])]
    for t in topics:
        if t not in tx.L1_NAME:
            errs.append(f"未知主题码 {t}（合法 T01–T15，见 dp_map）")
    subs = [str(s).upper() for s in (need.get("subtopics") or [])]
    for s in subs:
        if s not in tx.SUB:
            errs.append(f"未知子类码 {s}（见 dp_map）")
        elif topics and tx.SUB[s][0] not in topics:
            errs.append(f"子类 {s} 属于 {tx.SUB[s][0]}，与 topics {topics} 不一致")
    # 子类携带的父类补进 topics：agent 只传 S 码也算合法归类
    for s in subs:
        if s in tx.SUB:
            p = tx.SUB[s][0]
            if p not in topics:
                topics.append(p)
    clean["topics"] = topics
    clean["subtopics"] = subs
    secs = [str(s).upper() for s in (need.get("sectors") or [])]
    gb = {c for c, _n, _a in secmod.SECTORS}
    for s in secs:
        if s not in gb:
            errs.append(f"未知行业门类 {s}（合法 A–T，见 dp_map）")
    clean["sectors"] = secs
    # 主体/度量两轴**必须校验**：它们参与对题判定（主体缺席一票否决），
    # 传错码时静默不匹配，agent 会以为是「货架没有」。中文名（『国家』）
    # 归一成码；两样都不认才报错，并把合法码表指回去（dp_map 有全表）。
    from . import capability as capmod          # 循环导入：capability 顶层引 map
    for k, by_label, known, shown in (
            ("entities", capmod.ENTITY_CODE_BY_LABEL,
             set(capmod.ENTITY_LABEL), "entity_axis"),
            ("measures", capmod.MEASURE_CODE_BY_LABEL,
             set(capmod.MEASURE_LABEL), "measure_axis")):
        vals = [str(v).strip() for v in (need.get(k) or []) if str(v).strip()]
        out: list[str] = []
        bad: list[str] = []
        for v in vals:
            code = v if v in known else by_label.get(v) or by_label.get(v.lower())
            if code in known:
                if code not in out:
                    out.append(code)
            else:
                bad.append(v)
        if bad:
            errs.append(f"未知{k}码 {bad}（合法值见 dp_map.{shown}，可用中文名）")
        clean[k] = out
    geo = str(need.get("geo") or "").strip()
    clean["geo"] = geo
    # 0.44：地理层级由 agent 给。层级是有限枚举（见 dp_map.geo_axis），所以
    # 能像 topics 一样校验；给了就用它，不给才退回字面推断。字面与声明不
    # 一致只提示、不阻断——语义在 agent 那边，平台没资格否决它的判断，但
    # 有义务告诉它「按字面读是另一层」。
    from . import locator as locmod          # 循环导入：locator 不引 map
    gl = str(need.get("geo_level") or "").strip().lower()
    if gl:
        if gl not in locmod.GEO_RANK:
            errs.append(f"未知 geo_level {gl!r}（合法值见 dp_map.geo_axis.levels）")
            gl = ""
        elif geo:
            guessed = locmod.want_level(geo)
            if guessed and locmod.GEO_RANK.get(guessed, 0) != locmod.GEO_RANK[gl]:
                clean["geo_level_conflict"] = {
                    "geo_level": gl, "literal_guess": guessed,
                    "note": ("按你给的 geo_level 判；字面推断是另一层。"
                             "若层级给错，改 geo_level 重投。"),
                }
    clean["geo_level"] = gl
    # 0.44：指标轴。locator.match_indicators 早就吃 need.indicator，但这里
    # 没有把它放进 clean，等于 agent 声明「我要第一产业增加值」在这一层被
    # 丢掉了，只能退回从 query 里 2-gram 对齐 63k 个指标名——**框里没有的
    # 字段，agent 就只能猜**。这是「谁来做语义」漏掉的最后一格：具体那一列
    # 是自由文本（63,415 个指标名，不是码表），只有 agent 能译准。
    ind = [str(x).strip() for x in (need.get("indicator") or [])
           if str(x).strip()]
    if ind:
        clean["indicator"] = [x[:64] for x in ind][:8]
    else:
        clean["indicator"] = []
    # 街级地名（「衡山路」）不属于任何码表，但它是真实约束，必须透传到
    # match——否则「某条街附近」会被当成城市级提问判全绿。
    clean["geo_detail"] = str(need.get("geo_detail") or "").strip()
    for k in ("year_from", "year_to"):
        v = need.get(k)
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            errs.append(f"{k} 不是年份: {v!r}")
            continue
        if not 1900 <= iv <= 2100:
            errs.append(f"{k} 超出合理年份: {iv}")
            continue
        clean[k] = iv
    # 0.40：需求约束是**声明**，不是坐标。这里只做形状校验（类型 / 长度），
    # 不解释它们的值：平台不替 agent 决定「多新算新、多少钱算贵」。
    # 未知键仍然忽略——老 agent 传多一个词不该整次调用失败。
    from . import capability as capmod          # 循环导入：capability 顶层引 map
    for k in capmod.CONSTRAINT_KEYS:
        v = need.get(k)
        if v in (None, "", [], {}):
            continue
        if k in ("quality_requirement", "purpose", "regions"):
            if not isinstance(v, list):
                errs.append(f"{k} 应为数组: {v!r}")
                continue
            clean[k] = [str(x)[:32] for x in v][:8]
        elif k in ("budget_limit", "granularity"):
            if not isinstance(v, dict):
                errs.append(f"{k} 应为对象: {v!r}")
                continue
            clean[k] = {str(a)[:24]: b for a, b in list(v.items())[:8]}
        else:
            if not isinstance(v, str):
                errs.append(f"{k} 应为字符串: {v!r}")
                continue
            clean[k] = v[:32]
    return clean, errs


def _geo_axis() -> dict:
    """地理轴的**框**：层级枚举 + 省级取值 + 已实测的边界。

    为什么不在这里判「浦东新区属于上海」：那是知识，不是坐标校验。地名到
    层级的映射由 agent 做（它读得懂「浦东新区是区」），平台只做两件事——
    说清有哪几层、说清每层在当前源上能不能取到。
    """
    from . import locator as locmod          # 循环导入：locator 不引 map
    return {
        "form": ("自由文本地名（中英文皆可）。给了 geo_level 就按它判层级，"
                 "不再从字面反推；不给才由平台按字面推断。推断是启发式"
                 "（剥省名看剩余量），可能错——给得出来就给。"),
        "level_rule": ("geo_level 与 geo 字面冲突时以 geo_level 为准，并在回显的"
                       " need.geo_level_conflict 里标出字面推断结果，不阻断。层级"
                       "只说明「问的是哪一层」，不说明「源站有这一层」。"),
        "levels": [
            {"code": c, "rank": locmod.GEO_RANK[c], "name": n}
            for c, n in (
                ("national", "全国"),
                ("region", "跨省大区（东部/西部/长三角）——不是行政层级，"
                           "只能由若干省聚合，聚合口径由提问方定"),
                ("province", "省 / 自治区 / 直辖市（北京上海天津重庆按省级算）"),
                ("city", "地级市"),
                ("county", "区 / 县 / 开发区 / 街道"),
                ("hk", "中国香港"),
                ("mo", "中国澳门"),
                ("tw", "中国台湾"),
            )
        ],
        "province_values": list(locmod.PROVINCES),
        "reachability": (
            "层级是**提问**的属性；源站有没有这一层要问具体条目——"
            "dp_usage 的 locator_coverage 逐轴回 covered / absent / unknown。"
            "已实测的两条边界：(1) 国家统计局分省年度库只到省级，检索 "
            "search=上海市浦东新区 返回的 86 行 da 全是省级码 310000000000，"
            "没有区县级行；(2) 跨省大区不是任何表的原生层，只能拿若干省自己"
            "聚合。要区县级数据先确认架上有区县级源，别把分省表当区县表用。"
        ),
        "known_gaps": [
            "区县级：当前分省年度库无区县行，判 county 只会得到 out_of_scope",
            "港澳台：全国库是否含港澳台，源方未声明，平台不作推定",
        ],
    }


def describe() -> dict:
    """分级地图全貌（MCP dp_map 的载荷）。给 agent 看的，不是给人看的：
    拿到它就该把用户需求归到某个大类，再传结构化坐标路由。"""
    from . import capability as capmod          # 循环导入：capability 顶层引 map
    cats = []
    for c, name, gb, desc in tx.L1:
        subs = [
            {"code": sc, "name": nm}
            for sc, (parent, nm, _rx) in tx.SUB.items() if parent == c
        ]
        subs.sort(key=lambda x: x["code"])
        cats.append({"code": c, "name": name, "gb_anchor": gb,
                     "what": desc, "subtopics": subs})
    axes = capmod.axis_vocab()
    return {
        "ok": True,
        "map_version": MAP_VERSION,
        "sector_axis": [{"code": c, "name": n} for c, n, _a in secmod.SECTORS],
        # 主体轴与度量轴也属于「坐标」，而且是被拿来验证对题的坐标。
        # 不公开它们，need 里的 entities 就成了盲猜，猜错还静默判缺口。
        "entity_axis": axes["entity_axis"],
        "measure_axis": axes["measure_axis"],
        # 0.44：地理轴过去是唯一一条**没有公开取值域**的轴。agent 只能把地名
        # 原样塞进 geo，平台再用字面结构反推层级（locator._want_level 那套
        # 「剥省名看剩余量」）。反推的上限就是规则写得多细——「问题无限」最后
        # 就卡在这个口子上。但层级本身是**有限枚举**（5 级 + 港澳台），可以
        # 公开；公开后 agent 直接给 geo_level，平台不必再猜。
        "geo_axis": _geo_axis(),
        "axis_note": axes["note"],
        # 0.40：约束**不参与对题判定**，只产生警告。公开词表是怕 agent 把
        # 「免费」当成一个主体码传进 entities，然后困惑于为什么没命中。
        "constraint_axis": {
            "freshness_requirement": ["realtime", "recent", "historical"],
            "quality_requirement": ["authoritative", "verified"],
            "budget_limit": {"free_only": "bool", "max_amount": "int"},
            "purpose": ["commercial", "research", "redistribution", "compliance"],
            "granularity": {"spatial": ["micro", "admin"],
                            "temporal": ["annual", "monthly", "daily", "quarterly"]},
            "regions": ["自由文本地名数组；2 个以上才表示多地区需求"],
            "note": ("约束只出警告、不删候选：货架上多数源方未声明许可与时间覆盖，"
                     "硬过滤会误杀真实可取的条目。read constraints.checked / "
                     "constraints.unchecked / constraints.warnings。"),
        },
        "usage": (
            "两级地图：category(主题大类) → subtopic(子类)。理解用户需求后把"
            "需求归到大类，随 dp_route/dp_discover 的 need 参数传结构化坐标"
            "（topics/subtopics 必填其一，另带 sectors/entities/measures/geo/"
            "geo_level/year_from/year_to）；平台在大类里用各数据集的名片排出"
            "候选。**entities 与 measures 不是可选项**：主体轴决定「一条记录"
            "代表什么」，缺席时任何候选都只能判『部分满足』（unknown_axes 会写"
            "着「未验证 主体」）；度量轴说明要的是哪个量（价格/产量/时序…）。"
            "两者取值见 entity_axis / measure_axis，中文名也认。**具体那一列"
            "用 indicator 投**：它是自由文本（源站 6.3 万个指标名，不是码表），"
            "只有你能把口语译成列名；不给它，表内定位器只能拿 query 做字面"
            "模糊对齐，问「农民收入」就可能对上「农村居民人均可支配收入」。"
            "**地理轴别再"
            "只塞一段地名**：先看 geo_axis.levels 定层级，用 geo_level 明确"
            "投递，平台才不用从字面反推；geo_axis.reachability 写着每层在现有"
            "源上实测能不能取到（区县目前无行）。query 只作字面兜底，任何语言"
            "都不需要平台分词。"),
        "categories": cats,
    }
