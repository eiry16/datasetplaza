"""数据集能力声明（capability）：回答 agent 的两个问题。

    「你能干什么？」   -> ``asks``：能直接回答的问题句
    「你手上有什么？」 -> ``entities`` × ``measures`` × ``sectors`` × ``geo``

为什么非有不可
--------------
路由的输入是 **agent 的需求**，不是数据集的名称。只做名称字面匹配时，
路牌只能回答「你的词有没有出现在我名字里」——用户问「苏州哪家面馆能开发
票」，货架上一条都匹配不到，即使它明明有一份全国餐饮门店名录：那份数据的
名字里不会出现「发票」两个字。

于是有了两套坐标：

    需求侧  parse_need(提问)   -> (主体, 度量, 行业, 地理)
    供给侧  declare(数据集)    -> (主体, 度量, 行业, 地理)

两边对上了才叫「这条路能走」。对不上但行业对得上，叫**部分满足**，要如实
说出来（有门店名录、没有评价）。四条都对不上，叫**缺口**，要明确告诉
agent「货架上没有这类数据，别在这儿找」——这比返回一个 0 分候选有用得多。

证据三档（``basis``），不许混淆
------------------------------
``declared``  发布方自己说了（BeaconMFG 的 coverage 声明、源方字段清单）
``derived``   我们按名称/主题/发布方**确定性**推出，可复现、可复核
``unknown``   推不出来，就留空

``derived`` 不是猜测的同义词，但也不是事实。它必须能被一句话复核：
``basis_note`` 里写清「哪几个词命中了哪条规则」。凡是写成 ``derived`` 的
能力，agent 都该在交付时标注「按目录元数据推断，取数前请回源核对」。

刻意不做的事
------------
不推断字段名。238 条目录条目里有 235 条 ``fields`` 为空，它们的字段字典
我们**确实不知道**。编一份出来会让 agent 兴冲冲跑过去、打开发现对不上，
那比说「不知道」坏得多。度量（measures）是「这类数据通常能给什么」的
粗粒度声明，措辞上必须留出余地。
"""
from __future__ import annotations

import re

from . import sectors as sectormod
from . import taxonomy as tx
from . import map as mapmod

# 规则版本。落盘的能力声明带着它；规则改了没重跑脚本，读取侧能发现声明
# 是旧的。不做自动重算——运营手工改过的说明不该被下次启动抹掉。
RULE_VERSION = "cap-18"   # 0.39：名字里明写的比率/指数补进 measures
# 0.29 补轴：主题轴可以由分类树推断补齐，但推断 ≠ 实测。补进来的轴必须带
# 来源标记，match() 据此降权；axis_version 让「哪批卡做过补轴」可审。
# 0.39 起多一档 source_mapped（源方分类翻译），卡上必须带 axis-2。
AXIS_VERSION = "axis-2"

# ── 主体：一条记录代表什么 ────────────────────────────────────────────
# 顺序即优先级：一条数据同时像「企业」和「统计」，先归企业——问企业的人和
# 问统计的人要的东西不一样，前者要一条条主体，后者要一个数。
#
# 英文词一律加 \b：子串匹配在这个语料上会稳定出错——``rating`` 命中
# ``operating``（「运营场址」被判成有用户评分），``count`` 命中 ``country``，
# ``rent`` 命中 ``different``。中文不加边界（没有词边界可用），所以中文词
# 必须写长：``户数`` 会命中 ``商户数据``。
ENTITY_RULES: list[tuple[str, str, str]] = [
    ("company", "企业",
     r"企业|公司|厂商|工商|法人|经营主体|注册登记|企业名录|"
     r"\benterprise|\bcompan|\bbusiness register|\bbusiness director|"
     r"\bfirms?\b|\bcorporat|\bestablishment"),
    ("store", "门店",
     # 「民宿/旅馆/酒店/宾馆/客栈」是住宿业态的门店（GB/T H 住宿和餐饮业
     # 下的 61 住宿业）：找民宿推荐就是在找门店，主体轴必须认出来。
     # 「户」绝不能单独进规则——会命中「商户数据」把企业名录判成住户。
     # 0.20：补单字「店」。中文里「X店」就是门店，X 是补不完的业态词
     # （咖啡店/花店/书店/药店/便利店…），但后缀「店」只有一个。列业态
     # 等于把词表往词典方向补，认后缀才是有限集合。带「馆/厅/吧」后缀的
      # 只列高频生活服务业态——单列「馆」会命中博物馆/图书馆/体育馆。
     r"店|咖啡馆|茶馆|酒馆|餐馆|饭馆|酒吧|门店|商户|店铺|网点|餐饮店|"
     r"商超|超市|便利店|加油站|4s店|"
     r"民宿|旅馆|酒店|宾馆|客栈|"
     r"\bstores?\b|\boutlets?\b|\bshops?\b|\bmerchant|\bpoi\b|"
     r"\bhotels?\b|\bhostels?\b|\bhomestay|\bb&b\b"),
    ("person", "个人",
     r"人口普查|居民|个人|患者|病人|"
     r"\bpersons?\b|\bindividual|\bpatients?\b|\bresidents?\b"),
    ("household", "住户",
     # 「户数」不能进规则：它会命中「商户数据」这类复合词，把一份企业名录
     # 判成住户数据。中文没有词边界，短词必须付代价地写长。
     r"住户|家庭收支|\bhouseholds?\b"),
    ("region", "地区",
     r"地区|省份|城市|区县|地级市|县域|人口|"
     r"\bprovince|\bcity\b|\bcities\b|\bregion|\bmunicipal|\bcounty\b|\bcounties\b|"
     r"\bpopulation\b|\bcensus\b|\bwards?\b"),
    ("country", "国家",
     r"国家|全球|世界|各国|国际比较|"
     r"\bcountr(?:y|ies)\b|\bworld\b|\bglobal\b|\bnations?\b"),
    ("event", "事件",
     r"事件|灾害|事故|病例|灾情|警情|"
     r"\bdisaster|\bevents?\b|\bincident|\baccident|\bhazard"),
    ("document", "文献",
     # 「政策」单字不能收：「货币政策」「利率政策」是金融主题，不是文献。
     # 只有「政策文件/政策文本/法规」这类才真的是一条条文档记录。
     r"文献|论文|专利|法规|政策文件|政策文本|政策库|标准文本|"
     r"\bpapers?\b|\bpatent|\bdocument|\blegislation\b|\bpublication"),
    ("corpus", "语料",
     r"语料|标注|对话|图像|语音|文本样本|语料库|高质量数据集|交互记录|多模态|"
     r"时空重建|监测文本|文本数据|"
     r"\bcorpus\b|\bannotation|\bdialogue|\bimage dataset|\bspeech\b|\bllm\b"),
    ("product", "商品",
     r"产品|商品|commodit|\bskus?\b|\bproduct"),
]

# ── 度量：这条数据能给出什么量 ────────────────────────────────────────
# 只登记「能拿到手的东西」，不登记「可能有关」。opinion 单列，是因为它最常
# 被误以为存在：企业名录里有店名地址，但没有一条点评。
MEASURE_RULES: list[tuple[str, str, str]] = [
    ("identity", "登记与资质",
     r"登记|注册|工商|法人|资质|许可|证照|营业执照|社会信用代码|"
     r"\bregistrat|\blicen|\buscc\b|\blegal name"),
    ("contact", "联系方式",
     r"联系|电话|邮箱|\bcontact|\bphones?\b|\bemails?\b|\btelephone"),
    ("location", "地址与位置",
     r"地址|坐标|经纬度|位置|地理位置|空间分布|"
     r"\baddress|\blocation|\bcoordinate|\bgeo\b|\bspatial\b|\bgis\b"),
    ("opinion", "口碑与评价",
     r"评价|评分|点评|口碑|评论|好评|差评|满意度|"
     r"\breviews?\b|\bratings?\b|\bsentiment\b|\bfeedback\b"),
    ("price", "价格",
     r"价格|房价|租金|物价|售价|报价|"
     r"\bprices?\b|\brent\b|\bcosts?\b|\btariff"),
    ("output", "产量与产值",
     r"产量|产能|产值|销售|营收|出货|"
     r"\boutput\b|\bproduction\b|\brevenue|\bsales\b"),
    ("labor", "就业与用工",
     r"就业|工资|薪酬|用工|招聘|从业人数|"
     r"\bemployment\b|\bwages?\b|\bsalar|\blabour\b|\blabor\b|\bjobs?\b|\bskills?\b"),
    ("finance", "财务与税收",
     r"财务|资产|利润|税收|纳税|负债|援助|资助|补贴|拨款|"
     r"\bfinanc|\btax(?:es)?\b|\bbalance sheet"),
    ("environment", "环境指标",
     r"空气|水质|排放|污染|碳排放|生态|"
     r"\bair quality\b|\bemission|\bpollution\b|\bcarbon\b|\bwater quality\b|"
     r"\benvironmental\b"),
    ("health", "疾病与诊疗",
     r"疾病|诊断|治疗|预后|复发|发病率|专病|死亡率|死亡人数|癌|肿瘤|"
     r"\bhealth\b|\bhealthcare\b|\bdisease|\bdiagnos|\btreatment|\bprognosis\b|"
     r"\bincidence\b|\bmortality\b|\bcancer\b|\btumor\b|\btumour\b"),
    ("count", "数量统计",
     r"数量|条数|统计|规模|占比|"
     # headcount 是复合词，\bcount\b 匹配不到它；而「贫困人口计数」这类
     # 指标恰恰只有这一个词面能说明它是计数。
     r"\bcounts?\b|\bheadcounts?\b|\bnumber of\b|\bshare of\b|\bstatistic"),
    ("series", "时间序列",
     r"时间序列|年度|月度|季度|时序|逐年|"
     r"\btime series\b|\bannual\b|\bmonthly\b|\bquarterly\b|\bpanel\b"),
    # 经营比率：入住率/出租率/空置率这类指标既不是产量也不是计数，
    # 单独成轴。不收裸「率」——概率/效率/汇率都会被误伤；只认明确的
    # 经营比率词（0.19 实测：酒店入住率被商户名录判成完全满足，
    # 因为「入住率」根本没解析出度量）。
    ("rate", "比率与指数",
     r"入住率|出租率|空置率|上座率|使用率|利用率|"
     r"\boccupancy\b|\butili[sz]ation rate\b|\bvacancy rate\b"),
]

# 0.39 名字度量（§7c）：香港《酒店房间入住率》、世行 `Tax revenue (% of GDP)`
# 名字里明写着「率 / %」，卡上 ``measures`` 却是空的——于是 match() 一边因
# 名称命中给 ``strong``，一边在 ``missing`` 里如实写「缺比率」，缺口与候选
# 自相矛盾。这里**只扫 name**，不扫 domain/描述：源方库名里的 "Statistics"
# 不能当成度量。补出来的轴带 ``measures_basis=name``，源方字段字典一到就能
# 推翻它（``_apply_evidence`` 的实测证据仍优先）。
# 刻意不收 ``per capita``：那是通用修饰语而非度量，收进来会让 3 万条世行
# per-capita 系列靠一个词刷屏（0.38 §9 人均寿命被挤掉就是这么来的）。
_NAME_MEASURE_RULES: list[tuple[str, str, str]] = [
    ("rate", "比率与指数",
     r"入住率|出租率|空置率|上座率|使用率|利用率|比率|占比|百分比|指数|"
     r"\brate\b|\bratio\b|\bpercentage\b|\bpercent\b|\bindex\b|\bindices\b|"
     r"\(%|\b% of\b|%"),
    ("count", "数量统计", r"\bheadcounts?\b|\bcounts?\b|\bnumber of\b|条数|数量"),
]

# 污水治理语境：``treatment`` 在这里是「处理工艺」，不是「诊疗」。
# 0.14 实测「France wastewater treatment plants discharge data」被判成
# measures=['health']，把整页候选带去医疗数据。这张表只用于**需求侧**的
# 上下文排除，供给侧字段推断不受影响；法语词是法国官方数据集的真实用语。
# 0.18 起污水词面住进分级地图（bdsp.map），这里只引入引用——需求归类的
# 词面全部绑定地图节点，不在业务代码里增长。
from .map import WASTEWATER_CONTEXT  # noqa: E402

