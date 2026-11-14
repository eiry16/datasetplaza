"""货架层倒排召回：把 discover 的「全量精算」换成「先召回、再精排」（0.36）。

与 `bdsp/router.py` 的区别：那条是**内容层**路由（在已建索引的 cells 上查值，
走 SQLite FTS），只覆盖少数真被抓过的数据集；这里的对象是**货架层**——
3.5 万条目录条目的名称与坐标，绝大多数没有内容索引，只能靠元数据指路。

## 为什么需要这一层

0.35 的 2,027 条，`discover` 一问 0.6s，能忍。接完 World Bank（29,544）与
香港（3,885）之后是 3.5 万条，同一条路径会变成 8–12s——因为它对**每一条**
都跑 normalize + capability.match + shelf_tier + 六个字段拼 hay + 四次子串
扫描 + profile.build + validate。这不是常数因子问题，是复杂度问题。

## 做法

两段式：

1. **召回（本模块）**：预建词元 → 条目下标的倒排表（中文 2-gram、英文整词），
   提问时按 IDF 累加取 top-K（默认 400，覆盖 3.5 万条的 1.1%）。
2. **精排**：listings.discover 原逻辑不动，只换作用域。

召回只要保证「不漏」，允许粗糙；「是不是这条路」仍由能力坐标判。

## 顺带解决「通用词压过专名」

倒排表自带 `df`（每个词出现在多少条里），IDF 白拿。0.33 报告里那条
「问『贵州有哪些苗语语料』，首条是《石斑鱼高精度标注数据集》——只因域名里
有『语料』二字」的根因，是字面分用了**固定权重**（名称命中恒 3.0），`语料`
这种几千条里都有的词和 `苗语` 这种只出现一次的词同样值钱。`idf_weight()`
给精排提供修正系数，按需启用（默认关，避免动既有回归）。

## 边界

- 索引是派生数据，可由 `listings.json` 随时重建，不进 git。
- 按 mtime 校验失效，过期自动重建。
- **召回不含语义**：「营业执照核验」vs「市场主体登记信息」这类说场景不报名
  字的提问，倒排救不了——那是能力卡 `asks` 轴的事，别在这里假装解决。
"""
from __future__ import annotations

import math
import pickle
import re
from pathlib import Path

VERSION = "rc-1"
DEFAULT_K = 400

_TOKEN = re.compile(r"[A-Za-z0-9_.\-]{2,}|[\u4e00-\u9fff]{2,}")
_CN = re.compile(r"^[\u4e00-\u9fff]+$")

# 中文 2-gram 会切出大量「信息/数据/服务」这类词，在 3.5 万条里遍地都是，
# 召回阶段只会拖慢、不会筛出东西。留在索引里（IDF 自然把它们压到接近 0），
# 但不当独立检索键。
_NOISE = frozenset("""信息 数据 服务 管理 平台 系统 中心 项目 情况 统计 查询 接口
资源 目录 名单 明细 记录 相关 综合 公共 基础 建设 发展 应用 业务 内容""".split())


def terms_of(text: str) -> list[str]:
    """文本 → 检索词元。中文段切 2-gram（无分词器时唯一确定性的做法）。"""
    out: list[str] = []
    for t in _TOKEN.findall(str(text or "").lower()):
        if _CN.match(t) and len(t) >= 4:
            for i in range(len(t) - 1):
                g = t[i:i + 2]
                if g not in _NOISE:
                    out.append(g)
        elif t not in _NOISE:
            out.append(t)
    return out


def item_terms(item: dict) -> list[str]:
    """索引一条目时的词元面：名称为主，domain 与发布方补召回。

    刻意**不索引 notes**：贵州那 1,783 条的 notes 都含「贵州省公共数据平台
    门户目录在册条目」，索引进去会让它们在几乎任何提问下互相召回，等于没筛。

    0.41 加 locator_terms（表内指标名）。这是 0.40 盲测里「进前 10 排不到第一
    / 完全没召回」的直接对症：用户问「摩托车交通事故死了多少人」，源方条目名
    只有「交通事故死亡人数」，指标名只存在于定位器里——不索引就永远指不到
    那张表。指标名是**源方声明的结构化文本**，不是自由说明，所以不会重演
    notes 的污染。
    """
    name = str(item.get("name") or "")
    dom = " ".join(str(d) for d in (item.get("domain") or []))
    pub = str(item.get("publisher") or "")
    lt = item.get("locator_terms")
    if not lt:
        loc = item.get("locator") or {}
        lt = [x.get("name") for x in (loc.get("indicators") or [])
              if isinstance(x, dict) and x.get("name")]
    extra: list[str] = []
    for nm in (lt or [])[:40]:
        extra += terms_of(nm)
    return list(dict.fromkeys(terms_of(name) + terms_of(dom) + terms_of(pub) + extra))


def _axis_key(kind: str, value) -> str:
    return "%s:%s" % (kind[:-1] if kind.endswith("s") else kind, value)


def axis_keys(item: dict) -> list[str]:
    """结构化坐标 → 倒排键。agent 走 need 主路径时直接查这些池子。"""
    cap = item.get("capability") or {}
    keys = [_axis_key(k, v) for k in ("topics", "subtopics", "sectors",
                                      "entities", "measures")
            for v in (cap.get(k) or [])]
    geo = str(cap.get("geo") or "").strip()
    if geo:
        keys.append("geo:%s" % geo)
    return keys


