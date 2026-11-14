"""数据集的类型体系：主题 / 形态 / 用途 三轴（单一真源）。

此前分类规则散落在草案生成脚本里，运行时却完全不知道分类——agent 只能
靠 ``domain`` 标签猜。``domain`` 是**采集主题桶，不是内容标签**（"Air
Quality Management Areas" 被标成 health、"Advertised housing prices" 被
标成 energy），拿它当分类用会稳定出错。所以类型体系必须落在包里，成为
唯一的判定入口。

三轴互相正交，各有不同用途：

===========  ==========================  ==============================
轴           取值                        agent 拿它判断什么
===========  ==========================  ==============================
主题 L1/SUB  T01–T15 + 子类 S*          这个问题该去哪个门类找
形态 FORM    M1–M7（M8=未知）            拿到手是表、还是文本、还是图
用途 USE     train/eval/stat/reference  能不能训模型 / 只能做参照
===========  ==========================  ==============================

三条不可让步的约束：

1. **单亲归属**。一条数据集只进一个子类。切不准就进 ``T00`` 待认领，
   不硬塞——多标签会把 237 条复制成 600 条，越分越臃肿。
2. **未知就是未知**。英文源名常常没有形态线索（``MLUK Datenzensus``），
   判不出就给 ``M8``/空，不用 ``M1`` 兜底冒充已知。
3. **分类不是证据**。这里只决定「去哪儿找、读多少」，能不能回答仍只由
   ``route``/``proof`` 决定。``curated`` 的条目同样不构成内容证明。

规则改动必须跑 ``scripts/audit_taxonomy.py``：它检查兜底率、门类偏斜、
子串误命中（``mental`` 曾吃进 ``Environmental``/``Départemental``）与
与基线简历的漂移。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

# ── 主题门类（GB/T 4754 门类对齐，便于与供应商名录等外部库做 join）──────
L1: list[tuple[str, str, str, str]] = [
    ("T01", "宏观经济与统计", "C42/N", "GDP、物价、生产率等总量与统计口径数据"),
    ("T02", "贸易与产业", "F51/F52", "进出口贸易、商业网点、产业结构"),
    ("T03", "金融财税与企业", "J/K72", "税收、利率、企业名录与经营主体"),
    ("T04", "人口与社会民生", "S/P25", "人口普查、贫困、就业、养老、居住"),
    ("T05", "教育与人力", "P", "学历教育、职业培训、考试与技能"),
    ("T06", "医疗卫生", "Q", "疾病、专病队列、诊疗预后、卫生服务"),
    ("T07", "农林牧渔与食品", "A", "种植、畜牧、渔业、林业、农产品"),
    ("T08", "能源与电力", "D", "发电、用电、能效、能源平衡"),
    ("T09", "环境与气候", "N77/N78", "空气、水、生态、固废、碳与气候"),
    ("T10", "气象与灾害", "M74/N79", "气象观测、灾害识别与灾情评估"),
    ("T11", "交通与物流", "G", "运输货运、路网设施、物流、出行管理"),
    ("T12", "工业制造与资源", "B/C", "机械装备、采矿化工、工业安全与工业语料"),
    ("T13", "城市与公用事业", "D46/N78", "供水污水、燃气热力、城市空间设施"),
    ("T14", "数字技术与 AI 语料", "I65", "对话语料、图像标注、知识问答、信息服务"),
    ("T15", "政府治理与法规", "S91/L72", "政策法规、监管处罚、政务平台"),
    ("T00", "未归类", "-", "规则无法判定，进待认领；不硬塞进任何门类"),
]
L1_NAME = {c: n for c, n, _a, _b in L1}
L1_DEF = {c: d for c, _n, _a, d in L1}
L1_GB = {c: g for c, _n, g, _d in L1}
L1_ORDER = [c for c, _n, _a, _b in L1]

# ── 顶层品类：一个「平台」同时覆盖所有主题，塞进主题门类只会造出巨型「其他」
KIND_LABELS = {
    "D": "数据集",
    "P": "平台 / 门户",
    "A": "聚合索引 / 检索服务",
}
# 公告附件本体（供给清单 xlsx）：是清单文件，不是业务数据集，不参与门类归属。
_OFFICIAL_ATTACH = re.compile(r"官方附件|公告附件")
KIND_RULES: list[tuple[str, str]] = [
    ("A", r"datasets server|openalex|聚合|索引服务|知识图谱平台"),
    ("P", r"平台|门户|目录|数据中心|数据库|开放数据|open data|data\.gov|data\.europa|"
          r"datasets$|kaggle|hugging face|zenodo|figshare|arxiv|服务|存储库|仓库|"
          r"beaconmfg|csmar|\bwind\b|ceic|oecd"),
]

# ── 子类：单亲归属，*09 为该门类的兜底槽 ───────────────────────────────
_S: dict[str, tuple[str, str, str]] = {
    "S0101": ("T01", "总量核算与生产率", r"\bgdp\b|productivity|national account|生产率"),
    "S0102": ("T01", "价格与指数", r"price ind|consumer price|inflation|\bcpi\b|价格指数|物价"),
    "S0103": ("T01", "区域统计与指标汇编", r"economic indicator|regional|namur|municipal|statistic|统计|指标|time series"),
    "S0104": ("T01", "数字经济与创新", r"digital transaction|digital econom|innovat|r&d\b|research and development|数字交易|数字经济|创新"),
    "S0109": ("T01", "其他宏观经济", r"."),

    "S0201": ("T02", "进出口与双边贸易", r"trade|import|export|comtrade|贸易|进出口"),
    "S0202": ("T02", "商业网点与主体分布", r"commercial|retail|shop|商铺|商圈|网点"),
    "S0203": ("T02", "产业结构与市场规模", r"business|enterprise|产业|市场"),
    "S0209": ("T02", "其他贸易产业", r"."),

    "S0301": ("T03", "税收", r"\btax(es)?\b|税收|纳税"),
    "S0302": ("T03", "利率与货币金融", r"interest rate|federal funds|monetary|\bfed\b|利率|货币"),
    "S0303": ("T03", "企业名录与经营主体", r"registry|list of|directory|companies|enterprise|vendor|名录|企业|商户"),
    "S0304": ("T03", "财务报表与信用", r"financial statement|credit|财报|信用"),
    "S0309": ("T03", "其他金融财税", r"."),

    "S0401": ("T04", "人口与普查", r"population|census|resident|demographic|birth|immigrant|人口|户籍|常住"),
    "S0402": ("T04", "贫困与收入分配", r"poverty|\bgap\b|vulnerable|middle class|severity|贫困|收入分配"),
    "S0403": ("T04", "就业与劳动", r"employ|labour|labor|union|\bjobs?\b|workforce|就业|劳动"),
    "S0404": ("T04", "养老与社会保障", r"pension|welfare|social security|养老|社保"),
    "S0405": ("T04", "住房与居住", r"housing|property|rent|住房|房价"),
    "S0409": ("T04", "其他社会民生", r"."),

    "S0501": ("T05", "学历教育", r"school|pupil|student|universit|attainment|education|bildung|ekpaideys|教育|招生|学历|中学|高中"),
    "S0502": ("T05", "职业与技能培训", r"vocational|training|skill|phonics|epal|职业|技能|培训"),
    "S0503": ("T05", "考试与评价", r"test|exam|survey|考试|评价|测评"),
    "S0509": ("T05", "其他教育人力", r"."),

    # mental 收紧为限定词：裸 `mental` 会命中 Environmental / Départemental
    "S0601": ("T06", "疾病与专病队列", r"cancer|disease|patient|sclerosis|stwardnienie|diabetes|dementia|asthma|专病|肿瘤|癌|心血管|代谢|慢病"),
    "S0602": ("T06", "诊疗与预后", r"surger|treatment|predict|诊疗|预后|切除|复发"),
    "S0603": ("T06", "卫生服务与机构", r"hospital|health system|health pathway|health map|emergency|ambulance|centre|centres|clinic|mental health|psychiatr|psycholog|psychische|gesundheit|卫生|医院|机构|急救"),
    "S0604": ("T06", "健康行为与调查", r"survey|wellbeing|well-?experienced health|health check|neighbourhood|census|general health|健康|行为|调查"),
    "S0605": ("T06", "生命统计与死亡", r"life expectancy|mortality|\bdeath|life table|survival rate|预期寿命|死亡率"),
    "S0609": ("T06", "其他医疗卫生", r"."),

    "S0701": ("T07", "种植业", r"crop|agricultur|farm|horticultur|棉|种植|农业|园艺"),
    "S0702": ("T07", "畜牧与养殖", r"livestock|breed|cattle|pig|畜牧|养殖|猪|禽"),
    "S0703": ("T07", "渔业水产", r"fisher|fishing|aquatic|seal|渔业|水产|大闸蟹|石斑鱼"),
    "S0704": ("T07", "地块与空间管理", r"sigpac|enclosure|parcel|地块"),
    "S0705": ("T07", "农业经营与补贴", r"holdings|subsid|land sale|经营|补贴|organic|datenzensus"),
    "S0709": ("T07", "其他农业食品", r"."),

    "S0801": ("T08", "电力供应与消费", r"electricity|power plant|grid|电力|用电|供电|发电"),
    "S0802": ("T08", "能源消费与能效", r"energy balance|energy saving|energy efficien|energy productivity|display energy cert|energy class|efficien|consumption|consommation|能效|能源平衡|消费"),
    "S0803": ("T08", "新能源与可再生", r"solar|renewable|\bwind\b|光伏|可再生|风能"),
    "S0805": ("T08", "能源补贴与政策", r"rebate|subsid|grant|incentive|\bscheme\b|program priorytetowy|补贴|补助|返还"),
    "S0809": ("T08", "其他能源", r"."),

    "S0901": ("T09", "空气与排放", r"air quality|emission|pollution|\bco2\b|f-gas|大气|空气|排放|污染"),
    "S0902": ("T09", "水环境", r"water quality|sediment|eau|水环境|水质|泥沙"),
    "S0903": ("T09", "生态与生物多样性", r"biodiversity|endangered|species|生态|生物多样|濒危"),
    "S0904": ("T09", "固废与资源", r"waste|mineral|recycl|固废|垃圾|资源|矿产"),
    "S0905": ("T09", "气候与碳", r"climate|carbon|碳|气候"),
    "S0909": ("T09", "其他环境", r"."),

    "S1001": ("T10", "气象观测与预报", r"weather|meteorolog|rain|storm|气象|暴雨|降水"),
    "S1002": ("T10", "灾害识别与灾情", r"disaster|flood|damage|灾情|灾害|内涝|冷害"),
    "S1009": ("T10", "其他气象灾害", r"."),

    "S1101": ("T11", "运输与货运", r"freight|transport performance|shipping|货运|运输"),
    "S1102": ("T11", "路网与基础设施", r"network|road|rail|air navigation|路网|铁路|航空|基础设施"),
    "S1103": ("T11", "物流与供应链", r"logistic|物流|供应链"),
    "S1104": ("T11", "出行与交通管理", r"mobility|traffic|driving|出行|交通|驾驶|导航"),
    "S1109": ("T11", "其他交通", r"."),

    "S1201": ("T12", "机械加工与装备", r"machin|equipment|steel|装备|机加工|机械|液压"),
    "S1202": ("T12", "采矿与能源化工", r"mining|coal|chemical|petro|矿山|煤炭|石化|化工"),
    "S1203": ("T12", "工业安全与运维", r"safety|monitoring|security|安全|运维|监测"),
    "S1204": ("T12", "工业语料与图元", r"corpus|knowledge|组态|图元|基因|大模型"),
    "S1209": ("T12", "其他工业", r"."),

    "S1301": ("T13", "供水与污水", r"water supply|water network|drinking|供水|净水|污水|管网"),
    "S1302": ("T13", "燃气与热力", r"gas\b|heating|燃气|供热|热力"),
    "S1303": ("T13", "城市空间与设施", r"urban|city|municipal|lawn|flower bed|green space|城市|市政|空间|绿化"),
    "S1309": ("T13", "其他公用事业", r"."),

    "S1401": ("T14", "对话与智能体语料", r"对话|聊天|智能体|negotiat|人机交互|语言交互"),
    "S1402": ("T14", "图像与多模态标注", r"图像|影像|标注|detection|4d|重建|pixel|multimodal"),
    "S1403": ("T14", "知识问答与图谱", r"知识问答|\bqa\b|图谱|knowledge"),
    "S1404": ("T14", "软件与信息服务", r"software|可信空间|软件|信息服务|平台"),
    "S1409": ("T14", "其他数字技术", r"."),

    "S1501": ("T15", "政策与法规", r"policy|regulation|law\b|法规|政策"),
    "S1502": ("T15", "监管与处罚", r"supervis|penalt|enforcement|监管|处罚|执法"),
    "S1503": ("T15", "政务服务平台", r"government|portal|demonstration|decision|政务|政府|决议"),
    "S1509": ("T15", "其他治理", r"."),
}
SUB = {c: (l1, name, re.compile(p, re.I)) for c, (l1, name, p) in _S.items()}
SUB_NAME = {c: v[1] for c, v in SUB.items()}

# ── 同族子类：同一份内容被两个相邻子类**同时合法认领**时，两边互通 ──────
# 这是分类树自己的一处建模缺陷，不是匹配规则的松口：S0901「空气与排放」
# 的定义里含 emission/排放/co2，S0905「气候与碳」的定义里含 carbon/碳，
# **「碳排放」同时命中两个兄弟节点**。map.NEED_HINTS 已经如实把
# 「碳排放 / 温室气体 / carbon emission」绑到两个节点上（兜底路径因此会
# 同时给出 S0901 和 S0905），而实测碳数据的署名判决落在 S0901
# （OWID CO₂ 两条）。于是 agent 只按 dp_map 归到 S0905 时，一张 S0901 的
# 卡会被判「主题缺口」——同一份数据、同一个问题，只因 agent 选了另一个
# 同样合法的节点就变成「货架没有」（0.28 全量回归实测：全国碳排放总量）。
#
# 只登记**已核实重叠**的这一对。固废 S0904 故意不列：塑料垃圾与二氧化碳
# 必须继续分开，那正是子类校验要拦的串门（0.15 百问循环）。
SUB_ALIAS: dict[str, tuple[str, ...]] = {
    "S0901": ("S0905",), "S0905": ("S0901",),
}


def sub_family(code: str) -> set[str]:
    """该子类连同同族兄弟姐妹的集合。只判「能不能通」，不改归属。"""
    return {code, *SUB_ALIAS.get(code, ())}


def subs_of(l1: str) -> list[dict]:
    """某门类下的子类清单（含兜底槽，调用方自行决定是否展示）。"""
    out = []
    for code, (pl1, name, _p) in SUB.items():
        if pl1 == l1:
            out.append({"sub": code, "name": name, "fallback": code.endswith("09")})
    return sorted(out, key=lambda x: x["sub"])


# ── 门类判定：顺序即优先级，前置守卫先吃掉会被宽词抢走的具体语义 ────────
_L1_RULES: list[tuple[str, str]] = [
    ("T15", r"administrative data|惠企|政务|政策|法规"),
    ("T03", r"\btax(es)?\b|税收|纳税"),
    ("T03", r"interest rate|federal funds|monetary policy|利率|货币"),
    ("T03", r"vendor registry|供应商灯塔|企业数据基座|企业名录|商户数据|registry index"),
    ("T02", r"commercial|retail|shop|商铺|商圈"),
    # 守卫：绿化养护 / 草坪花坛 / 绿色经济机构名（原被 "economy" 带进 T01）
    ("T13", r"lawns?\b|flower beds?\b|green space|green area|green economy|street furniture|parks? and gardens|绿化|草坪|花坛"),
    # 守卫：房价（原被 energy class 带进 T08）
    ("T04", r"housing price|advertised housing|house price|property price|\brent\b|rental price|房价|租金"),
    # 守卫：技能/劳动力（原被 economy 带进 T01）
    ("T05", r"\bskills?\b|workforce skills|curricul|literacy|技能"),
    # 守卫：就业（jobs 原未入 T04）
    ("T04", r"\bjobs?\b|job creation|employment rate|就业"),
    # 守卫：交通网络（原被 Departemental 的 "mental" 带进 T06）
    ("T11", r"r[ée]seau de transport|transport network|air navigation|freight|rail network|bus network|metro"),
    # 0.39 守卫：environMENTAL 曾被子串 "mental" 吃进 T06 医疗；「环境卫生」
    # 是环境口径，不是诊疗数据。départemental 是法国行政层级，归政务。
    ("T09", r"environmental|\benvironment\b"),
    ("T15", r"d[ée]partemental"),
    ("T04", r"poverty|middle class|vulnerable|贫困"),
    ("T04", r"pension|welfare|social security|养老|社保"),
    ("T06", r"\bhealth\b|hospital|medical|disease|cancer|patient|pharmac|mental health|psychiatr|psycholog|psychische|wellbeing|surger|专病|肿瘤|医疗|诊疗|预后|随访|心血管|癌|life expectancy|mortality"),
    ("T05", r"education|school|pupil|student|training|vocational|phonics|attainment|universit|bildung|ekpaideys|考试|高考|招生|教育|学历"),
    ("T14", r"语言交互|对话|聊天|智能体|大模型|知识问答|问答对|语料|可信空间|4d时空|智能家居|组态图元"),
    ("T07", r"agricultur|farm|horticultur|livestock|fisher|fishing|breed|forestry|crop|sigpac|organic|datenzensus|大闸蟹|石斑鱼|农业|渔业|畜牧|棉|种植|园艺"),
    ("T08", r"energy|electricity|solar|renewable|power plant|能源|电力|发电|光伏|能效"),
    ("T09", r"environment|emission|pollution|air quality|biodiversity|waste|climate|\bco2\b|sediment|water quality|mineral|环境|污染|水质|生态|排放|大气|空气"),
    ("T10", r"weather|meteorolog|rain|storm|disaster|flood|气象|暴雨|灾情|内涝|冷害|灾害"),
    ("T11", r"transport|freight|shipping|mobility|\brail\b|logistic|traffic|driving test|交通|运输|货运|物流|导航|路网"),
    ("T12", r"industry|industrial|manufactur|mining|coal|machin|steel|chemical|装备|机加工|矿山|煤炭|石化|制造|工业|液压"),
    ("T13", r"water supply|water network|housing|property|municipal|供水|管网|燃气|供热|城市"),
    ("T04", r"population|census|resident|demographic|birth|immigrant|employ|labour|union|人口|户籍|常住|就业"),
    ("T01", r"\bgdp\b|productivity|price ind|consumer price|inflation|economic|economy|statistic|统计|生产率|物价|指数"),
    ("T15", r"government|regulation|policy|demonstration|law\b|监管|处罚"),
    ("T02", r"trade|import|export|comtrade|贸易|进出口"),
]
L1_RULES = [(c, re.compile(p, re.I)) for c, p in _L1_RULES]

# 弱证据：只在名称看不出、但 domain 标签/发布方能推断时使用（confidence=medium）
_WEAK = {
    "T01": r"statistic|econom|宏观", "T02": r"trade|贸易", "T04": r"poverty|population|人口",
    "T05": r"education|literacy|教育", "T06": r"health|健康|医疗|life expectancy|mortality",
    "T07": r"agricultur|渔业|农业", "T08": r"energy|electricity|能源|电力",
    "T09": r"environment|air quality|环境|co2", "T10": r"weather|气象|暴雨",
    "T11": r"transport|交通", "T12": r"industry|工业", "T14": r"图像|标注|问答",
}
WEAK = {c: re.compile(p, re.I) for c, p in _WEAK.items()}

# ── 源方分类映射（0.39）────────────────────────────────────────────────
# 世行 /v2/indicator 的 ``topics`` 只覆盖 21.6%，香港 CKAN 的 ``groups``
# 覆盖得多，但两者在货架上都落在 ``domain`` 里，与「源方数据库名」混在一起。
# 0.38 把整个 domain 当弱证据用，结果 8,779 条盖了 medium 的章、9,867 条
# 仍是 T00——库名（``WDI Database Archives``）根本不是主题。
#
# 现在只认一张**显式翻译表**：源方确实给了分类名，我们把它翻成 L1，逐条
# 落盘可审。证据档因此是新增的 ``source_mapped``：比 inferred 强（有源方
# 分类作依据），比 declared 弱（不是发布方写进合同的主题声明）。
# 翻不出来的一律不猜，T00 待人工定案。表由 scripts/build_source_db_map.py 生成。
SOURCE_MAP_PATH = Path(__file__).parent / "data" / "source_db_map.json"


@lru_cache(maxsize=1)
def _source_map() -> dict:
    try:
        return json.loads(SOURCE_MAP_PATH.read_text(encoding="utf-8")).get("rules") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def source_l1(item: dict) -> tuple[str, str, str] | None:
    """返回 (L1, 依据档, 源方分类原文)；没查到就返回 None，绝不猜。"""
    mp = _source_map()
    for label in item.get("domain") or []:
        key = str(label).strip()
        row = mp.get(key)
        if row and row.get("l1"):
            return str(row["l1"]), str(row.get("via") or "source_db_name"), key
    return None


# ── 形态轴（M8 = 未知，不当 M1 兜底）──────────────────────────────────
FORMS: list[tuple[str, str, str]] = [
    ("M5", "知识问答/图谱", r"知识问答|问答对|knowledge graph|图谱"),
    ("M4", "图像/多模态", r"图像|影像|目标检测|detection|标注|4d|重建|pixel"),
    ("M3", "对话/文档/文本", r"policy|法规|framework|plan\b|report|archive|magazine|document|通稿|文本|scorecard|对话|聊天|语料|语言交互|谈判|问答"),
    ("M2", "时序监测", r"time series|时序|监测|monitoring|序列|daily|monthly"),
    ("M6", "名录/登记", r"registry|list of|directory|名录|companies|企业数据|商户|holdings"),
    ("M7", "地理/空间", r"\bgis\b|spatial|network|inspire|sigpac|enclosure|图元|空间|areas?\b|map\b"),
    ("M1", "指标/统计表", r"statistic|indicator|\brate\b|\bcount\b|percentage|index|balance|headcount|\bgap\b|severity|scheme|survey|统计|指标|指数"),
]
_FORM = [(c, n, re.compile(p, re.I)) for c, n, p in FORMS]
FORM_NAME = {c: n for c, n, _p in FORMS}
FORM_NAME["M8"] = "未知形态"

# ── 用途轴（补 Kaggle 的 task 维度）：agent 的第三问常是"能不能训模型" ──
# Kaggle 的 task 是**发布方自己标的**；我们没有这份声明，只能两档：
#   declared  名字/说明里明写了训练、评测、名录
#   inferred  行业惯例推断（T14 的对话/问答/标注语料即训练语料），可被推翻
# 两档分开记，是因为前者能写进合同、后者只能作为建议。判不出就留空——
# 用途猜错比留空贵得多。
USE_LABELS = {
    "train": "可训练（有标注/语料）",
    "eval": "可评测（有基准/金标）",
    "stat": "仅统计口径（聚合值）",
    "reference": "仅参照（名录/法规/词典）",
    "": "未标注",
}
USE_BASIS_LABELS = {"declared": "源方声明", "inferred": "行业惯例推断（可推翻）"}
_USE_RULES: list[tuple[str, str]] = [
    ("train", r"训练集|训练数据|训练语料|标注数据|语料库|corpus|training set|annotated dataset|ground truth"),
    ("eval", r"benchmark|评测集|评测基准|gold standard|test set|基准测试"),
    ("reference", r"registry|list of|directory|codebook|handbook|classification|standard|名录|词典|法规|标准|清单"),
    ("stat", r"indicator|statistic|time series|指标|统计|时序"),
]
USE_RULES = [(c, re.compile(p, re.I)) for c, p in _USE_RULES]

# ── 人工判决（confidence=curated）：每条都有可被推翻的依据 ──────────────
# 价值不在"改得对"，而在"署名、可复核"。规则打不动的条目才进这里，
# 攒到 10 条以上同类再考虑开子类，否则会把兜底率换成碎片率。
CURATED: dict[str, tuple[str, str, str]] = {
    # 0.14 法国污水治理专项：名称里的「人口 / 环境」会让规则分别误判成
    # T04 人口民生、T09 环境气候（medium 弱证据）。逐条署名判决为
    # T13 城市与公用事业 / S1301 供水与污水，依据写在第三列。
    "fr_ww_64505e7f037f":
        ("T13", "S1301", "Eurostat env_ww_con：人口接入污水处理厂比例，属供水污水，非人口普查"),
    "fr_ww_a8524fc19b5d":
        ("T13", "S1301", "World Bank SH.STA.BASS.ZS：基本卫生设施覆盖率，属供水污水与卫生服务"),
    "fr_ww_027bb0cd0dc2":
        ("T13", "S1301", "World Bank SH.STA.SMSS.ZS：安全管理的卫生服务覆盖率，属供水污水"),
    "fr_ww_712e462f212a":
        ("T13", "S1301", "data.gouv.fr Indicateurs annuels Eaux usées：地方污水/废水年度指标"),
    "fr_ww_25d017f1f983":
        ("T13", "S1301", "data.gouv.fr Budget annexe Assainissement：地方污水治理专项预算"),
    # 0.15 三问复测：OWID 两族条目被 S0103「统计/指标」兜底吸进 T01 宏观，
    # 语义错位。按主题表真实语义逐条判决。
    "owid_child_mortality_e6aab525":
        ("T06", "S0605", "OWID child mortality：五岁以下儿童死亡率年度序列（UN IGME 口径），属生命统计与死亡"),
    "owid_co2_emissions_per_capita_07ed9ae3":
        ("T09", "S0901", "OWID CO₂ per capita：人均二氧化碳排放序列，属空气与排放，非宏观经济"),
    "owid_annual_co2_emissions_per_country_67ef6dd2":
        ("T09", "S0901", "OWID annual CO₂ emissions：分国二氧化碳排放年度序列，属空气与排放"),
    "eu_statistiques_garderie_p_riscolaire_89b57d54":
        ("T04", "S0409", "法语 garderie périscolaire=课外托育，市政府发布的儿童照护服务统计，非宏观指标"),
    "eu_statistiques_restaurant_scolaire_4b0218bc":
        ("T04", "S0409", "法语 restaurant scolaire=学校食堂，属市政家庭服务统计，不是教育成效数据"),
    "eu_emergency_calls_transferred_to_sns_a925b849":
        ("T06", "S0603", "发布方 INEM（葡萄牙国家医疗急救研究所），SNS24 为急救热线，属院前急救服务"),
    "eu_nfz_o_zdrowiu_stwardnienie_rozsian_0f95f741":
        ("T06", "S0601", "波兰国家医保局 NFZ 按病种发布，Stwardnienie rozsiane=多发性硬化（专病）"),
    "eu_offene_daten_der_direktion_f_r_psy_3d39b4fe":
        ("T06", "S0603", "希腊卫生部精神卫生司开放数据，psychische Gesundheit=心理健康服务"),
    "eu_international_general_social_surve_62e41f2c":
        ("T06", "S0604", "ISSP 2011 年模块主题为 Health and Health Care，属社会调查中的健康模块"),
    "eu_umweltschutz_betriebe_1_investitio_c300644e":
        ("T09", "S0909", "萨克森州统计局环保支出与投资统计（Umweltschutz=环境保护），属环境经济核算"),
    "eu_international_general_social_surve_17a653ed":
        ("T09", "S0909", "ISSP 2010 年模块主题为 Environment，属环境态度调查"),
    "eu_embossed_scorecard_difference_in_a_e3c343b5":
        ("T00", "", "DOV（佛兰德斯地下数据库）高程模型，属基础地理/测绘；现有 15 门类无对应位置，暴露门类缺口"),
    "eu_consommation_d_nergie_663a20c8":
        ("T08", "S0802", "图卢兹都会区能耗数据，consommation d'energie=能源消费"),
    "eu_program_priorytetowy_energia_plus_c0821798":
        ("T08", "S0805", "NFOŚiGW 的 Energia Plus 优先资助计划名录，属能源补贴而非能耗统计"),
    "eu_mluk_datenzensus_8f4bad44":
        ("T07", "S0705", "德国农业环境气候部数据普查（Datenzensus），发布方职能指向农业经营统计"),
    "eu_zweite_bildung_bildung_bildung_bil_2a98ccf2":
        ("T05", "S0501", "希腊语 Δευτεροβάθμια εκπαίδευση=中等教育，按地区统计公立高中；名称重复是源方抓取缺陷"),
    "eu_zweite_bildung_bildung_bildung_bil_c4d09e4a":
        ("T05", "S0502", "同上前缀，EPAL=希腊职业高中，属职业教育"),
    "eu_dopafmai_decisions_d3c826a1":
        ("T15", "S1503", "ΔΟΠΑΦΜΑΙ 为市政法人机构的决议记录；domain 标 education 是采集主题桶误标，实体是政务决议"),
    "eu_chiffres_de_l_conomie_de_l_innovat_cb365e19":
        ("T01", "S0104", "南特都会区创新经济指标汇编（Atlanpole 创新园），属创新/数字经济指标"),
    "eu_pln_2021_quality_air_75bdfe8d":
        ("T09", "S0901", "奥克西塔尼大区空气质量监测（PLN Quality AIR），属空气与排放"),
    "eu_pln_2023_quality_air_f5cf605e":
        ("T09", "S0901", "同上，2023 年份版本"),
}


# ── 判定入口 ──────────────────────────────────────────────────────────
def host_of(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower().replace("www.", "")
    except ValueError:
        return ""


def kind_of(item: dict) -> str:
    """D 数据集 / P 平台门户 / A 聚合索引。

    不能简单用「无 data_url 就是平台」：国家平台那 34 条同样没有 data_url
    （需登记/授权才能取数），但它们是货真价实的数据集。所以 data_url 只有
    指向**具体端点**（非站点首页）时才作为数据集判据，``hub_`` 前缀作为
    目录服务的补充判据——这是采集阶段就能写入的信息。
    """
    name = item.get("name") or ""
    # 门户目录里的一条记录，不是门户本身（0.35）。这批条目带着 ``portal``
    # 块，说明它来自某个门户的目录接口——是「目录里的一条」，不是「一个
    # 站点入口」。名称规则里的「服务 / 目录 / 数据库」会把它误判成平台，
    # 于是「都匀市社保卡服务网点」会带着 1303 条子集变成一个 hub。
    if item.get("portal"):
        return "D"
    # 香港开放数据平台的条目同理（0.37）：它们是「一个数据集页」，名称里的
    # 「服务」是服务类数据，不是服务门户。不加这一条，3,885 条里凡是叫
    # 「XX 服务」的都会变成带 3,831 条子集的 hub。
    if item.get("hk"):
        return "D"
    k = "D"
    for kk, pat in KIND_RULES:
        if re.search(pat, name, re.I):
            k = kk
            break
    data_url = (item.get("data_url") or "").strip()
    if data_url:
        parsed = urlparse(data_url)
        if parsed.path.strip("/") or parsed.query:
            return "D"
        return k
    if (item.get("dataset_id") or "").startswith("hub_"):
        return k if k in ("P", "A") else "P"
    return k


def assign_l1(name: str, item: dict) -> tuple[str, str, str]:
    """返回 (门类, 置信度, 命中依据)。依据要能被人复核，所以留命中的原文串。

    0.39 的证据优先级：名称规则(high) → 源方分类映射(source_mapped) →
    名称/发布方词表(medium) → T00。**domain 不再整体当弱证据**：世行的
    domain 是数据库名、香港的是 CKAN 分组，混着地理标签（"世界银行"/
    "中国香港"）——拿它猜主题是 0.38 分类塌陷的根因。
    """
    for code, pat in L1_RULES:
        m = pat.search(name)
        if m:
            return code, "high", m.group(0)
    src = source_l1(item)
    if src:
        return (src[0], "source_mapped",
                "源方分类「%s」→ %s（%s：一次翻译，非合同声明）"
                % (src[2], L1_NAME.get(src[0], src[0]), src[1]))
    blob = " ".join([name, item.get("publisher") or ""])
    for code, pat in WEAK.items():
        m = pat.search(blob)
        if m:
            # 0.39：名称/发布方词表推断既不是人工定案，也不是源方分类。
            # 扩容前这条通道产出 0 条，去掉 domain 后还剩 115 条，改标
            # inferred——「零 medium」是给人审过的条目立的承诺，不该被
            # 词表悄悄稀释，也不该把机器推断冒充成中等置信度。
            return code, "inferred", "按名称/发布方词表推断：" + m.group(0)
    # 公告附件本体（供给清单 xlsx）不是业务数据集：它自己就是那张清单，
    # 没有门类可归。T00 待认领，但要把「为什么没归」写出来——空理由会让
    # 运营以为漏判，也会让「每条都有依据」的回归永远挂在它身上。
    if _OFFICIAL_ATTACH.search(name):
        return "T00", "none", "官方公告附件本体（供给清单文件），非业务数据集"
    return ("T00", "none",
            "源方未给可翻译的分类名，名称规则也未命中；未归类待人工定案")


def sub_of(l1: str, name: str) -> str:
    for code, (pl1, _n, pat) in SUB.items():
        if pl1 != l1 or code.endswith("09"):
            continue
        if pat.search(name):
            return code
    return l1.replace("T", "S") + "09"


def form_of(name: str) -> str:
    for code, _label, pat in _FORM:
        if pat.search(name):
            return code
    return "M8"


# ── 形态的第二判据：distribution 的文件格式（源方声明，不是我们猜）──────
# 名称给的是「内容意图」，格式给的是「拿到手是什么」。两者不是一回事，
# 也不能互相替代：名称为统计、分发却是 PDF 的，agent 打开才发现算不了。
#
# 用法有严格优先级（见 form_with_meta）：
#   * 结构化载体（地理/图像/图谱）→ 格式覆盖名称，这是结构性事实
#   * 名称判得出（M1/M2/M3/M6）→ 保留名称，因为 CSV 既可能是指标表也可能是
#     时序，格式无法区分；名称能区分
#   * 名称判不出（M8）→ 用格式兜底，这才是补采集的主要收益
# 不这样分优先级，一补格式就会把「时序监测」全压平成「指标表」。
_FORMAT_FORM = {
    # 表格载体
    "CSV": "M1", "TSV": "M1", "XLS": "M1", "XLSX": "M1", "ODS": "M1",
    "PARQUET": "M1", "PX": "M1", "JSON-STAT": "M1", "SDMX": "M1",
    # 文档/文本载体
    "PDF": "M3", "DOC": "M3", "DOCX": "M3", "ODT": "M3", "TXT": "M3", "RTF": "M3",
    # 图像载体
    "PNG": "M4", "JPG": "M4", "JPEG": "M4", "TIFF": "M4", "TIF": "M4", "GIF": "M4",
    # 图谱载体
    "RDF": "M5", "RDF_XML": "M5", "RDF_TURTLE": "M5", "N3": "M5", "TTL": "M5",
    "JSON_LD": "M5", "OWL": "M5", "NT": "M5",
    # 地理/空间载体
    "SHP": "M7", "SHAPE-ZIP": "M7", "GEOJSON": "M7", "GPKG": "M7", "KML": "M7",
    "KMZ": "M7", "GML": "M7", "WMS_SRVC": "M7", "WFS_SRVC": "M7",
    "WCS_SRVC": "M7", "WMTS_SRVC": "M7", "GEOTIFF": "M7",
    "MAP_SRVC": "M7", "MAP_PRVW": "M7", "GPX": "M7",
}
# 归一：源方写得五花八门（rdf+xml / turtle / ld+json），先归一再查表
_FORMAT_ALIAS = {
    "RDF+XML": "RDF_XML", "APPLICATION/RDF+XML": "RDF_XML",
    "TURTLE": "RDF_TURTLE", "TEXT/TURTLE": "RDF_TURTLE",
    "LD+JSON": "JSON_LD", "JSON-LD": "JSON_LD", "APPLICATION/LD+JSON": "JSON_LD",
    "SHAPEFILE": "SHP", "ESRI SHAPE": "SHP", "EXCEL XLSX": "XLSX", "EXCEL XLS": "XLS",
    "GEOJSON": "GEOJSON", "JSON": "JSON", "XML": "XML",
}
# RDF 家族在 DCAT 目录里几乎总是**目录自身的元数据导出**，不是数据内容。
# 实测（2026-11-08，134 条已解析的欧盟条目）：41 条同时含 RDF 与内容载体，
# 0 条只有 RDF。所以 RDF 不参与形态判定——否则「纳慕尔统计查询」这种纯统计
# 表会被翻成「知识图谱」，比留空更糟。只有 RDF 的反而标未知并说明原因。
_GRAPH_FORMATS = {"RDF", "RDF_XML", "RDF_TURTLE", "N3", "TTL", "JSON_LD", "OWL", "NT"}
# 非内容载体：页面与元数据序列化。和 RDF 同一类东西——它们说明「这东西以什么
# 形式暴露」，不说明「数据本身长什么样」。实测 5 条欧盟条目只有 XML/HTML/ATOM，
# 名称也判不出（"DO PA FMAI decisions"、"ICP Forests Level II"），于是一起落进
# 未知。未知本身没错，**不写原因**才错：读卡的人会当成采集没做，再去返工一遍。
_NON_CONTENT_FORMATS = {"XML", "XHTML", "HTML", "ATOM", "RSS", "DTD_XML"}
# 打包容器：里面可能是任何东西，展开前无法判形态
_CONTAINER_FORMATS = {"ZIP", "BIN", "TAR", "GZ", "GZIP", "7Z", "RAR"}
# 通用格式：没有结构声明时既可能是记录集也可能是元数据导出（JSON 尤其如此）
_AMBIGUOUS_FORMATS = {"JSON", "JSONL", "NDJSON", "UNKNOWN"}
# 强结构化载体：只有它们能推翻名称判定
_STRONG_FORMS = {"M7", "M4"}

# ── 第三种依据：平台构造（不是源方声明，也不是我们从名称猜）─────────────
# 有些平台的发布形态是**由构造决定的**，不需要逐条声明：
#   * OWID grapher 的 .csv 就是图表的导出，表头固定 Entity,Code,Year,<指标>
#     （实测 population.csv：Afghanistan,AFG,-10000,14737），天生是实体×年×值；
#   * 世行指标接口按 country×date 返回序列。
# 这两类如果按「CSV → 指标表」判会判成 M1，正好判反——它们是时序。
# 规则只认 URL 形状，不认前缀：换成别名域名也照样命中，改名不会静默失效。
_PLATFORM_FORM: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    ("M2", re.compile(r"^https?://(?:www\.)?ourworldindata\.org/grapher/", re.I),
     "OWID grapher 图表导出为「实体×年×值」序列，非指标截面"),
    ("M2", re.compile(r"^https?://(?:api\.|data\.)?worldbank\.org/.*indicator/", re.I),
     "世行指标接口按国家×年份返回时间序列"),
)
FORMAT_LABELS = {k: FORM_NAME.get(v, v) for k, v in _FORMAT_FORM.items()}


def normalize_format(fmt: str) -> str:
    f = (fmt or "").strip().upper()
    return _FORMAT_ALIAS.get(f, f)


def format_from_url(url: str) -> str:
    """从分发 URL 的扩展名推格式（API 没给 format 时的兜底，需标注来源）。"""
    path = (url or "").split("?")[0].split("#")[0].lower()
    for ext, fid in ((".geojson", "GEOJSON"), (".shape.zip", "SHAPE-ZIP"), (".csv", "CSV"),
                     (".json", "JSON"), (".xlsx", "XLSX"), (".xls", "XLS"), (".pdf", "PDF"),
                     (".zip", "ZIP"), (".shp", "SHP"), (".gpkg", "GPKG"), (".kmz", "KMZ"),
                     (".kml", "KML"), (".xml", "XML"), (".tsv", "TSV"), (".txt", "TXT")):
        if path.endswith(ext):
            return fid
    return ""


def form_from_formats(formats: list[str]) -> str:
    """按分发格式判形态：先剔除 RDF 家族，再取占多数的映射结果。

    剔除的理由见 ``_GRAPH_FORMATS`` 上方注释——那是实测出来的，不是偏好。
    多个内容载体冲突时，结构化载体优先（地理 > 图像 > 文档 > 表格）。
    """
    tally: dict[str, int] = {}
    for f in formats:
        nf = normalize_format(f)
        if nf in _GRAPH_FORMATS:
            continue
        code = _FORMAT_FORM.get(nf)
        if code:
            tally[code] = tally.get(code, 0) + 1
    if not tally:
        return ""
    order = {"M7": 3, "M4": 2, "M3": 1, "M1": 0}
    return sorted(tally.items(), key=lambda kv: (-kv[1], -order.get(kv[0], 0)))[0][0]


def platform_form(item: dict) -> tuple[str, str]:
    """平台构造决定的形态：(形态码, 说明)；不命中返回 ("", "")。

    这是第三种依据，介于「源方声明」（distribution）与「我们猜」（name）之间：
    事实是平台级的、可复核的（打开就能看到表头），但不是这条记录自己声明的。
    所以卡上单独标 basis=platform，不混进 distribution。
    """
    for url_key in ("source_url", "data_url"):
        u = (item.get(url_key) or "").strip()
        if not u:
            continue
        for code, pat, note in _PLATFORM_FORM:
            if pat.search(u):
                return code, note
    return "", ""


def _form_unknown_note(formats: list[str]) -> str:
    """判不出形态时，说清楚"为什么判不出"。

    只有**所有**格式值都能归到「页面 / 容器 / 通用」这三类无信息桶时才给理由；
    只要还剩一个没识别的内容载体（比如一个新格式），就宁可留空——编一个
    "不足以判形态"糊上去，比空字符串更容易骗过复核。
    """
    fam = {normalize_format(f) for f in formats if f}
    fam -= _GRAPH_FORMATS          # RDF 家族在 form_with_meta 里单独给了理由
    if not fam or fam - _NON_CONTENT_FORMATS - _CONTAINER_FORMATS - _AMBIGUOUS_FORMATS:
        return ""
    parts = []
    if fam & _NON_CONTENT_FORMATS:
        parts.append("页面/元数据序列化")
    if fam & _CONTAINER_FORMATS:
        parts.append("打包容器")
    if fam & _AMBIGUOUS_FORMATS:
        parts.append("无结构声明的通用格式")
    return "仅有" + "、".join(parts) + "，不足以判内容形态"


def form_with_meta(name: str, formats: list[str] | None = None,
                   item: dict | None = None) -> tuple[str, str, str]:
    """返回 (形态码, 依据, 说明)。依据 ∈ platform / distribution / name / unknown。

    优先级见上方注释。平台构造排在最前，因为它是结构性事实且可当场验证；
    排在名称之前，是因为「OWID · population」这种名字根本判不出时序，
    而 CSV 又会把它判成指标表——两个都不如直接看平台怎么发。
    """
    pform, pnote = platform_form(item or {})
    if pform:
        return pform, "platform", pnote
    by_name = form_of(name)
    fmts = list(formats or [])
    by_fmt = form_from_formats(fmts)
    if not by_fmt:
        only_graph = bool(fmts) and all(normalize_format(f) in _GRAPH_FORMATS for f in fmts)
        if only_graph and by_name == "M8":
            return "M8", "unknown", "仅有 RDF/JSON-LD 序列化（多为目录元数据导出），不据此判形态"
        if by_name == "M8":
            return "M8", "unknown", _form_unknown_note(fmts)
        return by_name, "name", ""
    if by_fmt in _STRONG_FORMS and by_name != by_fmt:
        why = f"分发格式为{FORM_NAME.get(by_fmt, by_fmt)}类，覆盖名称判定（原判 {by_name}）"
        return by_fmt, "distribution", why
    if by_name == "M8":
        return by_fmt, "distribution", ""
    return by_name, "name", ""


def use_of(name: str, notes: str = "", l1: str = "", form: str = "") -> tuple[str, str]:
    """返回 (用途, 依据档位)。依据档位为空表示没有依据，不猜。"""
    blob = f"{name} {notes}"
    for code, pat in USE_RULES:
        if pat.search(blob):
            return code, "declared"
    # 推断档：AI 语料门类下的对话/问答/多模态标注，行业惯例即训练语料。
    # 源方没声明，所以标 inferred——agent 可以当线索用，但不能当承诺。
    if l1 == "T14" and form in ("M5", "M4", "M3"):
        return "train", "inferred"
    return "", ""


def family_key(name: str) -> str:
    """族键：抹掉年份/括号/阈值/城乡后缀，让同一指标的变体归到一起。

    世界银行 36 条贫困指标其实只有 6 个概念（headcount/gap/severity ×
    阈值 × 城乡）；SIGPAC 6 条只是 6 个年份。按族出卡才压得住读取量。
    """
    s = (name or "").lower().strip()
    s = re.sub(r"^[a-z]{2,5}\d{2,3}\s*[-–]\s*", "", s)
    s = re.sub(r"\b(19|20)\d{2}\b", "", s)
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\+?\$\s?\d+(\.\d+)?(\s*-\s*\d+(\.\d+)?)?(\s*a day)?", "", s)
    s = re.sub(r"[-–—]\s*(rural|urban|national|ward data|count|percentage)\b", "", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def formats_of(item: dict) -> list[str]:
    """条目可得的载体格式，三级回退：分发声明 → 分发访问链 → 条目自身下载链。

    ``_hub_meta`` 由 ``bdsp.enrich`` 在读取目录时挂上（不写回 listings.json）。
    第三级是 0.7 补的：OWID 8 条的 ``data_url`` 就是
    ``https://ourworldindata.org/grapher/population.csv``，扩展名明摆着，
    却因为没有侧车而被判成未知——文档一直写着「退到 URL 扩展名」，实现漏了。
    只用 ``data_url`` 不用 ``catalog_url``：后者常是目录页，扩展名不描述内容。
    """
    meta = item.get("_hub_meta") or {}
    dist = meta.get("distributions") or {}
    ids = [f.get("id") for f in (dist.get("formats") or []) if f.get("id")]
    if ids:
        return ids
    for urls in ((dist.get("access_urls") or []), (item.get("data_url"),)):
        derived = [d for d in (format_from_url(u or "") for u in urls) if d]
        if derived:
            return derived
    return []


def classify(item: dict) -> dict:
    """一条目录条目的完整类型标注（纯函数，不改入参）。

    平台类条目（P/A）不参与主题归类——它们同时覆盖所有主题，硬塞只会造出
    一个装着几十条的门类。它们的 ``l1`` 返回 ``"-"``，覆盖范围交给 facet。
    """
    name = item.get("name") or ""
    ds_id = item.get("dataset_id") or ""
    kind = kind_of(item)
    if kind != "D":
        return {
            "kind": kind, "kind_label": KIND_LABELS[kind],
            "l1": "-", "l1_name": "—", "l1_gb": "-", "l1_def": "平台/门户不参与主题归类",
            "sub": "-", "sub_name": "—",
            "confidence": "kind", "reason": "平台/门户条目：覆盖多主题，不做单亲归属",
            "form": "-", "form_name": "—", "form_basis": "", "form_note": "",
            "use": "", "use_basis": "",
            "family": "",
        }

    if ds_id in CURATED:
        l1, sub, why = CURATED[ds_id]
        conf = "curated"
        if not sub:
            sub = (l1.replace("T", "S") + "09") if l1 != "T00" else "-"
    else:
        l1, conf, why = assign_l1(name, item)
        sub = sub_of(l1, name) if l1 != "T00" else "-"

    form, form_basis, form_note = form_with_meta(name, formats_of(item), item)
    use, use_basis = use_of(name, item.get("notes") or "", l1, form)
    return {
        "kind": kind, "kind_label": KIND_LABELS[kind],
        "l1": l1, "l1_name": L1_NAME.get(l1, "—"),
        "l1_gb": L1_GB.get(l1, "-"), "l1_def": L1_DEF.get(l1, ""),
        "sub": sub, "sub_name": SUB_NAME.get(sub, "—"),
        "confidence": conf, "reason": why,
        "form": form, "form_name": FORM_NAME.get(form, "—"),
        "form_basis": form_basis, "form_note": form_note,
        "use": use, "use_basis": use_basis,
        "family": family_key(name),
    }
