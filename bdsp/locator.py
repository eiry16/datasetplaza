"""表内定位器：把「用户要的那一格在哪张表里」变成可核对的声明。

一条目录条目只说「这里有这张表」，但提问常常精确到某一格——某个指标的
某一年、某个地区。把每格都建成条目会到千万级（7.6 万指标 × 31 省 × 25 年
≈ 5,900 万），既建不起也不需要。定位器换个做法：条目仍然是表，另附一份
**表内坐标声明**：

  * indicators  表里有哪几个指标（源方给的，最可靠）
  * geo         覆盖哪些地区
  * time        覆盖哪些年份（起、止、是否仍在更新）
  * request     要拿这一格，发什么请求

它**不取数、不缓存数值、不承诺那一格的具体数字**。它只回答「这一堆里
有没有你要的那一格」，以及「要拿的话怎么发请求」。

## 三态，不是布尔

每个轴只有三种状态：

  covered  明确包含（源方声明了范围，提问落在范围内）
  absent   明确不含（源方声明了范围，提问落在范围外）
  unknown  没声明 / 声明不完整

`unknown` 必须如实交出去。历史上两次翻车都出在把它当成别的东西：

  * 当 covered —— 0.40 盲测里 12 条「货架无路」的提问 12/12 都返回了非空
    候选，误报率 66.7%；其中「沪深两市上市公司年报 PDF 下载」甚至
    `code=OK`。
  * 当 absent —— 会把真有的路也判死。`temporal_coverage` 实测覆盖率只有
    0.05%，绝大多数表都会落到 unknown，一律当 absent 等于关掉九成九的表。

## basis 说明这个状态是哪来的

  indicator_list  源方接口返回了指标清单
  source_declared 源方声明了范围（元数据字段）
  tree_path       从分类树路径推断（例如「分省年度数据」→ 省级）
  name_parsed     从表名解析（国家统计局把年份切片写进表名，如 (2000-2002)）
  platform_probe  平台实测探测（探到某期有值，不等于声明了范围）

`name_parsed` / `tree_path` 是**推断**，不是源方声明：它们能提高命中率，
但不能单独支撑一个 `absent` 判决——推断错了就会把真路判死。所以
`strict_only()` 允许调用方只看声明级证据。
"""
from __future__ import annotations

import re

VERSION = "locator-1"

COVERED = "covered"
ABSENT = "absent"
UNKNOWN = "unknown"
STATES = (COVERED, ABSENT, UNKNOWN)

# 证据强度：declared 级才能单独判 absent。
DECLARED_BASES = frozenset({"source_declared", "indicator_list"})
BASES = ("indicator_list", "source_declared", "tree_path",
         "name_parsed", "platform_probe")

_AXES = ("indicator", "geo", "time", "granularity")

_ONGOING = ("至今", "现在", "今", "present", "now", "current")

# (2000-2002) （2023-至今） (-2014) (2015-)
_RE_PAREN = re.compile(
    r"[（(]\s*(\d{4})?\s*[-–—~至]\s*(\d{4}|至今|现在|present|now)?\s*[)）]",
    re.I)
# 1947-2002年 / 2015-2020
_RE_BARE = re.compile(r"(\d{4})\s*[-–—~]\s*(\d{4})\s*年")


def parse_period(text: str) -> dict | None:
    """从表名里解析时间区间。解析不出返回 None，绝不猜。

    国家统计局把长序列按年份切片成多张表，区间就写在表名里：
    「按行业分规模以上工业企业主要经济指标 (2000-2002)」「…（2023-至今）」
    「铁路主要干线客货运输量 (-2014)」。这是**表名推断**，不是源方声明的
    结构化字段，所以 basis 记 name_parsed，优先级低于 declared。
    """
    s = str(text or "")
    if not s:
        return None
    m = _RE_PAREN.search(s)
    if m:
        a, b = m.group(1), (m.group(2) or "")
        low = int(a) if a else None
        if b and b.isdigit():
            high, ongoing = int(b), False
        elif b and b.lower() in _ONGOING:
            high, ongoing = None, True
        else:
            high, ongoing = None, False
        if low is None and high is None:
            return None
        return {"from": low, "to": high, "ongoing": ongoing,
                "basis": "name_parsed", "matched": m.group(0).strip()}
    m = _RE_BARE.search(s)
    if m:
        return {"from": int(m.group(1)), "to": int(m.group(2)), "ongoing": False,
                "basis": "name_parsed", "matched": m.group(0).strip()}
    return None


def geo_from_path(path: list[str]) -> dict:
    """从分类树路径推断地理层级。年度/月度这类全国库与分省库在树上分叉。"""
    joined = " ".join(str(p or "") for p in (path or []))
    if any(k in joined for k in ("分省", "地区数据", "各省", "分地区")):
        return {"state": "province", "basis": "tree_path",
                "note": "分类树路径含分省标识，推断为省级粒度；未逐省核对"}
    if any(k in joined for k in ("全国", "年度数据", "月度数据", "季度数据")):
        return {"state": "national", "basis": "tree_path",
                "note": "分类树路径为全国库，推断为国家总量；不含分省"}
    return {"state": "", "basis": "", "note": "路径未给出地理层级"}


