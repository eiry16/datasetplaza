"""Deterministic text helpers. No segmenter, no model, no network.

Chinese queries are split on punctuation and whitespace into keyword
phrases; SQLite FTS5 trigram does substring matching on each phrase.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[\s,，、;；:：?？!！。()（）\[\]【】\"'“”‘’/]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def min_term_len(term: str) -> int:
    """2 for CJK, 3 for Latin/digits.

    A flat 3-character floor is an English rule applied to Chinese. 汤面 /
    面馆 / 名录 are whole words, and dropping them made every all-short
    Chinese query return empty_query — the most common query shape there is.
    Single CJK characters stay dropped: one character matches half the
    corpus and says nothing about intent.
    """
    return 2 if _CJK.search(term or "") else 3


def terms_of(query: str) -> tuple[list[str], list[str]]:
    raw = [t.strip() for t in _SPLIT.split((query or "").strip()) if t.strip()]
    usable = [t for t in raw if len(t) >= min_term_len(t)]
    dropped = [t for t in raw if len(t) < min_term_len(t)]
    return usable, dropped


def fts_terms(terms: list[str]) -> list[str]:
    """Only these can reach the trigram index; it needs 3+ characters.

    Kept separate from terms_of() so a 2-char word still counts as a term
    (it can be matched literally) while never being fed to MATCH.
    """
    return [t for t in terms if len(t) >= 3]


def literal_overlap(terms: list[str], declaration: str) -> float:
    """Share of terms appearing verbatim in a declaration, 0..1.

    The trigram overlap() cannot score a 2-char term at all, so short queries
    would look like "no relation" to every dataset. Substring presence is the
    honest weak signal: it says the dataset mentions 汤面, nothing more.
    """
    d = (declaration or "").lower()
    if not terms or not d:
        return 0.0
    hit = sum(1 for t in terms if t and t.lower() in d)
    return round(hit / len(terms), 4)


def match_expr(terms: list[str], mode: str = "and") -> str:
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms]
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(quoted)


def _grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text or "")
    return {s[i : i + 3] for i in range(max(0, len(s) - 2))}


def overlap(query: str, declaration: str) -> float:
    """Share of query 3-grams covered by a declaration, 0..1.

    denominator is min(), not union: a short query fully contained in a
    longer representative query should score 1.0.
    """
    q = re.sub(r"\s+", "", query or "")
    if len(q) < 3:
        # No trigram exists for a 1-2 char query; containment is the only
        # signal available, and returning 0.0 would erase the match.
        return 1.0 if q and q in re.sub(r"\s+", "", declaration or "") else 0.0
    a, b = _grams(query), _grams(declaration)
    if not a or not b:
        return 0.0
    return round(len(a & b) / min(len(a), len(b)), 4)
