"""GB/T 4754-2017 国民经济行业分类 · 门类（A–T）+ 口语别名。

这不是平台的「数据主题轴」（``taxonomy.L1`` 那 15 个主题），这是**数据集对
自己覆盖范围的声明**。两者混用会得出荒唐结论：BeaconMFG 是「全门类工商登记
数据库」，它的价值恰恰在于覆盖 A–T，而不是被塞进某一个主题里；拿主题轴去
问「餐饮在哪一类」，答案永远是「未归类」——错的是轴，不是数据。

所以链路是：
    用户问「苏州汤面」
    -> 口语别名命中 GB/T 门类 H（住宿和餐饮业）
    -> 在货架上找 **声明覆盖 H** 的数据集
    -> BeaconMFG 声明覆盖 A–T、字段含名称/经营范围/地址/联系方式
    -> 路牌回复：这条路能满足，去这儿取

平台只做「声明 × 提问」的匹配，不猜数据集里有没有某家面馆——那是取数方的
事，也正是路牌该守的边界。
"""
from __future__ import annotations

import re

# (门类码, 门类名, 口语别名)。别名是「用户会怎么说」，不是行业术语表：
# 用户不会说「住宿和餐饮业」，只会说「汤面」「面馆」「哪家好吃」。
SECTORS: list[tuple[str, str, tuple[str, ...]]] = [
    ("A", "农、林、牧、渔业",
     ("农业", "种植", "养殖", "畜牧", "渔业", "林业", "农产品", "农场", "果园")),
    ("B", "采矿业",
     ("采矿", "矿山", "煤炭", "石油开采", "天然气开采", "矿产")),
    ("C", "制造业",
     ("制造", "工厂", "生产", "加工", "服装", "纺织", "食品加工", "机械", "化工",
      "电子", "汽车", "钢铁", "水泥", "家具", "印刷", "医药制造")),
    ("D", "电力、热力、燃气及水生产和供应业",
     ("电力", "供电", "发电", "燃气", "供热", "自来水", "水厂")),
    ("E", "建筑业",
     ("建筑", "施工", "工程", "装修", "建材", "安装", "市政工程")),
    ("F", "批发和零售业",
     ("批发", "零售", "贸易", "商贸", "电商", "网店", "超市", "便利店", "门店",
      "经销商", "代理商")),
    ("G", "交通运输、仓储和邮政业",
     ("交通运输", "交通", "出行", "运输", "物流", "仓储", "快递", "货运", "客运", "港口", "航空", "铁路")),
    ("H", "住宿和餐饮业",
     ("餐饮", "饭店", "饭馆", "餐厅", "餐馆", "面馆", "汤面", "拉面", "小吃",
      "快餐", "火锅", "烧烤", "咖啡", "奶茶", "酒吧", "外卖", "食堂", "住宿",
      "酒店", "宾馆", "旅馆", "民宿", "农家乐")),
    ("I", "信息传输、软件和信息技术服务业",
     ("软件", "信息技术", "互联网", "通信", "IT", "数据服务", "云计算", "游戏")),
    ("J", "金融业",
     ("金融", "银行", "保险", "证券", "基金", "小贷", "支付", "融资租赁", "典当")),
    ("K", "房地产业",
     ("房地产", "物业", "楼盘", "中介", "房屋租赁", "开发商")),
    ("L", "租赁和商务服务业",
     ("租赁", "商务服务", "咨询", "广告", "人力资源", "会展", "旅行社", "会计")),
    ("M", "科学研究和技术服务业",
     ("科研", "研发", "检测", "设计", "技术服务", "实验室", "专利")),
    ("N", "水利、环境和公共设施管理业",
     ("水利", "环保", "环境治理", "园林", "绿化", "污水处理")),
    ("O", "居民服务、修理和其他服务业",
     ("居民服务", "修理", "家政", "美容", "美发", "洗浴", "洗衣")),
    ("P", "教育",
     ("教育", "培训", "学校", "幼儿园", "辅导", "驾校")),
    ("Q", "卫生和社会工作",
     ("医疗", "卫生", "医院", "诊所", "药店", "养老", "社会工作", "康复")),
    ("R", "文化、体育和娱乐业",
     ("文化", "体育", "娱乐", "影视", "演艺", "健身", "网吧", "KTV", "出版")),
    ("S", "公共管理、社会保障和社会组织",
     ("公共管理", "社会保障", "社会组织", "协会", "商会", "基金会")),
    ("T", "国际组织",
     ("国际组织", "使领馆")),
]

SECTOR_NAME = {c: n for c, n, _a in SECTORS}
_ALL = "".join(c for c, _n, _a in SECTORS)

_ASCII_TERM = re.compile(r"^[a-z0-9 .&+-]+$")