def _grams(text: str) -> set[str]:
    """中文按 2-gram，英文/数字按词。与召回侧同一套确定性切法。"""
    out: set[str] = set()
    for tok in re.findall(r"[a-z0-9.]+|[\u4e00-\u9fff]+", str(text or "").lower()):
        if re.match(r"[\u4e00-\u9fff]", tok):
            if len(tok) <= 2:
                out.add(tok)
            else:
                out.update(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.add(tok)
    return out


# 指标名里的**结构噪声**：这些词出现在几乎每张表里，拿它匹配等于没筛。
#
# 0.44 从旧表里拆出来的关键一步：旧表把「累计/同比/环比/增速/指数/比重」
# 也当噪声剥掉了，但它们不是噪声，是**限定词**——「地区生产总值」是亿元，
# 「地区生产总值增速」是 %，是两列完全不同的东西。剥掉它们之后，
# 「上海GDP增速」会命中「地区生产总值」并报 covered，等于把量纲答错。
# 噪声只留在「没有区分力」的那一类（总计/合计/平均…）。
_IND_NOISE = frozenset("""总计 合计 小计 其他 其中 以上 以下 全部 平均 本期
数量 金额 个数""".split())

# 只剥**词尾**的总计类后缀：源方常把「总计」缀在表名后面构成总计列。
# 不能剥词首的「其他」——那会让「其他交通事故死亡人数」和总计同义，
# 恰恰是要区分的两列。
_TAIL_TOTAL = re.compile(r"(总计|合计|小计|总和)\s*$")


def _clean_indicator(name: str) -> str:
    s = str(name or "")
    s = re.sub(r"[（(]\s*[^)）]*[)）]\s*$", "", s).strip()
    prev = None
    while prev != s:
        prev = s
        s = _TAIL_TOTAL.sub("", s).strip()
    return s


# 缩写 → 中文口径。只放确定无歧义的，防止把提问指错列。
ABBREV = {
    "gdp": ("生产总值", "国内生产总值"),
    "cpi": ("居民消费价格",),
    "ppi": ("工业生产者出厂价格",),
}


def _strip_geo_terms(s: str) -> str:
    """剥掉地名与时间词，只留指标部分。

    地名归 geo 轴、时间归 time 轴，都不该在指标轴上计分。这个函数同时
    作用在 query 和 need.indicator 上——只剥 query 是个真实的坑：
    agent 把整句「上海GDP增速」塞进 need.indicator 时，地名会漏回指标轴，
    让 note 里冒出「上海未出现在任何指标名中」这种自相矛盾的话。
    """
    s = str(s or "")
    for w in _NATION_WORDS:
        s = s.replace(w, "")
    for p in PROVINCES:
        s = s.replace(p, "")
    for m in MACRO_REGIONS:
        s = s.replace(m, "")
    for w in _REL_YEARS:
        s = s.replace(w, "")
    return re.sub(r"\d+|[年月日季]", "", s)


def match_indicators(loc: dict, query: str = "",
                     need: dict | None = None) -> dict:
    """在表内指标里找最像用户要的那一个。

    用 2-gram 重叠，不做语义推断——推断错了会指向错的那一列，比指向整张表
    更糟。名字被完整包含在提问里算强命中（用户常常直接念指标名）。
    """
    inds = [i for i in (loc.get("indicators") or []) if isinstance(i, dict)]
    if not inds:
        return {"matched": [], "state": UNKNOWN, "basis": "",
                "note": "该表没有指标清单，无法定位到列"}
    need = need or {}
    # need.indicator 是 agent 显式声明的「我要这一列」，它是提问的一部分，
    # 而且比 query 更明确。0.43 之前只拿 query 做匹配，于是
    # need={"indicator":["地区生产总值"]} + query="2024年上海GDP" 会被判成
    # absent——「没对上词」被写成「表里没有这一列」，等于亲手关掉真路。
    need_ind = [str(x) for x in (need.get("indicator") or []) if x]
    # 地名和时间归 geo / time 轴管，不该在指标轴上重复计分，更不该被当成
    # 「提问里没被覆盖的限定词」——否则「2024年上海GDP」会因为「上海」
    # 没出现在指标名里被判 unknown，把真路封掉。
    q_geo_free = str(query or "")
    # agent 声明的地名整串剥掉。它是最可靠的一份地名，而且分层地名
    # （「上海浦东新区」）拆成 2-gram 后会产生「浦东」「新区」这类碎片，
    # 碎片一旦漏进指标轴，就会让「浦东新区GDP」的指标轴报 unknown，
    # 掩盖掉真正的结论（地理轴已经说得比它清楚）。
    _g_need = str((need or {}).get("geo") or "").strip()
    if _g_need:
        q_geo_free = q_geo_free.replace(_g_need, "")
    q_geo_free = _strip_geo_terms(q_geo_free)
    need_ind_clean = [_strip_geo_terms(x) for x in need_ind]
    q = " ".join([str(query or "")] + need_ind).strip()
    qg = _grams(" ".join([q_geo_free] + need_ind_clean)) - _IND_NOISE
    # 常用缩写不是字面重合，但确实是同一个东西。只收无歧义的几个，
    # 不做同义词大扩张——扩错了会把提问指到别的列上。
    # 缩写扩展出来的词元是**我们**加的，不是用户明确要求的。它们必须
    # 排除在「限定词覆盖」检验之外，否则「2024年上海GDP」会因为扩展出的
    # 「国内」「内生」没落在指标名里，被判 unknown——自己加的词把自己绊倒。
    abbr_grams: set[str] = set()
    for abbr, zh in ABBREV.items():
        if abbr in q.lower():
            abbr_grams.add(abbr)
            for w in zh:
                abbr_grams |= _grams(w) - _IND_NOISE
    qg |= abbr_grams
    # need 里的主题/度量/主体值参与**打分**，但不参与**限定词覆盖检验**。
    # 它们是平台自己的枚举（T01 / country / price），不是用户说过的词；
    # 并进 qg 而不豁免，就会让「T01」去和指标名做重合检验，然后因为
    # 「T01 不在任何指标名里」把指标轴判成 unknown——自己的码把自己绊倒。
    # 0.44 实测：need={'topics':['T01'],'indicator':['第一产业增加值']} 原本
    # 会报 uncovered_terms=['t01']。自由文本只有 indicator，它照常参与检验。
    for k in ("measures", "topics", "subtopics", "entities"):
        v = need.get(k)
        if isinstance(v, (list, tuple)):
            codes = set(str(x).lower() for x in v)
            qg |= codes
            abbr_grams |= codes
    scored = []
    # 表名通常就对应「总计」那一列（源方把表名当总计口径的指标名）。
    # 但不能无条件偏袒总计：「摩托车交通事故死亡人数」问的是分车型那一列，
    # 把 +0.25 一加，总计就会压过明确修饰词。所以先看提问里有没有出现
    # 任何**区分列**的词元（某列核心词减去总计核心词的差集），有就不加成。
    tcore = _grams(_clean_indicator(loc.get("table_name") or "")) - _IND_NOISE
    cores = {}
    for it in inds:
        name = _clean_indicator(str(it.get("raw") or it.get("name") or ""))
        cores[id(it)] = (_grams(name) - _IND_NOISE, name)
    spec_hit = any((c - tcore) & qg
                   for it, (c, _n) in cores.items() if tcore and c != tcore)
    for it in inds:
        core, name = cores[id(it)]
        if not core:
            continue
        is_total = bool(tcore) and core == tcore
        # 「表名是提问的子串」只在提问没有修饰词时才算强命中：
        # 「自行车交通事故死亡人数」完整包含表名「交通事故死亡人数」，
        # 但问的显然是自行车那一列，不能因此把总计判成 1.0。
        if name and q and name in q and not (spec_hit and is_total):
            score = 1.0
        else:
            inter = core & qg
            score = len(inter) / max(1, len(core))
        if is_total and not spec_hit and score > 0:
            score = min(1.0, score + 0.25)
        elif is_total and spec_hit:
            # 提问里点名了某一列（自行车/摩托车/行人…）。此时总计常常也能
            # 拿满分——因为「自行车交通事故死亡人数」把表名字面完整包含，
            # 7 个词元全中。但用户问的显然不是总计，所以把总计压到修饰列
            # 之下，而不是靠同分排序的字母序去赌。
            score = round(score * 0.8, 3)
        if score > 0:
            scored.append((round(score, 3), it))
    scored.sort(key=lambda x: (-x[0], -len(cores[id(x[1])][0] - tcore),
                               str(x[1].get("name") or "")))
    top = [{"id": it.get("id"), "name": it.get("name"),
            "unit": it.get("unit"), "score": sc} for sc, it in scored[:5]]
    if not scored:
        # 提问里根本没有指标词（没传 indicator，query 也只是「上海的数据」）
        # ——这不是「表里没有这一列」，是**没问列**。判 absent 会让 agent
        # 以为这条路被封死了，而它其实只是还没说要哪一格。两种说法必须分开。
        if not (qg - abbr_grams):
            return {"matched": [], "state": UNKNOWN, "basis": "indicator_list",
                    "note": ("提问未指定具体指标，无法定位到列；"
                             "给 need.indicator 或用更具体的问句")}
        # 指标清单是声明过的，找不到就是真的没有——这一轴可以判 absent。
        return {"matched": [], "state": ABSENT, "basis": "indicator_list",
                "note": "指标清单已声明，但没有任何一项与提问对得上"}
    best = scored[0][0]
    # 同名兄弟（总计 vs 分车型）容易并列，score 太接近就报歧义。
    near = [x for x in scored if best - x[0] < 0.05]
    # 限定词覆盖检验：提问里的词元，最匹配那一列是否也都有？
    # 「春天粮食产量」在「夏收粮食产量／秋粮产量」这张表上，2-gram 只共享
    # 「粮食产量」这个通用部分，而「春天」在任何指标名里都不存在——只共享
    # 通用词就报 covered，等于把「表里有粮食产量」说成「表里有春天的」。
    # 这类误报和地理轴的「上海浦东判 covered」是同一种错：拿粗的命中冒充
    # 细的答案。所以只共享通用词时降为 unknown，并把没落地的词交出去。
    # 覆盖检验看的是**候选集**，不是第一名的单条。只比 top1 会误伤：
    # 「行人交通事故死亡人数」在表里确有对应列（行人乘车人…），但按重叠
    # 比例它排第 2，top1 是「其他交通事故死亡人数」——「行人」不在它的
    # 词元里，于是被判 unknown。只要交出去的前几名里有一列能接住，就不该
    # 说「表里没有这个词」。
    top_cores = [cores[id(_it)][0] for _sc, _it in scored[:5]]
    top_union: set[str] = set().union(*top_cores) if top_cores else set()
    uncovered = []
    for g in (qg - abbr_grams):
        if len(g) < 2 or g in top_union:
            continue
        # 跨词边界的 2-gram 碎片：「交通死亡人数」会切出「通死」，它在任何
        # 指标名里都不会出现，但它不代表用户要求了别的东西——它的每个字
        # 都已落在**同一列**的字符集里。真正的限定词（春天／增速／火车）
        # 不满足：它们至少带一个候选列里根本没有的字。
        # 注意是「同一列同时含这些字」，不是「全表字集合含这些字」——
        # 后者会让「火车」借「汽车」的「车」蒙混过关。
        if any(set(g) <= set("".join(c)) for c in top_cores):
            continue
        uncovered.append(g)
    if uncovered:
        return {"matched": top, "state": UNKNOWN, "basis": "indicator_list",
                "uncovered_terms": sorted(uncovered)[:6],
                "note": ("提问里的「%s」未出现在本表任何指标名中；最接近的是「%s」，"
                         "但口径可能不同，需自验后再取"
                         % ("、".join(sorted(uncovered)[:3]),
                            top[0].get("name") if top else ""))}
    return {"matched": top, "state": COVERED, "basis": "indicator_list",
            "ambiguous": len(near) > 1,
            "note": ("有多项分数接近，需按单位或表内其它列消歧"
                     if len(near) > 1 else "")}


def _time_state(axis: dict, year_from, year_to) -> dict:
    rng = axis.get("range") or {}
    basis = str(axis.get("basis") or "")
    low, high = rng.get("from"), rng.get("to")
    ongoing = bool(rng.get("ongoing"))
    want_lo = year_from if isinstance(year_from, int) else None
    want_hi = year_to if isinstance(year_to, int) else want_lo
    # 探针级证据（0.42）：这几年我们**真的取到过数值**。它不代表区间，只代表
    # 「这一年确实有」。有它就不该再答「源方未声明」——我们自己的实测也是
    # 证据，只是口径更窄：只认问单一年份、且那一年在实测名单里的情形。
    # 反过来，名单外的年份一律仍判 unknown，绝不因为「只见到 2024」就断言
    # 2019 没有——那正是把假阴性洗成结论的经典错法。
    seen = {int(y) for y in (axis.get("observed") or []) if str(y).isdigit()}
    if want_lo is not None and want_lo == want_hi and want_lo in seen:
        return {"state": COVERED, "basis": "platform_probe",
                "note": "%s 年实测取到数值（只证明该年有，不证明区间）" % want_lo}
    if low is None and high is None and not ongoing:
        return {"state": UNKNOWN, "basis": basis or "",
                "note": axis.get("note") or "源方未声明时间范围"}
    if want_lo is None:
        return {"state": UNKNOWN, "basis": basis,
                "note": "提问未给年份，时间轴无需判定"}
    # 只有声明级证据才允许判 absent；推断出来的区间一旦判死就关掉了真路。
    can_deny = basis in DECLARED_BASES
    if want_hi is None:
        want_hi = want_lo
    if low is not None and want_hi < low:
        return {"state": ABSENT if can_deny else UNKNOWN, "basis": basis,
                "note": "提问年份早于该表起点 %s" % low}
    if high is not None and want_lo > high:
        return {"state": ABSENT if can_deny else UNKNOWN, "basis": basis,
                "note": "提问年份晚于该表终点 %s" % high}
    if ongoing and high is None:
        if low is not None and want_lo >= low:
            return {"state": COVERED, "basis": basis,
                    "note": "该表仍在更新（至 %s 起）" % low}
    if low is not None and high is not None and low <= want_lo and want_hi <= high:
        return {"state": COVERED, "basis": basis,
                "note": "提问年份落在 %s-%s" % (low, high)}
    return {"state": UNKNOWN, "basis": basis,
            "note": "区间不足以判定（部分重叠或边界不清）"}


# 省级行政区简称（23 省 + 5 自治区 + 4 直辖市，港澳台另判）。
# 0.43 之前这里只硬编码了 4 个省名，`上海`／`江苏` 这种最自然的说法会
# 返回空层级，geo 轴直接判不了——「2024 上海 GDP」就卡在这一步。
# 直辖市（北京/上海/天津/重庆）在行政层级上等同省级，必须先于「市」字
# 后缀规则判定，否则「上海市」会被判成 city，永远对不上分省表。
PROVINCES = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
)