# ── 地名：词面 -> 规范名。只登记目录里真实出现过的，不做全量世界地图 ──
# 实测之后这张表变重要了：以前只用它判「数据集的名称/标签提到哪个国家」，
# 十几个词就够；现在还要判「某一列的值是哪一国的」。OWID 的 Entity 列里
# 有 109 个国名，只认识 26 个就会把一份全球面板判成「没有地理维度」。
PLACES: dict[str, str] = {
    "united kingdom": "英国", "uk": "英国", "england": "英国", "scotland": "英国",
    "wales": "英国", "london": "英国",
    "ireland": "爱尔兰", "belgium": "比利时", "spain": "西班牙", "france": "法国",
    "italy": "意大利", "greece": "希腊", "ukraine": "乌克兰", "germany": "德国",
    "lithuania": "立陶宛", "netherlands": "荷兰", "poland": "波兰",
    "luxembourg": "卢森堡", "austria": "奥地利", "slovenia": "斯洛文尼亚",
    "latvia": "拉脱维亚", "portugal": "葡萄牙", "czechia": "捷克",
    "czech": "捷克", "switzerland": "瑞士", "europe": "欧洲", "eu": "欧盟",
    "china": "中国", "united states": "美国", "usa": "美国", "japan": "日本",
    "korea": "韩国", "india": "印度", "brazil": "巴西", "mexico": "墨西哥",
    # 以下按实测语料补：值里真实出现过的国家名，缺一个就会漏判一片
    "afghanistan": "阿富汗", "albania": "阿尔巴尼亚", "algeria": "阿尔及利亚",
    "angola": "安哥拉", "argentina": "阿根廷", "armenia": "亚美尼亚",
    "australia": "澳大利亚", "azerbaijan": "阿塞拜疆", "bangladesh": "孟加拉国",
    "belarus": "白俄罗斯", "bolivia": "玻利维亚", "bosnia": "波黑",
    "bulgaria": "保加利亚", "cambodia": "柬埔寨", "cameroon": "喀麦隆",
    "canada": "加拿大", "chile": "智利", "colombia": "哥伦比亚",
    "costa rica": "哥斯达黎加", "croatia": "克罗地亚", "cuba": "古巴",
    "cyprus": "塞浦路斯", "denmark": "丹麦", "ecuador": "厄瓜多尔",
    "egypt": "埃及", "estonia": "爱沙尼亚", "ethiopia": "埃塞俄比亚",
    "finland": "芬兰", "georgia": "格鲁吉亚", "ghana": "加纳",
    "hungary": "匈牙利", "iceland": "冰岛", "indonesia": "印度尼西亚",
    "iran": "伊朗", "iraq": "伊拉克", "israel": "以色列", "jordan": "约旦",
    "kazakhstan": "哈萨克斯坦", "kenya": "肯尼亚", "kuwait": "科威特",
    "kyrgyzstan": "吉尔吉斯斯坦", "malaysia": "马来西亚", "malta": "马耳他",
    "moldova": "摩尔多瓦", "mongolia": "蒙古", "montenegro": "黑山",
    "morocco": "摩洛哥", "mozambique": "莫桑比克", "myanmar": "缅甸",
    "nepal": "尼泊尔", "new zealand": "新西兰", "nigeria": "尼日利亚",
    "north macedonia": "北马其顿", "norway": "挪威", "pakistan": "巴基斯坦",
    "peru": "秘鲁", "philippines": "菲律宾", "qatar": "卡塔尔",
    "romania": "罗马尼亚", "russia": "俄罗斯", "saudi arabia": "沙特阿拉伯",
    "senegal": "塞内加尔", "serbia": "塞尔维亚", "singapore": "新加坡",
    "slovakia": "斯洛伐克", "south africa": "南非", "sri lanka": "斯里兰卡",
    "sweden": "瑞典", "syria": "叙利亚", "taiwan": "中国台湾",
    "tanzania": "坦桑尼亚", "thailand": "泰国", "tunisia": "突尼斯",
    "turkey": "土耳其", "uganda": "乌干达", "uruguay": "乌拉圭",
    "uzbekistan": "乌兹别克斯坦", "venezuela": "委内瑞拉", "vietnam": "越南",
    "yemen": "也门", "zambia": "赞比亚", "zimbabwe": "津巴布韦",
}
# 中文地名 -> 所属大区。判「声明覆盖中国」是否满足「问苏州」靠这张表；
# 没有它，任何中文城市名都无法与「中国」这个声明对上。
CITY_PARENT: dict[str, str] = {
    "中国": "中国", "全国": "中国",
    "北京": "中国", "上海": "中国", "天津": "中国", "重庆": "中国",
    "苏州": "中国", "南京": "中国", "无锡": "中国", "常州": "中国", "南通": "中国",
    "徐州": "中国", "扬州": "中国", "杭州": "中国", "宁波": "中国", "温州": "中国",
    "广州": "中国", "深圳": "中国", "东莞": "中国", "佛山": "中国", "珠海": "中国",
    "成都": "中国", "武汉": "中国", "长沙": "中国", "郑州": "中国", "西安": "中国",
    "济南": "中国", "青岛": "中国", "合肥": "中国", "福州": "中国", "厦门": "中国",
    "南昌": "中国", "昆明": "中国", "贵阳": "中国", "南宁": "中国", "海口": "中国",
    "沈阳": "中国", "大连": "中国", "长春": "中国", "哈尔滨": "中国",
    "石家庄": "中国", "太原": "中国", "呼和浩特": "中国", "兰州": "中国",
    "西宁": "中国", "银川": "中国", "乌鲁木齐": "中国", "拉萨": "中国",
    "江苏": "中国", "浙江": "中国", "广东": "中国", "山东": "中国", "河南": "中国",
    "四川": "中国", "湖北": "中国", "湖南": "中国", "福建": "中国", "安徽": "中国",
    "河北": "中国", "陕西": "中国", "山西": "中国", "辽宁": "中国", "吉林": "中国",
    "黑龙江": "中国", "江西": "中国", "云南": "中国", "贵州": "中国", "广西": "中国",
    "海南": "中国", "甘肃": "中国", "青海": "中国", "宁夏": "中国", "新疆": "中国",
    "西藏": "中国", "内蒙古": "中国",
}

# 街道级地名的**形态**规则：中文路名以这些后缀收尾。这不是地名表——
# 路名永远补不完，后缀只有几个。解析结果只作提示（need.geo_detail），
# 不参与匹配：目前没有任何一条声明能按街道定位。
_GEO_STREET = re.compile(r"[\u4e00-\u9fff]{2,4}(?:路|街|大道|大街|弄|巷|广场|商圈)")
_GEO_FILLER = re.compile(r"^(?:帮我|给我|我想|我要|麻烦|请|想|要|找找|找|查查|查|"
                         r"看看|看|附近的|附近|位于|在|去|的|推荐|[省市县区])")

# ── 主题词 -> GB/T 门类。英文目录条目没有中文口语词，靠这张表进行业轴 ──
# 匹配规则（见 ``_keyword_hit``）：带 ``*`` 的是词干，按前缀包含匹配；
# 其余纯拉丁键按**整词**匹配。不加整词边界会稳定出错：``culture`` 命中
# ``agriculture``（西班牙地块数据被判成文化体育娱乐业），``care`` 命中
# ``healthcare``，``trade`` 命中 ``trademark``。
TOPIC_SECTOR: dict[str, tuple[str, ...]] = {
    "agricultur*": ("A",), "farm*": ("A",), "fisher*": ("A",),
    "forest*": ("A",), "livestock": ("A",), "crop*": ("A",),
    "mining": ("B",), "mineral*": ("B",),
    "manufactur*": ("C",), "industry": ("C",), "industrial": ("C",),
    "energy": ("D",), "electricity": ("D",), "gas supply": ("D",), "water supply": ("D",),
    "construction": ("E",), "building": ("E",),
    "retail*": ("F",), "wholesale": ("F",), "trade": ("F",), "commerce": ("F",),
    "commercial": ("F",), "shop*": ("F",), "business director*": ("F",),
    "transport*": ("G",), "traffic": ("G",), "logistic*": ("G",), "freight": ("G",),
    "mobility": ("G",),
    "restaurant*": ("H",), "catering": ("H",), "tourism": ("H",),
    "accommodation": ("H",), "hotel*": ("H",), "food service": ("H",),
    "software": ("I",), "ict": ("I",), "telecommunication*": ("I",),
    "internet": ("I",), "digital": ("I",), "information technology": ("I",),
    "financ*": ("J",), "banking": ("J",), "insurance": ("J",),
    "housing": ("K",), "property": ("K",), "real estate": ("K",),
    "business service*": ("L",), "consult*": ("L",), "advertis*": ("L",),
    "human resource*": ("L",), "employment service*": ("L",),
    "research": ("M",), "scientific": ("M",), "laborator*": ("M",),
    "patent*": ("M",),
    "environment*": ("N",), "air quality": ("N",), "pollution": ("N",),
    "emission*": ("N",), "water quality": ("N",), "waste": ("N",), "climate": ("N",),
    "lawn*": ("N",), "flower bed*": ("N",), "green space*": ("N",), "park*": ("N",),
    "education*": ("P",), "school*": ("P",), "training": ("P",), "literacy": ("P",),
    "health": ("Q",), "healthcare": ("Q",), "hospital*": ("Q",), "medical": ("Q",),
    "disease*": ("Q",), "cancer": ("Q",), "patient*": ("Q",), "care": ("Q",),
    "culture": ("R",), "sport*": ("R",), "entertainment": ("R",), "tourism site": ("R",),
    "poverty": ("S",), "social": ("S",), "welfare": ("S",), "pension*": ("S",),
    "crime": ("S",), "public administration": ("S",), "association": ("S",),
    "population": ("S",), "demographic*": ("S",), "census": ("S",),
    "macroeconomic*": ("T01",), "gdp": ("T01",), "inflation": ("T01",),
    "price index": ("T01",), "productivity": ("T01",),
    # 注意：不登记 "economy"/"economic"。欧洲城市开放数据里的 "Economy" 是
    # 一个**栏目名**（商业网点、企业名录、就业），不是宏观经济。把它映射到
    # 宏观经济与统计，会让「Namur - Economy - Shops by street」看起来像统计
    # 年鉴，agent 会按经济指标去用它，然后拿到一张门店清单。
    "气象": ("M",), "灾害": ("N",), "暴雨": ("M",), "灾情": ("N",),
    "医疗": ("Q",), "肿瘤": ("Q",), "诊疗": ("Q",), "专病": ("Q",), "预后": ("Q",),
    "餐饮": ("H",), "饭店": ("H",), "住宿": ("H",), "小吃": ("H",),
    "渔业": ("A",), "农业": ("A",), "工业": ("C",), "电机": ("C",), "装备制造": ("C",),
    "石化": ("C",), "化工": ("C",), "创新": ("M",),
    "金融": ("J",), "利率": ("J",), "货币政策": ("J",), "银行": ("J",),
    "贸易": ("F",), "进出口": ("F",), "企业": ("F",), "商户名录": ("F",),
    "供水": ("D",), "水质": ("N",), "时序监测": ("T01",), "宏观经济": ("T01",),
    "国际统计": ("T01",), "统计学": ("T01",),
    # 癌族 → T06 卫生：货架上真有中文肿瘤专病数据集（卵巢癌/肝癌切除复发
    # 预测等），0.17 实测它们因卡上无主题轴被记缺「主题」。单字「癌」中文
    # 按子串匹配，收所有病种名；拉丁词按整词。署名判决（CURATED）优先，
    # 这里只是兜底。
    "癌": ("T06",), "肿瘤": ("T06",),
    "cancer": ("T06",), "tumor": ("T06",), "tumour": ("T06",),
}

_ASCII_KEY = re.compile(r"^[a-z0-9 .&+-]+$")


def _keyword_hit(text: str, key: str) -> bool:
    """主题词命中判定。``*`` 结尾按词干前缀匹配，纯拉丁键按整词匹配，
    中文键按子串（中文没有词边界，也没有更好的办法）。"""
    k = key.lower()
    if k.endswith("*"):
        return k[:-1] in text
    if _ASCII_KEY.match(k):
        return re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", text) is not None
    return k in text

# 源方字段名 -> 度量。只在 declared（发布方给了字段清单）时使用，
# 不做反推：字段清单本身就是证据，它比任何名称推断都硬。
FIELD_MEASURE: dict[str, str] = {
    "uscc": "identity", "legal_name": "identity", "registration": "identity",
    "reg_no": "identity", "credit_code": "identity", "status": "identity",
    "address": "location", "city": "location", "province": "location",
    "lat": "location", "lng": "location", "lon": "location", "geo": "location",
    "phone": "contact", "tel": "contact", "email": "contact", "contact": "contact",
    "price": "price", "amount": "price",
    "revenue": "output", "output": "output", "sales": "output",
    "employees": "labor", "employment": "labor", "salary": "labor",
    "date": "series", "year": "series", "month": "series", "time": "series",
    "rating": "opinion", "review": "opinion", "score": "opinion",
    "company": "identity",
}

_ENTITY_LABEL = {c: l for c, l, _r in ENTITY_RULES}
_MEASURE_LABEL = {c: l for c, l, _r in MEASURE_RULES}
# 附加信息度量：缺它不影响「这条路能不能接住主体」，只影响细节丰富度。
# 只收两个——口碑评价、联系方式，都是名录/目录类数据的常见缺口，且都
# 不是提问的交付主体（问「推荐几家民宿」要的是名单，不是评分）。
# 价格/产量/税收/时序绝不入此表：那些缺了就是答不了。
_ENHANCEMENT_MEASURES = frozenset({"opinion", "contact"})

# ── 实测字段名 -> 坐标 ────────────────────────────────────────────────
# 这与上面的 ENTITY_RULES 是两套东西，不能合并：
#   ENTITY_RULES  匹配**数据集名称与描述**，判断「这个名字像什么」
#   _F_ENTITY     匹配**真实的列名**，判断「这一行记录代表什么」
# 前者只能猜，后者是事实。列名按 token 精确匹配（``legal_name`` 拆成
# legal/name，``CO2 emissions per capita`` 拆成 co2/emissions/per/capita），
# 不做子串——子串匹配在列名上同样会出错（``country`` 含 ``count``）。
_F_ENTITY: dict[str, str] = {
    "company": "company", "enterprise": "company", "firm": "company",
    "corporation": "company", "corp": "company", "organization": "company",
    "organisation": "company", "employer": "company", "legalname": "company",
    "store": "store", "shop": "store", "outlet": "store", "poi": "store",
    "merchant": "store", "venue": "store",
    "person": "person", "patient": "person", "individual": "person",
    "citizen": "person", "resident": "person", "people": "person",
    "household": "household",
    "country": "country", "nation": "country", "iso3": "country",
    "iso2": "country",
    "city": "region", "town": "region", "ward": "region", "district": "region",
    "region": "region", "province": "region", "county": "region",
    "municipality": "region", "commune": "region", "borough": "region",
    "prefecture": "region", "village": "region", "state": "region",
}