def build(items: list[dict]) -> dict:
    """建索引。3.5 万条约 2s，只在货架变动后跑一次。"""
    from .listings import _name_key   # 单向依赖：listings 不 import recall

    postings: dict[str, list[int]] = {}
    axes: dict[str, list[int]] = {}
    names: list[str] = []
    name_keys: list[str] = []
    ids: list[str] = []
    for idx, it in enumerate(items):
        names.append(str(it.get("name") or ""))
        ids.append(str(it.get("dataset_id") or ""))
        name_keys.append(_name_key(str(it.get("name") or "")))
        for t in item_terms(it):
            postings.setdefault(t, []).append(idx)
        for k in axis_keys(it):
            axes.setdefault(k, []).append(idx)
    return {"version": VERSION, "n": len(items),
            "postings": postings, "axes": axes, "names": names,
            "name_keys": name_keys, "ids": ids}


def save(index: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        pickle.dump(index, f, protocol=pickle.HIGHEST_PROTOCOL)


def load(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            idx = pickle.load(f)
    except Exception:                                            # noqa: BLE001
        return None
    return idx if isinstance(idx, dict) and idx.get("version") == VERSION else None


def fresh(index_path: str | Path, listings_path: str | Path) -> bool:
    p, lp = Path(index_path), Path(listings_path)
    if not p.exists() or not lp.exists():
        return False
    return p.stat().st_mtime >= lp.stat().st_mtime


class Recall:
    """召回器。两件事：给候选下标、给 IDF 权重。"""

    def __init__(self, index: dict):
        self.postings: dict[str, list[int]] = index["postings"]
        self.axes: dict[str, list[int]] = index["axes"]
        self.n: int = index["n"]
        self.names: list[str] = index["names"]
        self.ids: list[str] = index.get("ids") or []
        self._name_keys: list[str] | None = index.get("name_keys")
        self._name_counts = None

    def name_counts(self):
        """同名兄弟计数（_name_key 归一）。

        discover 原本每条目算一次 `_name_key` 再 Counter，3.5 万条约 0.3s；
        索引里已经存了归一后的名字，直接 Counter 一次即可（约 5ms），
        且缓存在 Router 实例上，一问只算一次。
        """
        if self._name_counts is None:
            from collections import Counter
            if not self._name_keys:
                from .listings import _name_key
                self._name_keys = [_name_key(n) for n in self.names]
            self._name_counts = Counter(self._name_keys)
        return self._name_counts

    def df(self, term: str) -> int:
        return len(self.postings.get(term) or ())

    def idf(self, term: str) -> float:
        return math.log(1.0 + self.n / (1.0 + self.df(term)))

    def idf_weight(self, term: str) -> float:
        """精排用的字面权重系数，归一到 1.0 附近。

        「苗语」(df=1) ≈ 1.0，「语料」(df=800) ≈ 0.37，「数据」(df=2 万) ≈ 0.1。
        不改变分数尺度，只改变同词不同稀缺度的相对价值。
        """
        return self.idf(term) / max(1e-9, math.log(1.0 + self.n))

    @staticmethod
    def need_keys(need: dict) -> list[str]:
        keys = [_axis_key(k, v) for k in ("topics", "subtopics", "sectors",
                                          "entities", "measures")
                for v in (need.get(k) or [])]
        geo = str(need.get("geo") or "").strip()
        if geo:
            keys.append("geo:%s" % geo)
        return keys

    def recall(self, query: str, need: dict | None = None,
               k: int = DEFAULT_K, expand: list[str] | None = None) -> list[int]:
        """top-K 条目下标，按命中强度降序。

        打分 = Σ IDF(词元) + 0.8Σ IDF(扩展词) + 6×轴命中数。轴权重远高于字面：
        结构化坐标比词面可信（0.18 定的主路径优先）。
        """
        scores: dict[int, float] = {}
        for t in terms_of(query):
            for i in self.postings.get(t) or ():
                scores[i] = scores.get(i, 0.0) + self.idf(t)
        for t in expand or ():
            for i in self.postings.get(t) or ():
                scores[i] = scores.get(i, 0.0) + self.idf(t) * 0.8
        if need:
            # 轴命中权重必须按「这个轴有多窄」缩放。固定 +6 会被同构源打穿：
            # World Bank 2.9 万条共享同一批 topic/measure，任何一个泛轴命中
            # 都等于给它们全体 +6，400 个召回名额瞬间占满——实测「企业失信
            # 情况」原本排第一的贵州条目因此掉出召回集。
            # 分母用 log1p(df)：df=1 的轴仍拿 ~6 分，df=2.9 万的轴只值 0.6。
            for key in self.need_keys(need):
                docs = self.axes.get(key) or ()
                if not docs:
                    continue
                w = 6.0 / (1.0 + math.log1p(len(docs)))
                for i in docs:
                    scores[i] = scores.get(i, 0.0) + w
        if not scores:
            return []
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
        return [i for i, _ in top]

    def stats(self) -> dict:
        sizes = [len(v) for v in self.postings.values()]
        return {"n": self.n, "terms": len(self.postings),
                "axis_keys": len(self.axes), "postings": sum(sizes),
                "avg_df": round(sum(sizes) / max(1, len(sizes)), 1) if sizes else 0,
                "max_df": max(sizes) if sizes else 0}


def get_or_build(index_path: str | Path, listings_path: str | Path,
                 items: list[dict], force: bool = False) -> Recall:
    if not force and fresh(index_path, listings_path):
        idx = load(index_path)
        if idx and idx.get("n") == len(items):
            return Recall(idx)
    idx = build(items)
    save(idx, index_path)
    return Recall(idx)