# 地理层级的**序**。这是本轮最关键的一处：0.43 之前层级只是一个标签，
# 没有大小关系，于是「上海浦东新区」命中「上海」就被判成 province，
# 「表也只到省」两边相等 → covered。提问比表更细，却回答「有这一格」，
# 是会让 agent 白跑一趟的最坏一种错。
# 有了序，就能表达「问得比表细」这个方向上的一律降级。
GEO_RANK = {
    "": 0, "national": 1, "region": 2,
    "province": 3, "city": 4, "county": 5,
    "hk": 3, "mo": 3, "tw": 3,      # 特别行政区/地区按省级比较
}

# 比省更细的行政单元后缀。「自治区」含「区」，必须在判定前先剔除，
# 否则「内蒙古自治区」会被当成县级。
SUBPROVINCE_MARKS = (
    "新区", "开发区", "高新区", "经开区", "试验区", "保税区", "工业园",
    "街道", "镇", "乡", "区", "县",
)

# 跨省的大区/经济区。它们不是行政层级，和「分省」表不可直接对齐：
# 一张分省表通常可以用若干省**聚合**出东部，但表本身只给省，聚合规则
# 由提问方决定。所以只能 unknown，不能装成 covered。
MACRO_REGIONS = (
    "东部", "西部", "中部", "东北", "南方", "北方", "沿海", "内陆",
    "长三角", "珠三角", "京津冀", "长江经济带", "粤港澳", "成渝",
)