_F_MEASURE: dict[str, str] = {
    "uscc": "identity", "creditcode": "identity", "legalname": "identity",
    "registration": "identity", "regno": "identity", "licence": "identity",
    "license": "identity", "permit": "identity", "status": "identity",
    "activestatus": "identity", "industrycode": "identity", "gb": "identity",
    "phone": "contact", "tel": "contact", "telephone": "contact",
    "mobile": "contact", "email": "contact", "mail": "contact",
    "contact": "contact", "fax": "contact", "website": "contact", "web": "contact",
    "address": "location", "addr": "location", "street": "location",
    "streetname": "location", "postcode": "location", "postal": "location",
    "zip": "location", "zipcode": "location", "lat": "location",
    "latitude": "location", "lon": "location", "lng": "location",
    "longitude": "location", "geo": "location", "geopoint": "location",
    "coord": "location", "coordinates": "location", "location": "location",
    "geom": "location", "point": "location",
    "rating": "opinion", "ratings": "opinion", "review": "opinion",
    "reviews": "opinion", "score": "opinion", "stars": "opinion",
    "comment": "opinion", "comments": "opinion", "feedback": "opinion",
    "satisfaction": "opinion", "sentiment": "opinion",
    "price": "price", "rent": "price", "cost": "price", "fee": "price",
    "tariff": "price", "fare": "price",
    "revenue": "output", "sales": "output", "output": "output",
    "production": "output", "turnover": "output", "volume": "output",
    "employees": "labor", "employment": "labor", "salary": "labor",
    "wage": "labor", "labour": "labor", "labor": "labor",
    "workforce": "labor", "staff": "labor",
    "tax": "finance", "debt": "finance", "assets": "finance",
    "profit": "finance", "income": "finance", "budget": "finance",
    "expenditure": "finance",
    "emission": "environment", "emissions": "environment", "co2": "environment",
    "pm25": "environment", "pm10": "environment", "no2": "environment",
    "aqi": "environment", "pollution": "environment", "carbon": "environment",
    "noise": "environment",
    "disease": "health", "diagnosis": "health", "mortality": "health",
    "morbidity": "health", "incidence": "health", "health": "health",
    "population": "count", "count": "count", "number": "count",
    "total": "count", "headcount": "count",
}

# 中文列名没有词边界，按子串认，所以只收不会误伤的整词。
_F_CN: dict[str, str] = {
    "企业": "company", "公司": "company", "商户": "store", "门店": "store",
    "店铺": "store", "网点": "store",
    "地址": "location", "坐标": "location", "经度": "location",
    "纬度": "location", "位置": "location",
    "电话": "contact", "手机": "contact", "邮箱": "contact", "联系人": "contact",
    "评分": "opinion", "评价": "opinion", "评论": "opinion", "口碑": "opinion",
    "价格": "price", "金额": "price", "产值": "output", "产量": "output",
    "营收": "output", "从业人数": "labor", "员工": "labor",
    "税收": "finance", "利润": "finance",
    "排放": "environment", "空气质量": "environment", "疾病": "health",
    "人口": "count", "数量": "count",
    "国家": "country", "城市": "region", "区县": "region", "省份": "region",
}

_CN_TIME = re.compile(r"年份|年度|日期|时间|月份|季度")
_TIME_FIELD = re.compile(
    r"^(year|date|time|period|month|quarter|week|day|obs_?time|"
    r"reference_?date|timestamp|年份|年度|日期|时间|月份|季度)$", re.I)
_GEO_FIELD = re.compile(
    r"countr|nation|\biso|city\b|town|ward|region|district|province|county|"
    r"municipal|borough|address|entity|commune|prefecture|\bstate\b|place|"
    r"latitude|longitude|\blat\b|\blon\b|\blng\b|\bgeo|\bcode\b|\biso", re.I)