def _find_term(q: str, term: str) -> int:
    """定位词面。纯拉丁词必须整词匹配，中文可以子串。

    这不是洁癖：别名 ``IT`` 只有两个字符，子串匹配会命中 ``Sensitive``、
    ``Priority``、``Facility``——把一份英国农田环境数据判成信息技术业。
    中文没有空格，整词边界无从谈起，所以中文继续走子串（``汤面`` 出现在
    ``苏州汤面店`` 里正是我们要的）。
    """
    t = term.lower()
    if _ASCII_TERM.match(t) and len(t) <= 6:
        m = re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", q)
        return m.start() if m else -1
    return q.find(t)


def detect(text: str) -> list[tuple[str, str, str]]:
    """提问里出现哪些门类。返回 [(门类码, 门类名, 命中的口语词)]。

    纯子串匹配，确定性，可复现。命中多个门类时按出现顺序返回——「苏州汤面
    店里的收银软件」同时是 H 和 I，两个都该被看见，不该二选一。
    """
    q = (text or "").lower()
    if not q:
        return []
    hits: list[tuple[int, str, str, str]] = []
    for code, name, aliases in SECTORS:
        for alias in (name, *aliases):
            pos = _find_term(q, alias)
            if pos >= 0:
                hits.append((pos, code, name, alias))
                break
    hits.sort()
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for _pos, code, name, alias in hits:
        if code not in seen:
            seen.add(code)
            out.append((code, name, alias))
    return out


def declared(item: dict) -> dict:
    """数据集声明的覆盖范围（没有声明就是空 dict，绝不替它猜）。"""
    cov = item.get("coverage")
    return cov if isinstance(cov, dict) else {}


def covers(item: dict, code: str) -> bool:
    cov = declared(item)
    sectors = cov.get("sectors") or []
    if not sectors:
        return False
    if _ALL in sectors or "all" in [str(s).lower() for s in sectors]:
        return True
    return code in [str(s).upper() for s in sectors]


def match(item: dict, query: str) -> dict | None:
    """提问 × 声明覆盖。命中才返回，附一句能直接给 agent 看的 why。

    分数刻意压在字面命中之下（每个门类 3 分，最多 2 个门类）：覆盖声明是
    「我们这类数据里有」，不等于「这条记录就在我们这儿」，不能盖过真正的
    名称/字段命中。
    """
    cov = declared(item)
    if not cov:
        return None
    hits = [h for h in detect(query) if covers(item, h[0])]
    if not hits:
        return None
    score = min(6.0, 3.0 * len(hits))
    labels = "、".join(f"{c} {n}" for c, n, _a in hits)
    fields = "、".join(cov.get("fields") or [])
    granularity = cov.get("granularity") or ""
    geo = cov.get("geo") or ""
    samples = cov.get("sector_samples") or {}
    why_parts = [f"声明覆盖 GB/T {labels}"]
    # 抽样条数是最硬的一句：它把「我覆盖这个门类」变成「这个门类里我有 6,562
    # 条小吃服务」。agent 拿它判断值不值得去，不用再问一次。
    for code, _n, _a in hits:
        if samples.get(code):
            why_parts.append(f"该类实测：{samples[code]}")
    if fields:
        why_parts.append(f"字段含 {fields}")
    if granularity:
        why_parts.append(f"粒度={granularity}")
    if geo:
        why_parts.append(f"范围={geo}")
    return {
        "score": score,
        "sectors": [{"code": c, "name": n, "matched": a} for c, n, a in hits],
        "why": "；".join(why_parts),
        "not_included": list(cov.get("not_included") or []),
    }