# 指标词表：剥地名时要把「上海GDP」里的 GDP 剥掉，否则会被当成剩余地名，
# 把「查省级 GDP」误降成「查区县」。这是一张**封闭**的小表——它不会随
# 用户提问增长，因为指标词是国家统计口径的固定词汇，不是开放集合。
_IND_WORDS = (
    "人均地区生产总值", "地区生产总值", "人均国内生产总值", "国内生产总值",
    "gdp", "生产总值", "增加值", "人口", "产量", "产值", "收入", "支出",
    "面积", "数量", "指数", "总额", "增速", "增长率", "就业", "失业",
    "价格", "消费", "投资", "进出口", "财政", "粮食", "能耗", "用水",
)
# 长词先剥，否则「地区生产总值」会被「生产总值」剥剩一个「地区」，
# 而「地区」里的「区」正是 SUBPROVINCE_MARKS 的触发词——一次剥词顺序
# 错误就能把「上海地区生产总值」判成区县级提问。
_IND_WORDS_ORDERED = tuple(sorted(_IND_WORDS, key=len, reverse=True))

# 国家层面的词。「中国交通死亡人数」里的「中国」归 geo 轴，不归指标轴；
# 不剥它，2-gram 会产生「国交」这种跨词边界的碎片，把指标轴判成 unknown。
_NATION_WORDS = ("中国", "全国", "国家", "我国", "大陆", "国内", "我国境内")