def _tokens(name: str) -> list[str]:
    """列名拆词。``legal_name`` / ``legalName`` / ``CO2 emissions`` 都能拆。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    parts = re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", s)
    return [p.lower() for p in parts if p]


def _entities_from_values(fields: list[dict], samples: dict[str, list[str]]) -> list[str]:
    """列名认不出的主体，可以看值。OWID 的列叫 ``Entity``，值里是两百个国家
    ——列名不含 country，但这一列就是国家维度。

    两道门槛防止过度解读：列名得像地理维度，且不同值要够多（≥5）。一个叫
    ``Nationality of Ownership`` 的列里只有 Ireland/Other 两个值，它是口径
    维度，不是国家维度；把它当主体会让整张表的粒度谎报成「国家级」。
    """
    by_name = {str(f.get("name") or ""): f for f in fields}
    out: list[str] = []
    for col, vals in (samples or {}).items():
        if not _GEO_FIELD.search(col):
            continue
        if (by_name.get(col, {}).get("distinct") or 0) < 5:
            continue
        places: set[str] = set()
        iso: set[str] = set()
        cn = 0
        for v in vals:
            low = v.lower()
            for word, cname in PLACES.items():
                if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low):
                    places.add(cname)
            if re.fullmatch(r"[A-Z]{3}", v):
                iso.add(v)
            if re.search(r"[省市县区]", v):
                cn += 1
        if len(places) >= 3 or len(iso) >= 3:
            code = "country"
        elif cn >= 2:
            code = "region"
        else:
            continue
        if code not in out:
            out.append(code)
    return out


def _axes_from_fields(fields: list[dict], samples: dict[str, list[str]]) -> tuple[list[str], list[str], list[str]]:
    """从真实字段名判主体与度量。返回 (主体, 度量, 依据词)。

    只认列名与列里的值，不认数据集名称——名称是别人对这份数据的称呼，列名
    是这份数据本身。两者冲突时，列名赢。
    """
    entities: list[str] = []
    measures: list[str] = []
    hits: list[str] = []
    names = [str(f.get("name") or "") for f in fields if not f.get("synthetic")]
    for name in names:
        toks = _tokens(name)
        for tok in toks:
            e = _F_ENTITY.get(tok)
            if e and e not in entities:
                entities.append(e)
                hits.append(f"{name}→主体")
            m = _F_MEASURE.get(tok)
            if m and m not in measures:
                measures.append(m)
                hits.append(f"{name}→度量")
        for cn, code in _F_CN.items():
            if cn in name:
                if code in _ENTITY_LABEL and code not in entities:
                    entities.append(code)
                    hits.append(f"{name}→主体")
                elif code not in measures and code in _MEASURE_LABEL:
                    measures.append(code)
                    hits.append(f"{name}→度量")
    # 列名认不出的主体，看值来补。放在列名之后：列名更具体（store 比 region
    # 有用），值只在该列名什么都没认出来时提供线索。
    for code in _entities_from_values(fields, samples):
        if code not in entities:
            entities.append(code)
            hits.append(f"列值→{_ENTITY_LABEL.get(code, code)}")
    # 时间列 → 序列；有数值列才算（纯文本里出现 year 不代表有观测）
    if any(_TIME_FIELD.match(n.strip()) for n in names) and any(
            f.get("type") == "number" for f in fields):
        if "series" not in measures:
            measures.append("series")
            hits.append("有年份列且有数值列→时间序列")
    # 名录类数据天然可数（与名称推断路径同一条规则）
    if ("company" in entities or "store" in entities) and "count" not in measures:
        measures.append("count")
    return entities, measures, list(dict.fromkeys(hits))


def _geo_from_values(samples: dict[str, list[str]]) -> str:
    """从样本值里看出地理范围。这是实测比推断强的地方：名称写着
    「Population」，值里写着 Afghanistan/Albania/Algeria，才知道它是全球面板。"""
    places: list[str] = []
    cn_hit = False
    iso3: set[str] = set()
    for col, vals in (samples or {}).items():
        if not _GEO_FIELD.search(col):
            continue
        for v in vals:
            low = v.lower()
            for word, cn in PLACES.items():
                if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low) and cn not in places:
                    places.append(cn)
            if any(len(c) == 2 and c in v for c in CITY_PARENT) or _CN_PLACE_VALUE(v):
                cn_hit = True
            if re.fullmatch(r"[A-Z]{3}", v):
                iso3.add(v)
    if len(places) >= 3 or len(iso3) >= 3:
        return "全球"
    if cn_hit:
        return "中国"
    if places:
        return "、".join(places[:3])
    return ""


def _CN_PLACE_VALUE(value: str) -> bool:
    """值是中文地名（省/市/区），不是代码。"""
    return bool(re.search(r"[省市县区镇街]", value))


def _granularity_observed(entities: list[str], fields: list[dict]) -> str:
    names = [str(f.get("name") or "") for f in fields if not f.get("synthetic")]
    yearly = any(re.fullmatch(r"year|年份|年度", n.strip(), re.I) for n in names)
    timed = any(_TIME_FIELD.match(n.strip()) for n in names)
    parts = [_ENTITY_LABEL.get(e, "") for e in entities[:2]]
    parts = [p for p in parts if p]
    if timed:
        parts.append("年度" if yearly else "时点")
    elif not any(_GEO_FIELD.search(n) for n in names):
        # 没有时间列、也没有地理列，却有两三个分类列 + 一个数值列：这是一张
        # 交叉分组表（英格兰人口按 age × gender）。说「分组」比说「地区」准
        # ——后者会让人以为每行对应一个地区。
        cats = [f for f in fields
                if f.get("type") == "string" and not f.get("synthetic")
                and 0 < (f.get("distinct") or 0) <= 30]
        nums = [f for f in fields if f.get("type") == "number"]
        if len(cats) >= 2 and nums:
            parts.append("分组")
    return "·".join(parts) if parts else "行级记录"


def _not_included_observed(entities: list[str], measures: list[str]) -> list[str]:
    """实测出来的「不含什么」。只对主体类数据说——一份人口分组表本来就该
    没有电话，列出来徒增噪音；而企业名录没有评分，是 agent 最容易误会的点。"""
    if not any(e in ("company", "store") for e in entities):
        return []
    out: list[str] = []
    for code in ("identity", "contact", "location", "opinion", "price"):
        if code not in measures:
            out.append(f"{_MEASURE_LABEL.get(code, code)}（实测字段中无对应列）")
    return out[:3]


def _asks_observed(entities: list[str], measures: list[str], sectors: list[str],
                   topic_names: list[str], fields: list[dict]) -> list[str]:
    """问题句。有主体就套模板；没有主体时，**用真实列名造句**——

    以前这里会退回「取水利、环境和公共设施管理业的环境指标数据」。那串行业名
    是名称推断来的（"co2" 命中 N 门类），对一份全球碳排放面板是文不对题的。
    实测之后有更好的材料：列名叫什么，就按什么筛。
    """
    if entities:
        return _asks(entities, measures, sectors, topic_names)
    dims = [str(f.get("name")) for f in fields
            if not f.get("synthetic") and f.get("type") == "string"
            and 1 < (f.get("distinct") or 0) <= 500]
    mlab = "、".join(_MEASURE_LABEL.get(m, m) for m in measures[:3])
    if dims and mlab:
        return [f"取{mlab}数据，可按 {' / '.join(dims[:3])} 筛选"]
    if mlab:
        return [f"取{mlab}数据"]
    if dims:
        return [f"检索记录，可按 {' / '.join(dims[:3])} 筛选"]
    return _asks(entities, measures, sectors, topic_names)


def _what_observed(geo: str, granularity: str, entities: list[str],
                   measures: list[str], fields: list[dict], records: int) -> str:
    """一句话，带真实列名。agent 判断「值不值得去」靠的正是这几个列名。"""
    head = " · ".join(x for x in (geo, granularity) if x)
    ent = "、".join(_ENTITY_LABEL.get(e, e) for e in entities)
    real = [str(f.get("name")) for f in fields if not f.get("synthetic")]
    body = f"{ent}数据，" if ent else ""
    if real:
        body += "字段 " + " / ".join(real[:4])
        if len(real) > 4:
            body += f" 等 {len(real)} 列"
    mea = "、".join(_MEASURE_LABEL.get(m, m) for m in measures[:3])
    if mea:
        body += ("，" if body else "") + f"可提供{mea}"
    body += f"（实测 {records} 行）"
    return f"{head}：{body}" if head else body


def _evidence_summary(ev: dict) -> dict:
    """放进能力卡的证据摘要：够复核，不臃肿（完整体在 item.evidence）。"""
    if ev.get("status") != "ok":
        return {
            "status": ev.get("status", ""),
            "endpoint": ev.get("endpoint", ""),
            "note": ev.get("note", ""),
            "probed_at": ev.get("probed_at", ""),
        }
    names = [str(f.get("name")) for f in ev.get("fields") or [] if not f.get("synthetic")]
    out = {
        "status": "ok",
        "endpoint": ev.get("endpoint", ""),
        "resolved_how": ev.get("resolved_how", "original"),
        "records": ev.get("records", 0),
        "field_count": ev.get("field_count", len(names)),
        "fields": names[:10],
        "probed_at": ev.get("probed_at", ""),
    }
    if ev.get("sample_only"):
        out["sample_only"] = True
    return out


def _apply_evidence(item: dict, base: dict, ev: dict) -> dict:
    """把实测结果盖到名称推断上。

    覆盖规则：**能测的轴以实测为准，测不了的轴保留推断并注明来源**。
    度量尤其不能合并——名称里写着「评价」而列里没有评分字段时，合并会让
    平台继续承诺一个不存在的度量。这正是 agent 被误导的源头。
    """
    fields = ev.get("fields") or []
    samples = ev.get("samples") or {}
    entities, measures, hits = _axes_from_fields(fields, samples)

    from_fields_entities = bool(entities)
    from_fields_measures = bool(measures)
    if not entities:
        entities = list(base.get("entities") or [])
    if not measures:
        measures = list(base.get("measures") or [])

    geo_values = _geo_from_values(samples)
    geo = geo_values or base.get("geo") or ""
    granularity = _granularity_observed(entities, fields)

    sectors = list(base.get("sectors") or [])
    topics = list(base.get("topics") or [])
    topic_names = list(base.get("topic_names") or [])
    # 子类必须透传：CURATED 判决在 base 卡里有，probed 重建时丢了它，
    # match() 的子类校验就永远拿不到卡侧证据（0.15 百问循环实测）。
    subtopics = list(base.get("subtopics") or [])
    asks = _asks_observed(entities, measures, sectors, topic_names, fields)
    what = _what_observed(_short(geo, 8), granularity, entities, measures,
                          fields, ev.get("records", 0))

    bits = [f"实测 {ev.get('records', 0)} 行 × {ev.get('field_count', 0)} 字段"]
    if from_fields_entities or from_fields_measures:
        bits.append("按列名判定 " + "、".join(hits[:4]))
    else:
        bits.append("列名未识别出主体/度量，沿用目录推断")
    if geo_values:
        bits.append(f"地理取自样本值（{geo_values}）")
    elif geo:
        bits.append("地理沿用目录推断")
    # 旧缓存里的 evidence 没有 sample_only 字段，用记录数/端点名兜底判定，
    # 这样不必为了加一个标记把 238 条全部重测一遍。
    sample_only = (bool(ev.get("sample_only")) or ev.get("records", 0) <= 1
                   or "preview" in str(ev.get("endpoint") or "").lower())
    if sample_only:
        bits.append("仅单行/预览样本，不代表全库覆盖范围")

    decl = {
        "what": what,
        "asks": asks,
        "entities": entities,
        "entity_labels": [_ENTITY_LABEL.get(e, e) for e in entities],
        "measures": measures,
        "measure_labels": [_MEASURE_LABEL.get(m, m) for m in measures],
        "sectors": sectors,
        "sector_names": [sectormod.SECTOR_NAME.get(c, c) for c in sectors],
        "topics": topics,
        "topic_names": topic_names,
        "subtopics": subtopics,
        "geo": geo,
        "geo_label": _short(geo, 8),
        "granularity": granularity,
        "role": "data",
        "basis": "probed",
        "basis_note": "；".join(bits) + f"；接入地址 {_short(ev.get('endpoint', ''), 96)}",
        "rule_version": RULE_VERSION,
        "evidence": _evidence_summary(ev),
    }
    absent = _not_included_observed(entities, measures)
    declared_absent = list(base.get("not_included") or [])
    merged = list(dict.fromkeys(absent + declared_absent))
    if merged:
        decl["not_included"] = merged[:5]
    if base.get("samples"):
        decl["samples"] = dict(base["samples"])
    return decl


# 对外只读别名：listings / mcp_server 要把这些标签直接展示给 agent，
# 不该去碰带下划线的内部名。
ENTITY_LABEL = _ENTITY_LABEL
MEASURE_LABEL = _MEASURE_LABEL
ENTITY_CODE_BY_LABEL = {l: c for c, l, _r in ENTITY_RULES}
MEASURE_CODE_BY_LABEL = {l: c for c, l, _r in MEASURE_RULES}


def axis_vocab() -> dict:
    """判对题用的两个坐标轴词表：主体（一条记录代表什么）、度量（能给出什么量）。

    为什么必须公开
    --------------
    这两轴是「这条路能不能接住你」的判据，其中**主体缺席还是一票否决**
    （见 match 的 complete 条件）。平台只让 agent 传 ``entities`` 却不给
    合法值，等于让调用方猜一个会被用来否决自己的暗号：传「国家」而不是
    ``country``，档案就静默判成「主体没验证」，然后 agent 以为货架没有。
    0.28 全量回归实测：「全国碳排放总量 分行业」只带主题+度量去问，
    OWID 二氧化碳明明对题，却因主体轴缺席而永不判完全满足——而 dp_map
    从没告诉过调用方主体轴有这么一套码。

    词表是有限集合（12 个主体 × 13 个度量），且随规则版本走，不随语料长。
    """
    return {
        "entity_axis": [{"code": c, "label": l} for c, l, _r in ENTITY_RULES],
        "measure_axis": [{"code": c, "label": l} for c, l, _r in MEASURE_RULES],
        "note": ("entities/measures 是平台验证对题用的码表，随 dp_discover 的 need "
                 "传；中文名也认（『国家』=country）。主体轴缺席会让任何候选都"
                 "无法判『完全满足』——它不是可选项。"),
    }

# (主体, 度量) -> 一句能直接抄给 agent 的问题句。没列到的组合走兜底模板，
# 这里只放高频、措辞不该被模板糟蹋的那几条。
_ASK_TEMPLATES: dict[tuple[str, str], str] = {
    ("company", "identity"): "查某家企业的工商登记信息（名称/统一社会信用代码/法人）",
    ("company", "contact"): "查某家企业的联系方式",
    ("company", "location"): "按地址定位企业，或查某地的企业分布",
    ("company", "count"): "按行业、地区统计企业数量",
    ("store", "identity"): "查某家门店对应的工商主体",
    ("store", "location"): "查某地有哪些门店（可按城市/区县/坐标半径筛）、开在哪",
    ("store", "count"): "按行业、地区统计门店数量",
    ("store", "opinion"): "查门店的口碑与评分",
    ("person", "health"): "查人群的疾病与诊疗指标",
    ("person", "count"): "统计某地区的人口规模",
    ("region", "count"): "统计某地区某类主体的数量",
    ("region", "series"): "取某地区逐年的指标序列",
    ("country", "series"): "取某国逐年的指标序列",
    ("country", "count"): "做跨国指标对比",
    ("event", "location"): "查某地发生过什么事件/灾害",
    ("corpus", "series"): "取可用于训练的样本语料",
    ("document", "identity"): "检索政策法规或专利文本",
}


def _text_of(item: dict) -> str:
    """派生用的全部文本。

    刻意**不含 publisher**：机构名里满是干扰词——西班牙农业部门的英文名带着
    ``Cultura``，于是「SIGPAC 地块数据」被判成文化体育娱乐业；联合国机构的
    中文名带着「中国」，于是全球贸易数据被判成中国范围。发布方只用于判地理
    （``_geo_of`` 单独处理），不参与主体/度量/行业。

    也不含 ``notes`` 里的模板句——「官方目录条目（France）」只含地名。
    """
    parts = [item.get("name") or "", " ".join(item.get("domain") or [])]
    meta = item.get("_hub_meta") or {}
    if meta.get("description"):
        parts.append(str(meta["description"])[:600])
    for kw in (meta.get("keywords") or []):
        if isinstance(kw, dict) and kw.get("label"):
            parts.append(str(kw["label"]))
    return " ".join(parts)


def _hits(rules, text: str) -> list[str]:
    low = text.lower()
    out = []
    for code, _label, pat in rules:
        if re.search(pat, low):
            out.append(code)
    return out


def _geo_of(item: dict) -> str:
    """地点：先看 domain（目录的第二个标签就是国家，最可靠），再看名称，
    最后才用「中文发布方 → 中国」这条弱规则。

    可能返回多个地名（「中国、欧盟」）：双边贸易数据本来就同时覆盖两方，
    只留一个会让另一半的提问匹配不上。空字符串表示「没说」——那比说错好。
    """
    domains = [str(d).strip() for d in (item.get("domain") or [])]
    # 0.39：香港的地理层级不能塌成「中国」。0.36 的接入脚本在 declare()
    # 之后手工覆盖过 geo，但每次 build_capabilities 重算卡都会把这条补丁
    # 冲掉——实测 3,885 条全退回「中国」，于是「深圳的电子厂」被一堆香港
    # 统计表靠 geo 命中挤出首位（0.32 修过「中国/贵州不覆盖中国/深圳」，
    # 这里是把同一条层级规则补到源头）。规则落在通用判据里，重算不丢。
    if "中国香港" in domains:
        return "中国香港"
    hay = " ".join(domains) + " " + (item.get("name") or "")
    low = hay.lower()
    found: list[str] = []
    # 长词优先，避免 "ireland" 被 "land" 类子串误伤（当前无此词，防日后加）
    for word in sorted(PLACES, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low):
            found.append(PLACES[word])
    # 提问侧常直接说中文地名（「英国碳排放」），而 PLACES 的键是英文词面。
    # 反向查一次规范名，否则用户用中文问就永远解析不出地理轴。
    for cn in sorted(set(PLACES.values()), key=len, reverse=True):
        if cn in hay and cn not in found:
            found.append(cn)
    if found:
        return "、".join(list(dict.fromkeys(found))[:3])
    # 「国际统计」「全球」这类目录标签说的是范围，不是国家。缺了它，OWID 的
    # 碳排放数据会对「英国碳排放」这个提问哑口无言——它明明能答，只是声明
    # 里没写国家名。
    if re.search(r"国际统计|全球|全世界|世界各国|\bworld\b|\bglobal\b", hay, re.I):
        return "全球"
    for city, parent in CITY_PARENT.items():
        if city in hay:
            # 保留层级，不要压成国家。「上海衡山路附近的咖啡店」若把地理轴
            # 塌成「中国」，agent 看到的需求坐标里就没有地点了，任何全国性
            # 数据都能在 geo 轴上「命中」——范围轴一宽，排序就只剩字面词面
            # 在起作用（0.20 实测：上海门户因此压过真正对题的商户名录）。
            # 层级写进字符串（父/子），geo_covers 按前缀判包含。「全国」
            # 「中国」本身就是国家级，不再套一层。
            return parent if city in (parent, "全国", "中国") else f"{parent}/{city}"
    pub = item.get("publisher") or ""
    name = item.get("name") or ""
    # 先剥掉括号里的中文译名：「Federal Reserve Bank of St. Louis（圣路易斯
    # 联储）」是英文机构，括号里几个汉字不代表它覆盖中国数据。剥完还有中文
    # 才算中文发布方。联合国机构、国际组织的中文名同样会带偏，再用名称排除。
    pub_own = re.sub(r"[（(][^）)]*[）)]", "", pub)
    if (re.search(r"[\u4e00-\u9fff]", pub_own)
            and not re.search(r"国际|全球|世界|联合国|world|global|united nations", name, re.I)):
        return "中国"
    return ""


# 格式词不是行业词。政务目录里「电子证照」说的是载体形态，不是电子制造业；
# 「（再加工）」说的是加工环节，不是农副食品加工。两条都在 0.32 上架贵州目录
# 条目时实测出错：「食品经营许可证（电子证照）」声明了 C 制造业，于是「深圳
# 的电子厂」把一份贵州餐饮证照排到第一条。供给侧的行业推断必须先把这些
# 格式词摘掉，再交词表——宁可少判一个门类，也不能判错一个。
_FORMAT_WORDS = re.compile(
    r"电子证照|电子印章|电子签名|电子票据|电子档案|电子凭证|电子证明"
    r"|再加工|数据加工|加工处理"
    # 「出租汽车」是营运车辆，不是汽车制造业。留着「汽车」这两个字，一份
    # 网约车经营许可证就会被声明成 C 制造业——和电子证照是同一类错。
    r"|网络预约出租汽车|巡游出租汽车|出租汽车")


def _sector_text(text: str) -> str:
    return _FORMAT_WORDS.sub(" ", text or "")


def _sectors_of(item: dict, text: str) -> tuple[list[str], list[str], str, list[str]]:
    """行业：declared 覆盖声明优先，其次主题词表 + 中文口语词。

    返回 (GB/T 门类码, 主题轴码, 依据, 主题子类码)。主题轴（T01–T15）单列
    出去：它回答「这类数据讲什么」，不回答「覆盖哪个行业」——把 statistics
    塞进 GB/T 门类会让「英国统计年鉴」看起来像某个具体行业的数据。
    子类（S0904 固废、S0901 空气…）只有在有真证据（署名判决）时才带：
    T09 一个 L1 装着空气/水/固废/生态，只比 L1 会把「塑料垃圾」判成
    二氧化碳的对题（0.15 百问循环实测）。
    """
    text = _sector_text(text)
    cov = sectormod.declared(item)
    low = text.lower()
    codes: list[str] = []
    themes: list[str] = []
    hit_words: list[str] = []
    subs: list[str] = []
    curated: tuple | None = None
    # 署名判决（taxonomy.CURATED）只覆盖**主题轴**：每条都有可被推翻的依据。
    # 此前判决只接到 dp_index 分类视图，能力卡的主题轴仍走词表兜底，两边会
    # 不一致（0.15 实测：OWID CO₂ / child mortality 分类视图是 T09/T06，
    # 能力卡却还是 S0103 兜底的 T01，需求侧主题轴验证因此落空）。GB/T 门类
    # 码仍按词表/声明算——主题与门类是两个维度，主题判决不该清空门类。
    curated = tx.CURATED.get(str(item.get("dataset_id") or ""))
    for word, cs in TOPIC_SECTOR.items():
        if not _keyword_hit(low, word):
            continue
        hit_words.append(word)
        for c in cs:
            if len(c) == 3 and c.startswith("T"):  # T01–T15 是主题轴
                if c not in themes:
                    themes.append(c)
            elif c not in codes:
                codes.append(c)
    for code, _name, alias in sectormod.detect(text):
        if code not in codes:
            codes.append(code)
        hit_words.append(alias)
    if cov.get("sectors"):
        declared_codes = [str(s).upper() for s in cov["sectors"]]
        if "ALL" in declared_codes:
            declared_codes = [c for c, _n, _a in sectormod.SECTORS]
        codes = declared_codes
    basis = ""
    if curated:
        themes = [curated[0]]
        if curated[1] and curated[1] != "-":
            subs = [curated[1]]
        basis = "人工判决：" + curated[2]
    elif hit_words:
        basis = "主题词命中 " + "、".join(dict.fromkeys(hit_words))
    elif cov.get("sectors"):
        basis = "发布方声明"
    return codes, themes, basis, subs


def _granularity(entities: list[str], measures: list[str]) -> str:
    if "company" in entities or "store" in entities:
        return "企业/门店"
    if "person" in entities:
        return "个人"
    if "household" in entities:
        return "住户"
    if "region" in entities:
        return "地区"
    if "country" in entities:
        return "国家"
    if "event" in entities:
        return "事件"
    if "series" in measures:
        return "时间点"
    return ""


def _asks(entities: list[str], measures: list[str], sectors: list[str],
          topic_names: list[str] | None = None) -> list[str]:
    """问题句：让 agent 一眼看到「这条数据能接住什么话」。

    上限 4 条。宁可少说：列十条泛泛的会稀释掉真正能用的那一条。
    """
    out: list[str] = []
    # 轮转取句，而不是让第一个主体吃满配额：全门类名录同时声明了
    # 企业 + 门店，按原顺序 company 的三条模板先把 4 条配额占满，
    # 「查某地有哪些门店、开在哪」被挤掉——而它恰恰是「贵阳民宿推荐
    # 几家」这类找门店的提问最该看到的一句（0.19 实测）。每个主体
    # 先保证一条，再回头补齐。
    per_entity: dict[str, list[str]] = {}
    for e in entities:
        for m in measures:
            t = _ASK_TEMPLATES.get((e, m))
            if t:
                per_entity.setdefault(e, []).append(t)
    round_i = 0
    while len(out) < 4:
        added = False
        for e in entities:
            qs = per_entity.get(e) or []
            if round_i < len(qs):
                if qs[round_i] not in out:
                    out.append(qs[round_i])
                added = True
                if len(out) >= 4:
                    break
        if not added:
            break
        round_i += 1
    if not out and entities:
        e = entities[0]
        label = _ENTITY_LABEL.get(e, "")
        sec = "、".join(sectormod.SECTOR_NAME.get(c, c) for c in sectors[:2])
        out.append(f"检索{sec + '的' if sec else ''}{label}记录")
    if not out and measures:
        sec = "、".join(sectormod.SECTOR_NAME.get(c, c) for c in sectors[:2])
        mlab = "、".join(_MEASURE_LABEL.get(m, m) for m in measures[:3])
        out.append(f"取{sec + '的' if sec else ''}{mlab}数据")
    if not out and sectors:
        sec = "、".join(sectormod.SECTOR_NAME.get(c, c) for c in sectors[:2])
        out.append(f"检索{sec}相关的目录条目")
    if not out and topic_names:
        out.append(f"检索{'、'.join(topic_names[:2])}主题的数据")
    return out[:4]


def _short(text: str, limit: int = 20) -> str:
    """展示用短名：去掉括号补充说明。``coverage.geo`` 是给机器匹配的，
    可能写着「中国（city 维度可按城市筛）」，一句话说明里不该出现它。"""
    s = re.split(r"[（(]", (text or "").strip())[0].strip()
    return s[:limit]


def declare(item: dict, evidence: dict | None = None) -> dict:
    """生成一条数据集的能力声明。纯函数，无 IO。

    证据优先：给了 ``evidence``（或条目自带 ``item["harvest"]``）且实测
    成功，就用真实字段判能力（``basis="probed"``）；否则退回名称推断
    （``declared`` / ``derived`` / ``unknown``）。

    实测失败也要留痕：卡上会写「未实测（原因）」，而不是悄悄退回推断——
    读者有权知道这张卡是看来的还是猜的。
    """
    ev = evidence if evidence is not None else item.get("harvest")
    base = _declare_named(item)
    if not ev:
        return base
    if base.get("role") == "portal":
        return base
    if ev.get("status") == "ok" and ev.get("fields"):
        return _apply_evidence(item, base, ev)
    base["evidence"] = _evidence_summary(ev)
    reason = ev.get("note") or STATUS_FALLBACK.get(ev.get("status", ""), ev.get("status", ""))
    if reason:
        base["basis_note"] = (base.get("basis_note") or "") + f"；未实测：{reason}"
    return base


def enrich_axes(item: dict, decl: dict, prior: dict | None = None) -> dict:
    """补齐主题轴，并给每根轴留下可审的来源（原地改 decl 并返回）。

    为什么不写进 ``declare()``：那个函数只说「按现有证据能声明什么」，是
    可复现的纯函数；补轴是**额外引入一条推断**，要单独留痕、单独降权，
    不能混进 ``basis=probed`` 的实测声明里。0.29 的 A/B/C 实测：无脑补
    176 条主题轴会让 16 问的 shelf_gap 从 8 掉到 2，同时把「高等教育」
    指去工会设施时间——所以补轴必须与 match() 的安全阀成对上线。

    平台/门户条目（role=portal，或分类视图 kind=P/A）按设计不补单亲主题：
    它们同时覆盖所有主题，硬塞一个 T 码只会造出假的归类证据。
    """
    prov = dict(decl.get("axis_provenance") or {})
    # 0.39：重跑必须能自愈档位。第一版把 source_mapped 卡沿用时一律标
    # declared，第二次 build_capabilities 就把 17,469 张「源方分类翻译」洗成了
    # 「发布方声明」——正好是 0.29 注释警告的伪造证据，只是换了个档位复发。
    # 所以 classify 每次都算，且 source_mapped 判在 prior 沿用之前。
    cl = tx.classify(item)
    ok_l1 = (cl["kind"] == "D" and cl["l1"].startswith("T")
             and cl["l1"] != "T00")
    prior_prov = (prior or {}).get("axis_provenance") or {}
    prior_tier = prior_prov.get("topics")

    if decl.get("role") == "portal":
        prov["topics"] = "not_applicable"
    elif (not decl.get("topics") and ok_l1
          and cl["confidence"] == "source_mapped"):
        # 0.39 新档：源方给了库名/分组，我们做了一次翻译。它比「按名称
        # 词表推断」强，但绝不是发布方写进合同的主题声明，所以既不能
        # 叫 declared，也不能按 inferred 那样几乎不给分。match() 给中间
        # 权重，且不进 verified（不解锁 complete）。
        decl["topics"] = [cl["l1"]]
        decl["topic_names"] = [tx.L1_NAME.get(cl["l1"], cl["l1"])]
        if cl["sub"].startswith("S") and not cl["sub"].endswith("09"):
            decl["subtopics"] = [cl["sub"]]
        src = tx.source_l1(item)
        prov["topics"] = "source_mapped"
        prov["topics_via"] = src[1] if src else "source_db_name"
        prov["topics_confidence"] = "source_mapped"
        prov["topics_reason"] = cl["reason"]
        decl["basis_note"] = ((decl.get("basis_note") or "")
                              + f"；主题轴由源方分类映射（{AXIS_VERSION}，"
                                "非实测，判对题时降权）")
    elif (not decl.get("topics") and prior and prior.get("topics")
          and prior_tier not in ("inferred", "undeclared", None)):
        # 重建卡片是刷新规则，**不是推翻既有判决**。旧卡上已经有主题轴
        # （署名判决 / 主题词表命中）时原样保留；否则一条本来能判 complete
        # 的真实匹配，重建后会因为变成「推断主题」被安全阀一票否决
        # （0.29 单测实测）。0.39 起档位按 prior **原样沿用**，不再一律
        # 升级成 declared——source_mapped 不能被洗白。
        decl["topics"] = list(prior["topics"])
        decl["topic_names"] = list(prior.get("topic_names")
                                   or [tx.L1_NAME.get(t, t) for t in prior["topics"]])
        decl["subtopics"] = list(prior.get("subtopics") or [])
        prov.update({k: v for k, v in prior_prov.items()})
        prov["topics"] = prior_tier
        prov.setdefault("topics_via", "stored_card")
    elif decl.get("topics"):
        # 已有主题轴：署名判决 / 主题词表 / 发布方声明，都不是本轮补的。
        prov.setdefault("topics", "declared")
    elif ok_l1 and cl["confidence"] in ("high", "medium", "curated",
                                        "inferred"):
        decl["topics"] = [cl["l1"]]
        decl["topic_names"] = [tx.L1_NAME.get(cl["l1"], cl["l1"])]
        # Sxx09 是「该大类下未细分」的兜底槽，不是真子类证据：只补 L1，
        # 不补子类，免得把「没细分」伪装成「已判到细类」。
        if cl["sub"].startswith("S") and not cl["sub"].endswith("09"):
            decl["subtopics"] = [cl["sub"]]
        prov["topics"] = "inferred"
        prov["topics_via"] = "taxonomy.classify"
        prov["topics_confidence"] = cl["confidence"]
        prov["topics_reason"] = cl["reason"]
        decl["basis_note"] = ((decl.get("basis_note") or "")
                              + f"；主题轴由分类树推断（{AXIS_VERSION}，非实测，判对题时降权）")
    else:
        prov.setdefault("topics", "undeclared")
    decl["axis_provenance"] = prov
    decl["axis_version"] = AXIS_VERSION
    return decl


STATUS_FALLBACK = {
    "no_url": "目录未提供取数地址",
    "no_public_endpoint": "已核实源站没有免登录的内容级取数端点",
    "portal_catalog_only": "门户不持有内容级记录，已核实其公开目录检索端点",
    "html": "该地址是网页而非数据",
    "binary": "该地址是 Excel/PDF 等文件，需另做表格解析",
    "binary_nontable": "二进制容器里没有可还原的记录表（排版报表/扫描件）",
    "service_doc": "该地址是地图/目录服务文档",
    "auth": "该地址需要凭证",
    "upstream_blocked": "上游 WAF/CDN 拒绝本次访问，登记凭证无效",
}


def _declare_named(item: dict) -> dict:
    """只按目录元数据（名称/主题/声明）判能力。无实测证据时的基线。"""
    text = _text_of(item)
    cov = sectormod.declared(item)

    entities = _hits(ENTITY_RULES, text)
    measures = _hits(MEASURE_RULES, text)
    # 0.39：名字里明写的度量要补进卡，否则「有这条真数据」与「缺这个指标」
    # 会在同一条回答里同时成立。只补缺、不覆盖既有判定，并留可推翻的标记。
    from_name = [m for m in _hits(_NAME_MEASURE_RULES, str(item.get("name") or ""))
                 if m not in measures]
    measures.extend(from_name)

    # declared 的字段清单是硬证据：它能把「按名称猜的度量」落实成「真有这列」。
    field_measures: list[str] = []
    field_names = [str(f.get("name") or "") for f in (item.get("fields") or [])]
    for name in field_names:
        key = name.lower().split(".")[-1]
        m = FIELD_MEASURE.get(key)
        if m:
            field_measures.append(m)
    if cov.get("fields"):
        for name in [str(f) for f in cov["fields"]]:
            key = name.lower().split(".")[-1]
            m = FIELD_MEASURE.get(key)
            if m:
                field_measures.append(m)
    for m in field_measures:
        if m not in measures:
            measures.append(m)

    # 名录类数据自带计数能力。一份企业名录即使没有「数量」这个字段，也能
    # 按行业/地区数出有多少家——「苏州有多少家面馆」问的正是这个。不写这
    # 一条，最该被这条路接住的问题会被判成「缺数量统计」。
    if ("company" in entities or "store" in entities) and "count" not in measures:
        measures.append("count")

    sectors, themes, sector_basis, topic_subs = _sectors_of(item, text)
    # 细粒度主题：GB/T 的 N 门类只有 20 个大类，光靠它分不清「污水处理」
    # 和「空气质量」。污水词族单独打 T13（城市与公用事业 / 供水污水），
    # 让需求侧的 topic 轴有东西可比。只补标签，不改门类归属。
    if WASTEWATER_CONTEXT.search(text) and "T13" not in themes:
        themes.append("T13")
        sector_basis = (sector_basis + "；污水主题词命中 T13").strip("；")
    geo = _geo_of(item)
    if cov.get("geo"):
        geo = str(cov["geo"])
    granularity = str(cov.get("granularity") or "") or _granularity(entities, measures)
    topic_names = [tx.L1_NAME.get(t, t) for t in themes]

    # 平台门户不是数据集：它自己不提供记录，只提供「去哪儿找」。把它和
    # 数据集混为一谈，agent 会问「这个门户有几条数据」——它没有。所以单列
    # 一种能力说法，且排在 data 之外。
    # 判据不能只看 kind：目录里混着「上海市-软件和信息技术服务业-企业数据
    # 可信空间产品」这种名字带"服务"的产品条目，它是有记录的数据集。
    name_l = (item.get("name") or "").lower()
    is_hub = (item.get("dataset_id") or "").startswith("hub_") or (
        tx.kind_of(item) in ("P", "A")
        and bool(re.search(r"门户|平台|目录|开放数据|datasets?\b|open data|"
                           r"仓库|存储库|聚合索引|数据中心|数据服务|数据库$", name_l)))
    if is_hub:
        basis = "declared"
        basis_note = "目录服务条目：本身不持有记录，只提供站内检索与申领入口"
        what = _hub_what(item, topic_names, geo)
        asks = ["到该门户站内检索目标数据", "查该门户收录了哪些数据集"]
        return {
            "what": what,
            "asks": asks,
            "entities": [], "entity_labels": [],
            "measures": [], "measure_labels": [],
            "sectors": sectors, "sector_names": [sectormod.SECTOR_NAME.get(c, c) for c in sectors],
            "topics": themes, "topic_names": topic_names,
            "geo": geo, "geo_label": _short(geo, 8),
            "granularity": "数据门户",
            "role": "portal",
            "basis": basis, "basis_note": basis_note,
            "rule_version": RULE_VERSION,
        }

    # 一个是非判断：这条声明到底站在什么证据上
    if cov:
        basis = "declared"
        basis_note = "发布方 capability/coverage 声明" + (
            f"；{sector_basis}" if sector_basis and sector_basis != "发布方声明" else "")
    elif entities or measures or sectors or themes:
        basis = "derived"
        bits = []
        if entities:
            bits.append("主体=" + "/".join(_ENTITY_LABEL.get(e, e) for e in entities))
        if measures:
            bits.append("度量=" + "/".join(_MEASURE_LABEL.get(m, m) for m in measures))
        if themes:
            bits.append("主题=" + "/".join(tx.L1_NAME.get(t, t) for t in themes))
        if sector_basis and sector_basis != "发布方声明":
            bits.append(sector_basis)
        basis_note = "按目录元数据推断：" + "；".join(bits)
    else:
        basis = "unknown"
        basis_note = "目录元数据不足以判断能力，只能按名称字面检索"

    what = _what(_short(geo, 8), _short(granularity, 12), entities, measures,
                 sectors, topic_names)
    asks = _asks(entities, measures, sectors, topic_names)
    decl = {
        "what": what,
        "asks": asks,
        "entities": entities,
        "entity_labels": [_ENTITY_LABEL.get(e, e) for e in entities],
        "measures": measures,
        "measure_labels": [_MEASURE_LABEL.get(m, m) for m in measures],
        # 0.39：哪些度量是「看名字推的」，可被源方字段字典推翻。空串表示
        # 全部来自词表/字段声明，没有名字推断成分。
        "measures_basis": "name" if from_name else "",
        "measures_from_name": from_name,
        "sectors": sectors,
        "sector_names": [sectormod.SECTOR_NAME.get(c, c) for c in sectors],
        "topics": themes,
        "topic_names": topic_names,
        # 主题子类（署名判决才有）：T09 一个 L1 装着空气/水/固废/生态，
        # 需求与卡都带子类时 match() 必须子类相交，防止「塑料垃圾 →
        # 二氧化碳」这种 L1 内部串门（0.15 百问循环实测）。
        "subtopics": topic_subs,
        "geo": geo,
        "geo_label": _short(geo, 8),
        "granularity": granularity,
        "role": "data",
        "basis": basis,
        "basis_note": basis_note,
        "rule_version": RULE_VERSION,
    }
    if cov.get("not_included"):
        decl["not_included"] = list(cov["not_included"])
    # 抽样条数是最硬的一句：它把「我覆盖这个门类」变成「这个门类里我有
    # 6,562 条小吃服务」。agent 拿它判断值不值得去，不用再问一次。
    if cov.get("sector_samples"):
        decl["samples"] = dict(cov["sector_samples"])
    return decl


def _hub_what(item: dict, topic_names: list[str], geo: str) -> str:
    """门户的一句话。必须说清「这里没有记录」——否则 agent 会以为能直接取数。"""
    pub = _short(item.get("publisher") or "", 16)
    head = f"数据门户（{pub}）" if pub else "数据门户"
    what = "、".join(topic_names[:3])
    tail = f"聚合{what}类数据集" if what else "聚合多个数据集"
    if geo:
        tail = f"{geo}的{tail}"
    return f"{head}：{tail}的入口，本身不持有记录，需站内检索后回源取数"


def _what(geo: str, granularity: str, entities: list[str],
          measures: list[str], sectors: list[str],
          topic_names: list[str] | None = None) -> str:
    """一句话「能干什么」。

    诚实原则写在这里：主题不知道就说「具体主题未声明」，绝不拿一个泛词
    （统计、数据）冒充主题——「海豹统计」被说成「宏观经济统计」比不说更坏。
    """
    head = " · ".join(x for x in (geo, granularity) if x)
    topics = topic_names or []
    theme = "、".join(topics[:2]) or "、".join(
        sectormod.SECTOR_NAME.get(c, c) for c in sectors[:2])
    ent = "、".join(_ENTITY_LABEL.get(e, e) for e in entities)
    mea = "、".join(_MEASURE_LABEL.get(m, m) for m in measures[:4])
    if ent and mea:
        body = f"{ent}数据，可提供{mea}"
    elif ent:
        body = f"{ent}数据（内容级字段未声明）"
    elif mea and theme:
        body = f"{theme}类数据，可提供{mea}"
    elif mea:
        body = f"可提供{mea}，具体主题未声明"
    elif sectors:
        body = "、".join(sectormod.SECTOR_NAME.get(c, c) for c in sectors[:3]) + "相关数据"
    elif topics:
        body = f"{'、'.join(topics[:3])}主题数据（内容级字段未声明）"
    else:
        # 兜底也要有信息量：说「不知道主题」比说「无说明」有用，因为 agent
        # 至少知道这条只能靠名称字面检索，不必按能力去指望它。
        body = "目录条目，内容主题未在元数据中声明，需回源站确认"
    return f"{head}：{body}" if head else body


# ── 0.40：需求约束（声明型，不参与硬过滤）──────────────────────────────
# 以前 need 只回答「要什么」（主体/度量/行业/地理/主题），不回答「要多新、
# 多准、能花多少钱、拿去干什么、要多细、覆盖哪几个地区」。前一类决定对不
# 对题，后一类决定能不能用——两者缺一，agent 只能取回来再自己撞墙。
#
# 这些字段是**声明**，不是判定：货架上绝大多数条目没有源方的时间覆盖与
# 许可声明（见 source_terms.coverage()），拿它们做硬过滤会制造大面积假阴性。
# 所以 check_constraints() 只出警告，并把「没验的约束」明写成 unchecked。
CONSTRAINT_KEYS = ("freshness_requirement", "quality_requirement",
                   "budget_limit", "purpose", "granularity", "regions")

_REGION_ADMIN = re.compile(r"[\u4e00-\u9fff]{2,10}?(?:省|市|自治区|特别行政区)")
_REGION_COUNTRY = re.compile(
    r"中国香港|中国澳门|中国台湾|中国|美国|英国|德国|法国|日本|韩国|印度|"
    r"巴西|俄罗斯|欧盟|澳大利亚|加拿大|新加坡|世界|全球|"
    r"\b(?:China|USA|United States|UK|Germany|France|Japan|India|Brazil|EU)\b",
    re.I)


def empty_constraints() -> dict:
    """一份形状稳定的空约束，保证 need 的键在任何入口下都存在。"""
    return {
        "freshness_requirement": "",
        "quality_requirement": [],
        "budget_limit": {},
        "purpose": [],
        "granularity": {},
        "regions": [],
    }


def parse_constraints(query: str) -> dict:
    """从用户原话里认出 6 类约束。只认明确词面，不做语义推断。"""
    q = str(query or "")
    out = empty_constraints()

    if re.search(r"实时|当天|今日|今天|此刻|最新|当前|现时|"
                 r"real[- ]?time|\blive\b|up[- ]?to[- ]?date|\bcurrent\b", q, re.I):
        out["freshness_requirement"] = "realtime"
    elif re.search(r"近年|最近|近\s*[一二三四五\d]+\s*年|\brecent\b|\blatest\b", q, re.I):
        out["freshness_requirement"] = "recent"
    elif re.search(r"历史|历年|过去\s*\d+\s*年|时间序列|\bhistorical\b", q, re.I):
        out["freshness_requirement"] = "historical"

    if re.search(r"官方|权威|政府|统计局|\bofficial\b|\bauthoritative\b", q, re.I):
        out["quality_requirement"].append("authoritative")
    if re.search(r"可核验|可追溯|核实|校验|准确|精确|经审核|"
                 r"\bverified\b|\bauditable\b", q, re.I):
        out["quality_requirement"].append("verified")

    if re.search(r"免费|不花钱|零预算|无预算|不付费|不要付费|"
                 r"\bfree\b|\bno budget\b|\bzero cost\b", q, re.I):
        out["budget_limit"]["free_only"] = True
    m = re.search(r"预算\s*[¥￥]?\s*(\d+)", q)
    if m:
        out["budget_limit"]["max_amount"] = int(m.group(1))

    for word, pat in (
        ("commercial", r"商用|商业用途|商业使用|商业化|\bcommercial\b"),
        ("research", r"科研|研究|论文|学术|\bresearch\b|\bacademic\b"),
        ("redistribution", r"再分发|转载|二次发布|对外提供|转发给|"
                           r"redistribut|republish"),
        ("compliance", r"合规|审计|监管|\bcompliance\b|\baudit\b"),
    ):
        if re.search(pat, q, re.I):
            out["purpose"].append(word)

    gran = out["granularity"]
    for word, pat in (("micro", r"逐条|明细|单笔|微观|\bmicro\b|record[- ]level"),
                      ("admin", r"省级|市级|区县|街道|乡镇|\bprovince\b|\bcity\b|\bdistrict\b"),
                      ("annual", r"年度|按年|逐年|\bannual\b"),
                      ("monthly", r"月度|按月|逐月|\bmonthly\b"),
                      ("daily", r"日度|逐日|每日|\bdaily\b"),
                      ("quarterly", r"季度|按季|\bquarterly\b")):
        if re.search(pat, q, re.I):
            gran["spatial" if word in ("micro", "admin") else "temporal"] = word

    # 多地区：只在真的认出 2 个以上互不相同的地名时才填。一个地名已经由
    # geo 轴表达，重复一遍只会让 agent 以为平台做过并集判断。
    found: list[str] = []
    for pat in (_REGION_ADMIN, _REGION_COUNTRY):
        for m in pat.finditer(q):
            v = m.group(0).strip()
            if v and v not in found and not any(v in f or f in v for f in found):
                found.append(v)
    if len(found) >= 2:
        out["regions"] = found[:8]
    return out


def parse_need(query: str) -> dict:
    """把 agent 的提问解析成需求坐标。这是路由的入口。

    与 ``sectors.detect`` 的分工：那个只认行业口语词，这个要同时认出「要什么
    主体」「要什么量」「要哪个地方」。三者缺一，路由都会退化回字面匹配。
    """
    q = (query or "").strip()
    low = q.lower()
    # 需求侧用 detect_query：门类别名之外再认一轮用户口语词（博物馆/专利/
    # PM2.5…）。供给侧 _sectors_of 仍用 detect，两张表互不污染。
    sectors = [c for c, _n, _a in sectormod.detect_query(q)]
    unmapped = sectormod.detect_unmapped(q)
    entities = _hits(ENTITY_RULES, q)
    # 「世界银行」是机构名：「世界」不是主体范围、「银行」不是金融行业。
    # 0.15 实测「世界银行非洲援助金额」被判 entities=['country']、sectors=['J']，
    # 与 0.13「国内生产总值≠制造业」同类复合词误判。
    if re.search(r"世界银行|世行|\bworld bank\b", q, re.I):
        if "country" in entities:
            entities.remove("country")
        sectors = [s for s in sectors if s != "J"]
    # 主题轴：走分级地图（bdsp.map，0.18 起定调）。需求归类只有一个入口
    # ——地图节点 + 绑定在节点上的引导词面（有限集合，上限=节点数）；
    # agent 主路径则是拿 dp_map 自己归大类、传结构化坐标，根本不经过这里。
    # 业务代码不再写任何领域词族——那是个补不完的无底洞。
    _mn = mapmod.resolve_need(q)
    topics: list[str] = _mn["topics"]
    subtopics: list[str] = _mn["subtopics"]
    # 「居民消费价格指数」是一个宏观指标名，不是「一条条居民记录」。
    # 不摘掉 person，CPI 会把各国人口普查数据整页拉进候选。
    if "person" in entities and re.search(
            r"居民消费价格|居民消费指数|居民收入|居民储蓄|居民消费水平", q):
        entities.remove("person")
    # 污水语境下 ``treatment`` 不当诊疗；其它医疗词面（disease / diagnosis）
    # 仍走原词表。只清洗传给度量规则的文本，raw 保留用户原话。
    measure_text = re.sub(r"\btreatment\b", "processing", q, flags=re.I) \
        if WASTEWATER_CONTEXT.search(q) else q
    need = {
        "entities": entities,
        "measures": _hits(MEASURE_RULES, measure_text),
        "geo": _geo_of({"name": q, "domain": [], "publisher": ""}),
        "sectors": sectors,
        "topics": topics,
        "subtopics": subtopics,
        # 门类体系之外的主题（军事/国防）。认出来是为了**说没有**，
        # 不是为了硬塞进某个 GB/T 门类。
        "unmapped_topic": unmapped,
        "ambiguous": [],
    }
    # 「哪家好吃」「推荐」这类词没有度量词面，但意图是口碑。不认出它，
    # 「推荐几家苏州汤面」会被当成「找门店」，凭空承诺了评价能力。
    if re.search(r"哪家|好吃|推荐|口碑|怎么样|评价|评分|值得去", q):
        if "opinion" not in need["measures"]:
            need["measures"].append("opinion")
    # 「多少家」「数量」「总数」是真数量意图。「统计」不算——它是填充词
    # （「服务贸易额 统计」），拿它造 count 需求会把 UN Comtrade 这类真
    # 正主打成「缺数量统计」的 mismatch（0.17 实测）。
    if re.search(r"多少家|几家|多少个|数量|总数", q):
        if "count" not in need["measures"]:
            need["measures"].append("count")
    # MEASURE_RULES 词表里的「统计/statistic」也会造 count（供侧共用，不能
    # 动词表）——需求侧把它洗掉：除非还有别的数量词面（数量/条数/规模/
    # 占比/number of…）撑着，纯「统计」撑起来的 count 不作数。
    if "count" in need["measures"] and not re.search(
            r"数量|条数|规模|占比|多少|几家|"
            r"\bcounts?\b|\bheadcounts?\b|\bnumber of\b|\bshare of\b",
            measure_text, re.I):
        need["measures"].remove("count")
    # 「某家公司的电话」这类联系意图
    if re.search(r"电话|联系方式|怎么联系", q):
        if "contact" not in need["measures"]:
            need["measures"].append("contact")
    # 「在哪」「地址」是位置意图
    if re.search(r"在哪|地址|位置|附近|分布", q):
        if "location" not in need["measures"]:
            need["measures"].append("location")
    # 「2010～2020」这类年份区间：解析出来透出给 agent，让它看见「要的
    # 是哪段时间」。不参与 match 硬验证——能力卡目前没有时间覆盖声明，
    # 拿它一票否决会把没有时间标注的好数据全打掉。
    ym = re.search(r"((?:19|20)\d{2})\s*[～~\-–—至到]\s*((?:19|20)\d{2})", q)
    if ym and int(ym.group(1)) <= int(ym.group(2)):
        need["year_from"] = int(ym.group(1))
        need["year_to"] = int(ym.group(2))
    # 「2010年」单年份：同样透出不做硬验证。区间和单年互斥——先区间后单年，
    # 命中区间就不再看单年（「2010～2020年」里的 2020 不该覆盖区间）。
    if "year_from" not in need:
        y1 = re.search(r"((?:19|20)\d{2})\s*年?度?(?!\d)", q)
        if y1 and 1950 <= int(y1.group(1)) <= 2030:
            need["year_from"] = need["year_to"] = int(y1.group(1))
    # 街道级地名（「衡山路」「南京西路」）：平台不做地名解析，但必须让
    # agent 看见「还有一条街级约束没下推」——它决定取到城市级数据后要不要
    # 再按地址字段筛。认法是形态（以路/街/大道/弄/巷收尾的中文串），不是
    # 地名词表：路名补不完，后缀只有几个。取最长的一段（避免把「上海衡山
    # 路」截成「海衡山路」），再剥掉前面的动词/方位虚词。
    cands = _GEO_STREET.findall(q) or [""]
    street = max(cands, key=len)
    while street and _GEO_FILLER.match(street):
        street = _GEO_FILLER.sub("", street, count=1)
    # 「上海衡山路」里「上海」已经进地理轴了，街名只留「衡山路」。
    _city = need["geo"].rsplit("/", 1)[-1]
    if _city and _city != "中国" and street.startswith(_city):
        street = street[len(_city):]
    need["geo_detail"] = street
    need["matched"] = bool(need["entities"] or need["measures"]
                           or need["sectors"] or need["topics"])
    # 只有「统计 / 数量」这类弱度量、既没主体也没行业也没主题时，任何数据集
    # 都不该被判「完全满足」——这一格信息量不足以证明对题。discover 会把它
    # 摊开给 agent 看，让「换个说法重问」成为明确的下一步。
    need["weak_need"] = not (need["entities"] or need["sectors"]
                             or need["topics"])
    # 0.40：约束与坐标分开存。坐标参与 match()，约束不参与——它们只在
    # discover 里出可解释的警告，绝不改分数、也绝不删候选。
    need.update(parse_constraints(q))
    need["raw"] = q
    return need


def need_from_coordinates(coords: dict) -> dict:
    """agent 走分级地图归类后的入口：结构化坐标直接成需求，**不分词**。

    coords 应先过 ``map.validate_need``。这是主路径——agent 理解用户需求、
    在 dp_map 上归大类，把 topics/subtopics/sectors/geo/year… 传进来；
    平台只做结构校验（validate 已做）和拿名片验证对题。任何语言都走这条
    路，平台不补词表。
    """
    entities = [str(e) for e in (coords.get("entities") or [])]
    measures = [str(m) for m in (coords.get("measures") or [])]
    sectors = [str(s) for s in (coords.get("sectors") or [])]
    topics = [str(t) for t in (coords.get("topics") or [])]
    subtopics = [str(s) for s in (coords.get("subtopics") or [])]
    geo = str(coords.get("geo") or "").strip()
    if geo:
        # 与 parse_need 同一地名规范化：agent 可能传「中国」也可能传「China」。
        # 已是层级（「中国/上海」）时只规范最后一段——整体再解析会被「中国」
        # 抢先命中，城市层级塌回国家（0.20 实测）。
        if "/" in geo:
            geo = _geo_of({"name": geo.rsplit("/", 1)[-1], "domain": [],
                           "publisher": ""}) or geo
        else:
            geo = _geo_of({"name": geo, "domain": [], "publisher": ""}) or geo
    need = {
        "entities": entities,
        "measures": measures,
        "geo": geo,
        # 街级约束由调用方（parse_need 或 agent）给，原样透传，不猜。
        "geo_detail": str(coords.get("geo_detail") or "").strip(),
        # 0.44：agent 明确给的层级与要的那一列，**原样带到下游**。这两项是
        # 语义判断的产物（agent 知道「浦东新区是区」「农民收入对应哪一列」），
        # 在这里丢掉等于逼平台重新推断——而平台只有启发式。locator 优先用
        # geo_level，回显里也照实报，agent 才能核对它自己那份判断。
        "geo_level": str(coords.get("geo_level") or "").strip(),
        "indicator": [str(x) for x in (coords.get("indicator") or []) if str(x).strip()],
        "sectors": sectors,
        "topics": topics,
        "subtopics": subtopics,
        "unmapped_topic": [],
        "ambiguous": [],
    }
    # 层级声明与字面推断不一致时的提示（validate_need 产的），一路带到回显。
    if coords.get("geo_level_conflict"):
        need["geo_level_conflict"] = coords["geo_level_conflict"]
    for k in ("year_from", "year_to"):
        if coords.get(k) is not None:
            need[k] = int(coords[k])
    # 0.40：主路径（agent 传坐标）同样能带约束；没传也要给一份稳定的空形状，
    # 免得 discover 的返回体在两条路上键名不一致。
    base = empty_constraints()
    for k, default in base.items():
        v = coords.get(k)
        if v in (None, "", [], {}):
            need[k] = default
        else:
            need[k] = v
    need["matched"] = bool(entities or measures or sectors or topics)
    need["weak_need"] = not (entities or sectors or topics)
    need["raw"] = str(coords.get("raw") or "")
    return need


def _geo_scope(value: str) -> list[tuple[str, str]]:
    """把地理声明拆成 [(国家/上级, 具体地区), …]。

    「中国/贵州」是一种说法，不是两个地方：前者是国家层，后者是它管的那块
    地。「中国、欧盟」才是两个独立的覆盖范围。括注（「中国（可按城市筛）」）
    是说明，不参与比对。
    """
    head = re.split(r"[（(]", str(value or ""))[0]
    out: list[tuple[str, str]] = []
    for chunk in head.split("、"):
        parts = [p.strip() for p in chunk.split("/") if p.strip()]
        if not parts:
            continue
        out.append((parts[0], parts[-1] if len(parts) > 1 else ""))
    return out


def geo_covers(declared: str, wanted: str) -> bool:
    """声明的地理范围能否覆盖提问的地方。

    四条判定，按强弱：
    1. 「全球」覆盖任何具体地方——世界银行的数据当然包含英国。
    2. 同名（「中国（可按城市筛）」⊃「中国」）。
    3. 中国 ⊃ 它的城市与省份（问苏州，声明说中国，算覆盖）。
    4. 两侧都写到具体地区而地区不同 → 不覆盖。「中国/贵州」的证照数据不含
       「中国/深圳」，此前只比国家层，贵州条目对深圳的提问亮过绿灯（0.32
       全量上架后实测：「深圳的电子厂」首条是贵州省食品经营许可证）。
    """
    if not declared or not wanted:
        return False
    w_scopes = _geo_scope(wanted) or [("", "")]
    for d_country, d_region in _geo_scope(declared):
        if d_country in ("全球", "世界", "国际"):
            return True
        for w_country, w_region in w_scopes:
            w_places = [p for p in (w_country, w_region) if p]
            # 4. 地区层冲突：贵州 ≠ 深圳，哪怕国家层一样。
            if d_region and w_region and d_region != w_region:
                continue
            for d in (d_country, d_region):
                if d and d in w_places:
                    return True
            # 3. 上级覆盖下级：问深圳，声明中国。
            for w in w_places:
                if CITY_PARENT.get(w) == d_country or CITY_PARENT.get(d_country) == w:
                    return True
    return False


# 需求里明写出的拉丁专名（GDP / CPI / ICP…）在条目名里复现，是「名字对题」
# 的硬证据。它不是语义猜测，也不看中文词表——只是字符串包含。
_LATIN_STOP = frozenset({"of", "the", "in", "and", "per", "data", "stat",
                         "stats", "statistics", "for", "to", "by", "vs", "a", "an"})


def _name_identity_hit(raw: str, name: str) -> bool:
    toks = {t for t in re.findall(r"[A-Za-z]{2,}", (raw or "").lower())
            if t not in _LATIN_STOP}
    low = (name or "").lower()
    return any(t in low for t in toks)


def match(decl: dict, need: dict, fields: list | None = None,
          name: str = "") -> dict | None:
    """声明 × 需求，按轴打分。返回 {score, hit, missing, why, complete, ...}。

    ``complete`` 的语义在 0.13 收紧过。旧实现是 ``not missing``——把
    「没发现缺失」当成了「确认对上」。当提问的行业没被解析出来、或数据集
    没声明行业时，行业轴根本不参与比对，一句「统计」（count）就能让
    missing 为空，于是「SIPRI 各国军费开支」会被 OWID 碳排放判成完全满足。
    现在要求四条同时成立：

      1. 没有任何轴被判缺；
      2. 至少一条**主体或行业**轴真的正向命中（不能只靠弱度量）；
      3. 至少两条强轴被验证过；
      4. 至少一条**内容轴**（度量/行业/主题）被验证——entity+geo 两个
         「范围轴」不证明内容对题（0.15 百问循环：全球森林覆盖率给
         OWID 二氧化碳亮过绿灯）。

    ``count`` 不算强轴：名录类数据一律自带「能数多少家」，它没有区分力。
    没能验证的轴列进 ``unknown_axes``，让 agent 看见「没查」而不是「没问题」。
    """
    if not decl or not need.get("matched"):
        return None
    hit: list[str] = []
    missing: list[str] = []
    verified: set[str] = set()
    weak_axes: set[str] = set()
    score = 0.0
    # 0.29 安全阀：推断出来的主题轴只是召回线索，不是对题证据。
    prov = decl.get("axis_provenance") or {}
    topic_inferred = prov.get("topics") == "inferred"
    # 0.39：源方分类映射档，比 inferred 强（+2.0 而不是 +0.5）、比 declared
    # 弱（不进 verified，不解锁 complete）。
    topic_mapped = prov.get("topics") == "source_mapped"

    # 主体：agent 要门店名录，给人口普查没用，这轴权重最高
    e_need = [e for e in need.get("entities") or [] if e != "region"]
    verified_entities: list[str] = []
    if e_need:
        d_ent = decl.get("entities") or []
        got = [e for e in e_need if e in d_ent]
        if got:
            score += 5.0 * len(got)
            verified.add("entity")
            verified_entities = list(got)
            hit.append("主体=" + "/".join(_ENTITY_LABEL.get(e, e) for e in got))
        else:
            # 主体不对是硬伤，但不到「完全无关」：它可能仍在同一行业里。
            # 扣一点分让它下沉，不扣到消失——「部分满足」也是要交付的信息。
            score -= 1.5
            missing.append("主体（要" + "/".join(_ENTITY_LABEL.get(e, e) for e in e_need) + "）")

    # 度量：能给的量对不上，就是「有这类主体但没这个数据」
    m_need = list(need.get("measures") or [])
    lost_core: list[str] = []
    if m_need:
        d_mea = decl.get("measures") or []
        got = [m for m in m_need if m in d_mea]
        lost = [m for m in m_need if m not in d_mea]
        # 「附加信息」与「核心交付」是两回事：名录/目录类数据天然不带
        # 口碑评价、联系方式，缺这两样不影响「这条路能不能找到主体」；
        # 价格/产量/税收/时序缺了，就是答不了这个问题（0.19 实测：
        # CPI 被 Comtrade 判 partial、粮食产量被能源数据判 partial，
        # 都是把核心度量当成了可容忍的附加项）。
        lost_core = [m for m in lost if m not in _ENHANCEMENT_MEASURES]
        if got:
            score += 3.0 * len(got)
            if any(m != "count" for m in got):
                verified.add("measure")
            hit.append("可提供" + "/".join(_MEASURE_LABEL.get(m, m) for m in got))
        if lost:
            missing.append("数据（缺" + "/".join(_MEASURE_LABEL.get(m, m) for m in lost) + "）")

    # 行业：覆盖声明命中
    s_need = list(need.get("sectors") or [])
    if s_need:
        d_sec = [str(x).upper() for x in (decl.get("sectors") or [])]
        got = [s for s in s_need if s in d_sec]
        if got:
            score += 3.0 * len(got)
            verified.add("sector")
            hit.append("覆盖 " + "、".join(
                f"{s} {sectormod.SECTOR_NAME.get(s, s)}" for s in got))
            samples = decl.get("samples") or {}
            first = next((s for s in got if samples.get(s)), None)
            if first:
                hit.append("该类实测：" + str(samples[first])[:120])
        else:
            # 问的是餐饮业，给一份全国碳排放序列——「数量统计」这轴对上了，
            # 行业对不上，它就不是这条路。扣分比只记 missing 更能把它压下去。
            score -= 2.0
            missing.append("行业（要" + "、".join(sectormod.SECTOR_NAME.get(s, s) for s in s_need) + "）")

    # 细粒度主题：门类（N）太粗，污水治理和空气质量都落在它下面。提问里
    # 出现污水词族时，必须验证数据集真的声明了 T13；命中才算这条轴被验证，
    # 没命中就是明确的缺口，而不是「部分满足」。
    #
    # 子类校验（0.15 百问循环）：T09 一个 L1 装着空气/水/固废/生态，只比
    # L1 会把「塑料垃圾」判成二氧化碳的对题。需求与卡**都带子类**时必须
    # 相交；任一方只有 L1（词表兜底的卡没有真子类证据）就退回 L1 比较。
    t_need = list(need.get("topics") or [])
    need_subs = set(need.get("subtopics") or [])
    # 同族子类互通：碳排放同时被 S0901「空气与排放」与 S0905「气候与碳」
    # 认领，两侧都合法（见 taxonomy.SUB_ALIAS 的来由）。按「直交」比会
    # 把一份 CO₂ 数据判成主题缺口，只因为 agent 在地图上选了另一个同样
    # 合法的节点。互通用的是**声明出来的**族关系，不是放松整条校验：
    # 固废 S0904 不在族里，塑料垃圾仍然不会被判成二氧化碳。
    need_subs_eff: set[str] = set()
    for s in need_subs:
        need_subs_eff |= tx.sub_family(s)
    alias_hit: set[str] = set()
    topic_undeclared = False
    if t_need:
        d_top = [str(x) for x in (decl.get("topics") or [])]
        got_t = [t for t in t_need if t in d_top]
        d_subs = set(decl.get("subtopics") or [])
        # 名片根本没有主题轴：这是「没声明」，不是「不覆盖」（0.28 全量回归
        # 实测：244 条里 203 条只有行业/主体轴）。按缺口处理会把整个主路径
        # 变成「只有 41 条能被找到」——肠旺面这类明明由行业+主体接得住的
        # 提问会被判成货架没有。按 unknown 处理，并在 complete 上否决，
        # 让 agent 看到「行业对上了、主题没声明，别当成完全满足」。
        if not d_top and not d_subs:
            topic_undeclared = True
        elif got_t and need_subs and d_subs and not topic_inferred and not (need_subs_eff & d_subs):
            got_t = []
            missing.append("主题（要"
                           + "、".join(tx.L1_NAME.get(t, t) for t in t_need)
                           + f"的 {'/'.join(sorted(need_subs))}，它是 {'/'.join(sorted(d_subs))}）")
        elif got_t and need_subs and d_subs and not (need_subs & d_subs):
            alias_hit = need_subs_eff & d_subs
        if got_t:
            lab = "主题=" + "、".join(tx.L1_NAME.get(t, t) for t in got_t)
            if alias_hit:
                lab += ("（子类同族互通：" + "/".join(sorted(need_subs))
                        + " ⇄ " + "/".join(sorted(alias_hit)) + "）")
            if topic_inferred:
                # 推断轴：只给一个弱到不足以翻盘的分数，不进 verified。
                # 否则 176 条补轴卡会集体变成「主题命中」的强证据，把字面
                # 相近但口径不对的条目抬到 top1（0.29 A/B/C 实测）。
                score += 0.5 * len(got_t)
                weak_axes.add("topic")
                hit.append(lab + "（推断，未实测，不作对题证据）")
            elif topic_mapped:
                # 源方分类映射：有源方给的库名/分组作依据，但那是「一次
                # 翻译」不是实测字段，给中间权重、同样不进 verified。
                score += 2.0 * len(got_t)
                weak_axes.add("topic")
                hit.append(lab + "（源方分类映射，非实测，不作完全对题证据）")
            else:
                score += 4.0 * len(got_t)
                verified.add("topic")
                hit.append(lab)
        elif topic_undeclared or topic_inferred or topic_mapped:
            # 推断主题同样不是证据：**对不上也不能制造缺口**。否则给
            # 「贵阳肠旺面」这类靠行业+主体接得住的条目补了 T07，need 是
            # T02，就会被硬记一条「主题（要…）」并扣 3 分——比补轴前
            # （卡未声明主题，不计分不扣分）更差。
            pass
        elif "主题（要" not in "".join(missing):
            score -= 3.0
            missing.append("主题（要" + "、".join(tx.L1_NAME.get(t, t) for t in t_need) + "）")

    # 地理：声明范围要能盖住问的地方
    g = need.get("geo") or ""
    d_geo = decl.get("geo") or ""
    if g and d_geo:
        if geo_covers(d_geo, g):
            score += 2.0
            verified.add("geo")
            hit.append(f"范围含{g}")
        else:
            missing.append(f"地理（要{g}，它只有{d_geo}）")
    # 街级约束（「衡山路附近」）：国家/城市级覆盖**不等于**街级覆盖。
    # 判据看真实字段，不看修辞。0.21 修正：BeaconMFG 其实**有** lat/lng
    # （2026-11-09 用 partner 身份实测 /v1/search，字段清单里此前漏了），
    # 所以不能再判「只到城市」。但它这层坐标是推断值（pv=derived），抽查
    # 发现「星巴克(七宝宝龙城中庭店)」被标到衡山路旁、同广场其他店却在
    # 闵行——**有坐标 ≠ 坐标可信**。缺的是精度，不是能力，文案必须分开写：
    # 前者让 agent 去筛了再复核，后者会让它直接放弃这条路。
    gd = need.get("geo_detail") or ""
    advisories: list[str] = []
    if gd and g and d_geo and geo_covers(d_geo, g):
        _fl = fields if fields is not None else (decl.get("fields") or [])
        _fnames = " ".join(str((f or {}).get("name") or "")
                           for f in _fl if isinstance(f, dict))
        has_point = bool(_fnames) and bool(re.search(
            r"address|addr|street|road|doorplate|zip|postal|"
            r"\blat\b|\blon\b|\blng\b|latitude|longitude|geom|coordinate|"
            r"地址|门牌|街道|路名|经纬|坐标|定位", _fnames, re.I))
        # 声明自己说坐标是推断/估计值的（granularity 里写清）。
        _derived = bool(re.search(r"推断|估计|近似|derived|estimated",
                                  str(decl.get("granularity") or ""), re.I))
        if not has_point and _fnames:
            # 一个点字段都没有：这不是精度问题，是**能力**问题——源方给不出
            # 可定位的记录，agent 拿回去也没法圈街。留在 missing 里。
            missing.append(f"位置（要{gd}的街级范围，它只到{d_geo}）")
        elif has_point and _derived:
            # 有坐标但是推断值（0.22）：这是**数据质量**，不是能力缺口。
            # 平台只负责指路与告知，筛不筛得准由调用方按源方规范自行处理
            # ——写进 missing 会让 agent 以为这条路不成立，反而放弃唯一
            # 对题的源（0.21 把 BeaconMFG 判成缺「位置精度」，方向错了）。
            advisories.append(f"{gd}街级筛选需复核：该源坐标为推断值（源方 pv=derived）")
    # 声明没写地理的，不加分也不扣分：没说胜过说错

    # 「没解析出这条轴」和「对上了这条轴」是两件事，绝不能都算通过
    unknown_axes: list[str] = []
    if not e_need:
        unknown_axes.append("主体")
    if not s_need:
        unknown_axes.append("行业")
    if topic_undeclared:
        unknown_axes.append("主题（卡未声明）")
    elif topic_inferred:
        unknown_axes.append("主题（推断，未实测）")
    elif topic_mapped:
        unknown_axes.append("主题（源方分类映射，未实测）")

    strong = verified & {"entity", "sector"}
    # 0.39 §7b：需求只给了「各国 / 某地区」这种**泛范围主体**，且没有任何
    # 行业/主题轴对上时，仅凭「产量」这类度量轴命中不能算接住。
    # 实证：「各国粮食产量 小麦」首条是世行《Total electricity output
    # (GWh)》，两者唯一的交集是 output 这个度量，却因 entity=country +
    # measure=output 被判 strong——0.15 加的「至少一条内容轴」把 measure
    # 也算进去了，在 3.5 万条语料上被泛指标压穿。
    # 豁免：需求里明写的拉丁专名在条目名里复现（「税收收入 占GDP 各国」→
    # 《Tax revenue (% of GDP)》），说明名字真的对题，不是靠一个通用度量词
    # 蹭上来的。0.39 实测：不豁免会把这条真数据一起打成 mismatch + 货架没有，
    # 而「各国粮食产量 小麦」→《Total electricity output (GWh)》没有这个
    # 交集，仍然被否决。两者需求结构完全同形，只有这一条硬证据能分开。
    generic_scope_only = (bool(e_need)
                          and all(e in ("country", "region") for e in e_need)
                          and (not verified_entities
                               or all(e in ("country", "region")
                                      for e in verified_entities))
                          and not (verified & {"sector", "topic"})
                          and not _name_identity_hit(need.get("raw") or "", name))
    # 「主体没解析出来」必须一票否决：不知道 agent 要的是一条条企业、一个
    # 地区还是一个统计序列，就不可能证明「完全满足」。BeaconMFG 声明覆盖
    # GB/T 全门类，只靠行业轴 + 一条登记度量就能凑够两条 verified，于是
    # 「博物馆馆藏文物清单」「地表水水质监测断面」都会被它判成完全满足。
    # 行业轴缺席可以容忍（「查一家公司的电话和地址」没有行业词，但主体 +
    # 两条度量足以确认），主体轴缺席不行。
    #
    # 0.15 百问循环再加一道：complete 必须至少验证一条**内容轴**
    # （度量 / 行业 / 主题）。「全球森林覆盖率」的需求只解析出主体 country +
    # 地理全球——两个都是「范围」轴，一条内容都没对上，OWID 二氧化碳、
    # 儿童死亡率、电力接入全都 missing 为空 + verified={entity, geo}，
    # 凑够「两条轴」亮绿灯。entity+geo 只证明「范围搭」，不证明「内容对」。
    complete = bool(not missing and strong and len(verified) >= 2
                    and not generic_scope_only
                    and "主体" not in unknown_axes
                    and not topic_undeclared
                    # 推断主题不能替代「卡真的声明了主题」：补轴只是让候选
                    # 可被召回和解释，不能解锁「完全满足」。但只在提问真的
                    # 带了主题轴时才否决——一条没有主题坐标的提问（「军工
                    # 企业名录」）本来就不比主题，不能因为卡补了推断主题
                    # 反而把真实的完全匹配打死。
                    and not (topic_inferred and bool(t_need))
                    and verified & {"measure", "sector", "topic"})
    # 结构轴（主体/行业/地理/主题）全对、只缺「附加信息」（口碑/联系方式）
    # 时，这条路仍然是对的——「民宿推荐几家」的本质是找住宿业门店，全行业
    # 商户名录接得住主体和行业（H 类实测样本就在 hit 里），只是没有评价
    # 数据。判 partial 让 agent 看见「路对但缺什么」；判 mismatch 等于说
    # 「这不是这条路」，方向反了（0.19 实测：字面最强的 BeaconMFG 被
    # shelf_gap 劝退）。
    #
    # 缺**核心度量**（价格/产量/税收/入住率…）时，只有在结构身份被确认
    # （主体 + 行业双命中，或主题命中）才容忍为 partial：那说明它确实是
    # 这类主体/这个行业的数据，只是没有那个指标，agent 可以据此找替代
    # 口径或明确告知缺什么。只靠地理/度量凑上来的（CPI→Comtrade、
    # 粮食产量→能源数据）仍是 mismatch：那不是这条路（0.19 百问审查
    # 抓到的误伤，别为了放行一条而放松全部）。
    identity_ok = (("entity" in verified and "sector" in verified)
                   or "topic" in verified)
    # 街级精度缺口不否决整条路：城市级名录确实能给出「上海有哪些咖啡馆」，
    # 只是筛不到「衡山路附近」。它和「缺口碑」同类——路对，细节精度不够。
    # 核心度量（价格/产量/入住率）缺了才是真的答不了。
    core_missing = [x for x in missing
                    if not x.startswith("数据（缺") and "街级" not in x]
    # §7b 修订（0.39 全量回归）：泛范围主体只否决**绿灯**（complete/strong），
    # 不能顺手把「纯范围命中」从 partial 打成 mismatch。0.15 的
    # test_scope_only_axes_cannot_light_the_green_light 锁的承诺是
    # 「entity+geo 不许亮绿灯，但仍算路对、只是缺内容」——需求里一条内容轴
    # 都没带（verified 只有 entity+geo）时给 partial 才是对的。
    # 真正要打掉的是「需求带了度量、唯一验证到的内容轴又只是 measure」：
    # 「各国粮食产量 小麦」→《Total electricity output (GWh)》只共用一个
    # output，判 partial 等于说这条路对，必须保持 mismatch。
    # generic_scope_only 的定义已排除 sector/topic 命中，所以这里等价于
    # 「泛范围 + 只蹭到度量」。
    scope_measure_only = generic_scope_only and "measure" in verified
    quality = ("strong" if complete else
               "partial" if (verified and not scope_measure_only
                             and not core_missing
                             and (not lost_core or identity_ok)) else
               "mismatch" if verified else "weak")

    if score <= 0:
        return None
    why = "；".join(hit) + ("；但缺 " + "、".join(missing) if missing else "")
    if advisories:
        why += "；使用时注意 " + "；".join(advisories)
    if unknown_axes:
        why += "；未验证 " + "、".join(unknown_axes)
    return {
        "score": round(score, 2),
        "hit": hit,
        "missing": missing,
        # 不影响「这条路对不对」的使用提示：数据质量、口径、精度。
        # 平台只负责告知，怎么处理由调用方按源方规范决定（0.22）。
        "advisories": advisories,
        "why": why,
        "complete": complete,
        "verified_axes": sorted(verified),
        "weak_verified_axes": sorted(weak_axes),
        "axis_evidence": {"topic": prov.get("topics", "undeclared")},
        "unknown_axes": unknown_axes,
        "quality": quality,
    }