# ── 需求侧口语词：只给 parse_need 用，不进供给侧声明 ──────────────────
# 为什么必须分开：detect() 同时被 capability._sectors_of() 用来给 238 条
# 数据集生成能力声明。把「博物馆」「专利」「PM2.5」这些**用户会说的词**
# 塞进 SECTORS，等于让名称里带这些词的数据集凭空声明一个行业；而
# BeaconMFG 声明覆盖 A–T 全门类，于是任何新别名都会把它拉进一堆不相干
# 提问的首位。需求侧该比供给侧宽，但不能共用一个表。
#
# 收词原则：只收明确的领域名词，不收「人口」「死亡率」「统计」这类泛词
# （离线单测锁死了 detect("儿童死亡率") == []，也锁死了「不要拿企业名录
# 回答儿童死亡率」这个路由事实）。
QUERY_HINTS: tuple[tuple[str, str], ...] = (
    # R 文化、体育和娱乐业
    ("非物质文化遗产", "R"), ("博物馆", "R"), ("馆藏", "R"), ("文物", "R"),
    ("非遗", "R"), ("票房", "R"), ("电影", "R"), ("图书馆", "R"),
    ("借阅", "R"), ("出版", "R"), ("museum", "R"), ("heritage", "R"),
    ("library", "R"), ("circulation statistics", "R"), ("cinema", "R"),
    # S 公共管理、社会保障和社会组织
    ("老年人口", "S"), ("社会保障", "S"), ("社会组织", "S"), ("刑事案件", "S"),
    ("基金会", "S"), ("低保", "S"), ("特困", "S"), ("优抚", "S"),
    ("警情", "S"), ("犯罪", "S"), ("welfare", "S"), ("pension", "S"),
    # M 科学研究和技术服务业
    ("观测站", "M"), ("实验室", "M"), ("降水量", "M"), ("专利", "M"),
    ("论文", "M"), ("科研", "M"), ("气象", "M"), ("arxiv", "M"),
    ("citation", "M"), ("laboratory", "M"), ("research", "M"),
    # N 水利、环境和公共设施管理业
    ("空气质量", "N"), ("二氧化碳", "N"), ("碳排放", "N"), ("污染物", "N"),
    ("水质", "N"), ("垃圾", "N"), ("环保", "N"), ("pm2.5", "N"),
    ("air quality", "N"), ("emission", "N"), ("climate", "N"),
    ("pollution", "N"), ("waste", "N"), ("environment", "N"),
    # 0.14 法国污水治理实测：中文口语、英文术语、法国官方用语都要能解析出
    # N 门类，否则「法国 污水治理 相关数据」这类提问一条坐标都没有。
    # 只加需求侧；detect() 的供给侧词表不动（儿童死亡率仍须返回空）。
    ("污水", "N"), ("废水", "N"), ("污泥", "N"), ("水污染", "N"),
    ("污水治理", "N"), ("污水管网", "N"), ("污水处理厂", "N"),
    ("wastewater", "N"), ("waste water", "N"), ("sewage", "N"),
    ("sanitation", "N"), ("uwwtd", "N"), ("effluent", "N"),
    ("assainissement", "N"), ("eaux usées", "N"), ("eaux usees", "N"),
    ("épuration", "N"), ("epuration", "N"), ("urban waste water", "N"),
    # F / J：贸易与金融（宏观经济指标 GDP/CPI 不映射到门类，见 TOPIC_SECTOR）
    ("进出口", "F"), ("贸易额", "F"), ("银行", "J"), ("保险", "J"),
)

# 门类体系之外的主题。GB/T 4754-2017 的 A–T 是**国民经济行业分类**，
# 里面没有国防/军事。硬塞进任何门类都是编造；正确做法是认出来、说出来。
# 0.15 百问循环实测补漏：「武器贸易」「核弹头」「兵力/军队」都没有命中——
# 「武器装备」要求四字连续，「武器贸易额」落空；「现役兵力 各国军队规模」
# 整句无词面。另一个副作用是「武器贸易」被 F 批发零售门类吸收（「贸易」
# 别名），不认出军事主题就会把军贸问题当商贸问题路由。
UNMAPPED_TOPICS: tuple[str, ...] = (
    "军事", "国防", "军费", "军工", "武器装备", "退役军人", "军备",
    "武器贸易", "军火", "武器", "核弹头", "核武器", "兵力", "军队", "部队",
    "武装力量", "军事基地",
    "military", "defence", "defense", "armament", "sipri", "veteran",
    "arms trade", "warhead", "troops",
)


def detect_query(text: str) -> list[tuple[str, str, str]]:
    """提问侧门类识别：官方门类别名 + 用户口语词，去重后返回。

    供给侧 ``_sectors_of()`` 继续用 ``detect()``，两份词表互不影响。
    """
    out = list(detect(text))
    # 「国内生产总值」不是制造业：C 门类别名里有「生产」，会把 GDP 判成
    # C 制造业，接下来整页候选都是工厂/贸易条目。供给侧 detect() 不动
    # （数据集名称里出现「生产」通常确实是制造业），只在需求侧摘掉。
    macro_terms = ("生产总值", "生产率", "生产力", "生产要素", "生产函数")
    out = [h for h in out
           if not (h[2] == "生产" and any(w in text for w in macro_terms))]
    seen = {c for c, _n, _a in out}
    q = (text or "").lower()
    found: list[tuple[int, str, str]] = []
    for term, code in QUERY_HINTS:
        pos = _find_term(q, term)
        if pos >= 0 and code not in seen:
            seen.add(code)
            found.append((pos, code, term))
    for _p, code, term in sorted(found):
        out.append((code, SECTOR_NAME.get(code, code), term))
    return out


def detect_unmapped(text: str) -> str:
    """提问落在门类体系之外时，返回命中的词面；否则空字符串。"""
    q = (text or "").lower()
    for term in UNMAPPED_TOPICS:
        if _find_term(q, term) >= 0:
            return term
    return ""