# 相对年份。`去年` 不剥，就会剥剩下「去」，被当成地名。
_REL_YEARS = ("去年", "今年", "明年", "前年", "上年", "下年", "本年", "当年",
              "全年", "年度", "年份", "季度", "月份")


def _strip_ind_words(s: str) -> str:
    """剥掉时间词与指标词，只留地名部分。"""
    for w in _REL_YEARS:
        s = s.replace(w, "")
    for w in _IND_WORDS_ORDERED:
        s = s.replace(w, "")
    return s

# 行政区划的通用后缀（剥掉后剩下的才是真正的「地名」）。
_ADMIN_SUFFIX = (
    "特别行政区", "自治区", "自治州", "省", "市", "县", "区",
    "壮族", "回族", "维吾尔", "自治",
)


def _residual_place(want: str) -> str:
    """剥掉省名、行政区后缀、指标词后，还剩下什么。

    这是为了回答一个**不能靠枚举**的问题：提问比省更细吗？
    全国有 2800+ 区县、300+ 地级市、无数开发区，写死名单永远追不上。
    但「上海市浦东」和「上海」的差别不在名单里，在**结构**里：前者剥完
    还剩「浦东」，后者剥完什么也不剩。用剩余量判断，就不需要认识任何一个
    具体地名——这是把「无限取值」换成「有限规则」的那一步。
    """
    rest = want.lower()
    # 时间词先剥：不剥的话「2024年上海GDP」会剥剩一个「2024年」，
    # len>=2 被当成地名，把省级提问误降成区县——过度修正和漏判一样是错。
    rest = re.sub(r"\d{4}\s*年度?", "", rest)
    rest = re.sub(r"\d+\s*(年|季度|月|季)", "", rest)
    rest = re.sub(r"\d+", "", rest)
    rest = _strip_ind_words(rest)
    for p in PROVINCES:
        rest = rest.replace(p, "")
    for s in _ADMIN_SUFFIX:
        rest = rest.replace(s, "")
    return rest.strip(" 　·-—、,，.。()（）")


