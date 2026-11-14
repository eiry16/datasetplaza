"""Deterministic cross-dataset routing.

The platform does NOT ask an LLM to read every dataset's self-introduction
and guess. It runs one cheap content-level query across all registered
datasets, aggregates hits per dataset, and blends in the declared
representative_queries as an intent signal. The agent only arbitrates the
returned shortlist. No values are returned at this stage.
"""
from __future__ import annotations

import json
import sqlite3

from .errors import fail, ok
from .index import query_fts_rows
from .listings import _lexicon_expand
from .text import fts_terms, literal_overlap, match_expr, overlap, terms_of


def _literal_rows(con: sqlite3.Connection, terms: list[str], cap: int = 600):
    """字面扫描，给 trigram 看不见的 2 字词用。

    2 字词不可能是 trigram，MATCH 永远 0 行——「汤面」这类中文基本词因此
    一条都命不中。回表 LIKE 是唯一还能用的确定性手段，代价是全表扫
    body（当前约 4.4 MB / 13 万行，几十毫秒），换来的是不再把正常中文问题
    判成「没这个词」。

    OR 而不是 AND：三个词同时出现在同一个 cell 里几乎不可能（那是值，不是
    段落），按覆盖率打分比「只有第一个词算数」诚实。
    """
    sql = ("SELECT citation, dataset_id, body FROM cells_fts WHERE "
           + " OR ".join(["body LIKE ?"] * len(terms)) + " LIMIT ?")
    rows = con.execute(sql, [f"%{t}%" for t in terms] + [cap]).fetchall()

    best: dict[str, tuple[int, str]] = {}
    for r in rows:
        body = (r["body"] or "").lower()
        covered = sum(1 for t in terms if t.lower() in body)
        prev = best.get(r["dataset_id"])
        if covered and (prev is None or covered > prev[0]):
            best[r["dataset_id"]] = (covered, r["citation"])
    # 折算成与 bm25 同量级的正分：字面匹配永远弱于真正的索引命中
    # （bm25 常在 3–20），所以上限压到 8。
    return [(cite, ds_id, min(8.0, 2.5 * covered))
            for ds_id, (covered, cite) in best.items()]


def route(con: sqlite3.Connection, query: str, top_n: int = 3) -> dict:
    terms, dropped = terms_of(query)
    warnings: list[str] = []
    if dropped:
        warnings.append("single-character terms ignored: " + ", ".join(dropped))
    # 策展词表扩展（与 discover 同源）：中文 query 经 CN→EN 词面扩展后才可能
    # 命中外文索引内容（如「能源」→energy）。此前只接了 discover，route 漏接，
    # 中文提问对已索引外文数据集永远 0 内容命中——架构不一致，2026-11-07 补齐。
    for t in _lexicon_expand(query.lower()):
        if t not in terms:
            terms.append(t)
    if not terms:
        return fail("empty_query", warnings=warnings)

    searchable = fts_terms(terms)
    short = [t for t in terms if len(t) < 3]
    match_mode = "and"
    rows: list[tuple[str, str, float]] = []
    if searchable:
        rows = query_fts_rows(con, match_expr(searchable, "and"), None, 300)
        if not rows and len(searchable) > 1:
            rows = query_fts_rows(con, match_expr(searchable, "or"), None, 300)
            match_mode = "or"
            warnings.append("no conjunctive hit; relaxed to OR")
    if short:
        rows.extend(_literal_rows(con, short))
        warnings.append(
            "2-character terms matched literally, not via the trigram index: "
            + ", ".join(short))

    by_ds: dict[str, list[tuple[str, float]]] = {}
    for citation, ds_id, score in rows:
        by_ds.setdefault(ds_id, []).append((citation, score))

    candidates = []
    for ds in con.execute("SELECT * FROM datasets").fetchall():
        ds_id = ds["dataset_id"]
        hits = by_ds.get(ds_id, [])
        rqs = json.loads(ds["representative_json"])
        field_desc = " ".join(
            f.get("description", "") for f in json.loads(ds["fields_json"])
        )
        intent = max(
            [overlap(query, q) for q in rqs]
            + [overlap(query, ds["name"]) * 0.8, overlap(query, field_desc) * 0.5]
        )
        if short:
            # 字面命中声明是弱证据，但比 0 诚实：它说明这个数据集确实提到
            # 了这几个词，只是词太短、进不了索引。
            literal = max(
                [literal_overlap(terms, q) for q in rqs]
                + [literal_overlap(terms, ds["name"]) * 0.8,
                   literal_overlap(terms, field_desc) * 0.5]
            )
            intent = max(intent, round(literal, 4))
        content_score = max([s for _c, s in hits], default=0.0)
        score = round(
            (10.0 if hits else 0.0) + min(content_score, 20.0) + intent * 3.0, 4
        )
        top_fields = []
        for citation, _s in hits[:8]:
            cell = con.execute(
                "SELECT column_name FROM cells WHERE citation = ?", (citation,)
            ).fetchone()
            if cell and cell["column_name"] not in top_fields:
                top_fields.append(cell["column_name"])
        acc = json.loads(ds["access_json"])
        candidates.append(
            {
                "dataset_id": ds_id,
                "name": ds["name"],
                "score": score,
                "has_content_hit": bool(hits),
                "hit_count": len(hits),
                "top_fields": top_fields,
                "intent_overlap": intent,
                "claim_status": ds["claim_status"],
                "access_level": ds["access_level"],
                "price": acc.get("price", {}),
                "publisher_level": ds["publisher_level"],
            }
        )

    # 0 分候选不是「可能相关」，是「完全没命中」。以前照样返回 3 条，
    # agent 于是对着 worldbank 电力数据回答苏州汤面 —— 宁可给空列表加
    # 一句解释，也不要给它一个可以去证明的错误目标。
    total_datasets = len(candidates)
    candidates = [c for c in candidates if c["score"] > 0]
    candidates.sort(key=lambda c: (not c["has_content_hit"], -c["score"]))
    shortlist = candidates[:top_n]
    if not shortlist:
        warnings.append(
            "no registered dataset declares or contains these terms; "
            "try dp_discover / dp_catalog to see what the shelf has")
    elif not any(c["has_content_hit"] for c in shortlist):
        warnings.append("no registered dataset has a content hit for this query")
    # 0.13：OR 降级命中的诚实标注。跨领域实测里「发明专利」报 7/8 数据集
    # 有内容命中、top 却是世行电力数据，「电影票房」命中儿童死亡率 212 条，
    # 都是年份/「中国」/「统计」这类泛词经 OR 放宽后造成的。召回逻辑不动
    # （8 组预标注评测依赖它），但必须让下游看得见「这是放宽后的命中」。
    relaxed = match_mode == "or"
    if relaxed:
        warnings.append(
            "OR 降级：内容命中可能只来自年份/国家/『统计』等泛词，"
            "不代表数据集内容与问题相关；请用 dp_discover 复核目录货架")
    return ok(
        query=query,
        match_mode=match_mode,
        all_datasets=total_datasets,
        datasets_with_content_hits=len(by_ds),
        datasets_with_exact_content_hits=(0 if relaxed else len(by_ds)),
        index_hit_reliability=("relaxed_or" if relaxed else "exact_and"),
        relaxed=relaxed,
        matched_terms=terms,
        candidates=shortlist,
        free=True,
        charged=0.0,
        deterministic=True,
        warnings=warnings,
    )