def _want_level(want: str) -> str:
    """提问里的地理层级，取**最细**的那一层。

    港澳台必须先判：`中国香港` 含「中国」，先判全国就会把香港当成中国
    内地——这正是 0.38 香港 3,885 条被误判成平台条目、以及「香港车祸」
    被指去全国表的同一个坑。

    判序按「细 → 粗」，因为细的包含粗的（上海市浦东新区 ⊃ 上海）：
      港澳台 → 跨省大区 → 区县 → 省 → 市 → 全国
    直辖市（北京/上海/天津/重庆）在统计口径上等同省级，所以「上海市」
    必须先命中省名，不能落到「市」字规则上——那个坑 0.43 已踩过。
    """
    if any(k in want for k in ("香港", "hk", "hong kong")):
        return "hk"
    if any(k in want for k in ("澳门", "macao", "macau")):
        return "mo"
    if any(k in want for k in ("台湾", "taiwan")):
        return "tw"
    # 跨省大区先于省名：'长三角' 不含省名，但 '京津冀' 含 '冀' 之外的也
    # 可能带省名（如「东部各省」），所以放在省名之前更稳。
    if any(k in want for k in MACRO_REGIONS):
        return "region"
    # 区县：先剥「自治区」再剥指标词，最后找区县后缀。
    # 「上海2024年地区生产总值」若直接找「区」，会命中「地区」里的那个字，
    # 把省级提问误降成县级——所以剥词必须在找后缀之前。
    stripped = _strip_ind_words(want.replace("自治区", ""))
    stripped = re.sub(r"\d+", "", stripped)
    if any(k in stripped for k in SUBPROVINCE_MARKS):
        return "county"
    has_prov = any(p in want for p in PROVINCES)
    if has_prov:
        # 含省名还不够：还要看剥完省名之后剩不剩地名。「江苏昆山市」
        # 剩「昆山」→ 县级；「上海市」只剩后缀 → 省级。这一条替代了
        # 「再列一份城市名单」的做法。
        if len(_residual_place(want)) >= 2:
            return "county"
        return "province"
    if any(k in want for k in ("省", "自治区")):
        return "province"
    if "市" in want:
        return "city"
    if any(k in want for k in ("全国", "中国", "国家", "大陆")):
        return "national"
    return ""


def want_level(want: str) -> str:
    """公开入口：geo 字面 → 层级码。

    map.resolve_need 用它给 agent 回一个「你的 geo_level 和字面不一致」的
    提示；_geo_state 用它做兜底。调用方只该在 need 里没给 geo_level 时用到它
    ——层级本来该由 agent 给，这里是推断，不是判定。
    """
    return _want_level(want)


def _geo_state(axis: dict, need: dict) -> dict:
    want = str((need or {}).get("geo") or "").strip()
    have = str(axis.get("geo") or axis.get("state") or "").strip()
    basis = str(axis.get("basis") or "")
    if not have:
        return {"state": UNKNOWN, "basis": basis,
                "note": axis.get("note") or "源方未声明地理范围"}
    if not want:
        return {"state": UNKNOWN, "basis": basis,
                "note": "提问未给地理范围，无需判定"}
    # 0.44：层级优先取 agent 投递的 geo_level（枚举，可校验）；只有没给时
    # 才从字面反推。反推是启发式——「浦东新区」能判对，「张江」就不能，
    # 而 agent 知道张江是浦东的一个片区。把这一步留在我们这边，等于把
    # 「无限的地名」重新塞回有限规则里。
    want_level = (str((need or {}).get("geo_level") or "").strip().lower()
                  or _want_level(want))
    if want_level not in GEO_RANK:
        want_level = _want_level(want)
    rank_w = GEO_RANK.get(want_level, 0)
    rank_h = GEO_RANK.get(have, 0)
    # 问得比表更细（省表 vs 区县提问）：这张表的行只到 have 这一级，往下
    # 根本没有行。0.43 之前这里会落进 have == want_level 之外、又没被任何
    # 分支接住，最终答「层级不一致」——但「上海浦东新区」当时因为命中
    # 「上海」被 _want_level 判成 province，两边相等，直接 covered。
    # 现在有了层级序，「上海市浦东新区」(county) > province，一律降下来。
    # 不判 absent：源站别处是否有区县数据不是这张表能说的，我们只知道自己
    # 这张表里没有。out_of_scope 让调用方知道「这条路基本不通」。
    if rank_w and rank_h and rank_w > rank_h:
        return {"state": UNKNOWN, "basis": "tree_scope", "out_of_scope": True,
                "note": ("该表只到「%s」层，提问要到「%s」层——本表不提供更细一级的行。"
                         "实测该源检索（search=上海市浦东新区）只回 da=省级码，"
                         "未见区县级行；如需区县级，请换源或改问省级"
                         % (have, want_level))}
    # 跨省大区（东部/长三角）：分省表可以用若干省聚合出来，但聚合规则由
    # 提问方定，表本身只声明到省。说 covered 就是把「能凑」说成「有」。
    if want_level == "region":
        return {"state": UNKNOWN, "basis": "tree_scope",
                "note": ("提问是跨省大区（%s），该表只声明到省级。可用表中若干省"
                         "自行聚合，但聚合口径由提问方决定，平台不代为认定" % want)}
    if have == want_level:
        # 分省库里也有一批只覆盖局部地区的表（民族自治地方、沿边城市…）。
        # 若档案里带了省名清单，就按清单核对，不再只比层级。
        listed = [str(x) for x in (axis.get("provinces") or []) if x]
        if have == "province" and listed and want_level == "province":
            hits = [p for p in listed if p and p in want]
            if not hits:
                return {"state": ABSENT, "basis": "source_declared",
                        "note": ("该表省名清单不含「%s」；清单由源方接口返回"
                                 % want)}
            return {"state": COVERED, "basis": "source_declared",
                    "note": "省名清单含「%s」" % hits[0]}
        # 实测取到过这个地区（响应里出现过对应 da）= 探针级证据，比树路径推断硬。
        # 只有被看到的那几个省升级，其余仍按树路径推断——看到上海不等于看到江苏。
        seen_geo = [str(x) for x in (axis.get("observed_provinces") or []) if x]
        if have == "province" and want_level == "province" and seen_geo:
            hit = [p for p in seen_geo if p in want]
            if hit:
                return {"state": COVERED, "basis": "platform_probe",
                        "note": "实测响应里出现过「%s」的行（da 地区码匹配）" % hit[0]}
        return {"state": COVERED, "basis": basis,
                "note": "地理层级一致（%s）" % have}
    if have == "national" and want_level in ("hk", "mo", "tw"):
        return {"state": UNKNOWN, "basis": basis,
                "note": ("该表是全国库，源方未声明是否含港澳台；"
                         "国家统计局年度数据通常不含，需按源方口径确认")}
    if have == "province" and want_level == "national":
        return {"state": UNKNOWN, "basis": basis,
                "note": "该表是分省粒度，通常含全国合计列但未声明，需逐表核对"}
    if have == "national" and want_level in ("province", "city"):
        return {"state": UNKNOWN, "basis": basis,
                "note": "该表是全国总量，未声明是否含分省/分市明细"}
    return {"state": UNKNOWN, "basis": basis,
            "note": "地理层级不一致或无法对齐（表=%s，问=%s）" % (have, want_level or "?")}


def coverage(loc: dict, need: dict | None = None,
             query: str = "", strict: bool = False) -> dict:
    """逐轴三态判定。strict=True 时只认声明级证据，推断一律降为 unknown。"""
    loc = loc or {}
    need = need or {}
    axes = loc.get("axes") or {}
    out: dict = {"version": VERSION, "axes": {},
                 "boundary": ("只判「这一堆里有没有这一格」，不取数、不承诺数值；"
                              "unknown 表示源方没声明，不代表没有")}

    ind = match_indicators(loc, query, need)
    if strict and ind.get("basis") not in DECLARED_BASES:
        ind = dict(ind, state=UNKNOWN)
    out["axes"]["indicator"] = ind
    out["axes"]["geo"] = _geo_state(axes.get("geo") or {}, need)
    out["axes"]["time"] = _time_state(axes.get("time") or {},
                                      need.get("year_from"), need.get("year_to"))
    gran = axes.get("granularity") or {}
    want_g = (need.get("granularity") or {})
    if isinstance(want_g, dict):
        want_g = want_g.get("temporal") or want_g.get("level") or ""
    if not gran.get("value"):
        out["axes"]["granularity"] = {
            "state": UNKNOWN, "basis": "", "note": "源方未声明粒度"}
    elif want_g and str(want_g).lower() not in str(gran.get("value")).lower():
        out["axes"]["granularity"] = {
            "state": UNKNOWN, "basis": gran.get("basis") or "",
            "note": "粒度声明「%s」与提问「%s」无法对齐" % (gran.get("value"), want_g)}
    else:
        out["axes"]["granularity"] = {
            "state": COVERED, "basis": gran.get("basis") or "",
            "note": "粒度一致（%s）" % gran.get("value")}

    if strict:
        for k, v in out["axes"].items():
            if v.get("basis") and v["basis"] not in DECLARED_BASES:
                out["axes"][k] = dict(v, state=UNKNOWN,
                                      note="推断证据（%s），strict 下不作判定" % v["basis"])

    states = [v["state"] for v in out["axes"].values()]
    yes = states.count(COVERED)
    no = states.count(ABSENT)
    axe = out["axes"]
    # 「有你要的那一格」不能只看覆盖了几条轴：时间是「去年 / 某年」这类
    # 提问的关键轴，它落在 unknown 时，指标和地理再怎么样也不能替它背书。
    #
    # 0.44 把 geo 也纳进来，但**只在提问真的给了地理时**：不提问地理的
    # 通用检索（「交通事故死亡人数」）本来就不该被地理轴卡住。之前的
    # 「东 部地区GDP」就是反例——地理明明判不出来（跨省大区），verdict
    # 却因为只看 indicator+time 而报了 has_cell，等于说「这一格在里面」。
    _must = ["indicator", "time"]
    if str((need or {}).get("geo") or "").strip():
        _must.append("geo")
    blocking = [k for k in _must if axe[k]["state"] != COVERED]
    if no:
        verdict = "no_cell"
    elif yes == 0:
        verdict = "unverifiable"
    elif blocking:
        verdict = "partial"
    else:
        verdict = "has_cell"
    out["summary"] = {
        "covered": yes, "absent": no,
        "unknown": states.count(UNKNOWN), "axes": len(states),
        "verdict": verdict,
        "blocking_axes": blocking,
    }
    out["verdict_note"] = {
        "has_cell": "指标与时间都明确包含，这一堆里应当有你要的那一格",
        "no_cell": "有轴明确不含：这一堆里没有你要的那一格，别当家答案来源",
        "partial": ("可给路牌，但必须把没验的那几轴一起交出去（%s 未确认）"
                    % "、".join(blocking)),
        "unverifiable": "全部落在 unknown：源方没声明，只能说「可能有」，不能说「有」",
    }[verdict]
    return out


def _recipe_hint(req: dict) -> str:
    """把 request 块压成一句话，供卡片用。

    为什么要这一句：定位器如果只说「已验证 true」，agent 仍然不知道进门之后
    该敲哪个地址、拿哪个字段。指路层不把路说清楚，就等于没指。
    """
    if not (req or {}).get("endpoint"):
        return ""
    parts = ["%s %s" % ((req.get("method") or "GET").upper(), req["endpoint"])]
    keys = [k for k in (req.get("cell_key") or []) if k]
    if keys:
        parts.append("→ 按 %s 过滤取 %s"
                     % ("+".join(keys), req.get("value_field") or "value"))
    if (req.get("verified_scope") or "") == "latest_period_only":
        parts.append("（仅最新一期）")
    return " ".join(parts)


def describe(loc: dict) -> dict:
    """定位器的一句话摘要，给候选列表和卡片用（不展开全量指标）。"""
    loc = loc or {}
    inds = [i for i in (loc.get("indicators") or []) if isinstance(i, dict)]
    axes = loc.get("axes") or {}
    t = axes.get("time") or {}
    rng = t.get("range") or {}
    req = loc.get("request") or {}
    span = ""
    if rng.get("from") or rng.get("to"):
        span = "%s-%s" % (rng.get("from") or "?", "至今" if rng.get("ongoing")
                          else (rng.get("to") or "?"))
    return {
        "version": VERSION,
        "table_id": loc.get("table_id") or "",
        "indicator_count": len(inds),
        "indicator_sample": [i.get("name") for i in inds[:5]],
        "time_span": span,
        "time_basis": t.get("basis") or "",
        # 区间未声明时，这一行是唯一能回答「去年那格到底有没有」的东西：
        # 实测取到过的年份。空数组不外露，免得空字段变成噪音。
        **({"time_observed": [int(y) for y in (t.get("observed") or [])
                              if str(y).isdigit()]} if t.get("observed") else {}),
        "request_verified": bool(req.get("verified")),
        # 验到什么程度：全量还是只验了最新一期。只给 bool 会把
        # 「最新一格能取」误读成「二十年序列都能取」。
        "request_scope": req.get("verified_scope") or "",
        "request_hint": _recipe_hint(req),
        "note": ("表内 %d 个指标；时间范围%s" % (
            len(inds), ("由表名推断 " + span) if span else "未声明")),
    }


def clear_cache() -> None:  # 与其它模块保持同一套测试钩子
    return None
