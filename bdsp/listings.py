"""Catalogue-only shelf entries ("目录条目").

A listing says: *this dataset exists, here is the source, here is its measured
reachability, here is how to get access*. It explicitly does NOT say that the
platform holds its content. Therefore listings never enter the FTS index,
never appear in ``route`` / ``prove``, and never produce a proof packet.

This is the honest way to put a large shelf in front of an agent: discovery is
free and broad, existence proof is narrow and only available for indexed data.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import catalog
from . import enrich
from . import capability as capmod
from . import forms as formsmod
from . import locator as locatormod
from . import map as mapmod
from . import profile as profilemod
from . import sectors as sectormod
from . import source_terms as termsmod
from . import taxonomy as tx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LISTINGS = ROOT / "listings" / "listings.json"

def status_label(i: dict) -> str:
    """可用性优先的状态标签（操作员定稿）。

    对使用者来说，路牌条目的重点是「能不能用、怎么用」——索引状态是平台的
    内部事实，只作括注，不再用「未接入」这种像故障的否定式表述。
    可用性判断依据巡检探针的实测结果，不凭条目自称。
    """
    st = (i.get("probe") or {}).get("upstream_status") or "unchecked"
    if st == "reachable":
        return "可用 · 经源方取数（平台未建索引）"
    if st == "auth_failed":
        return "可用 · 需源方凭证（平台未建索引）"
    if st == "unchecked":
        return "可用性待检（平台未建索引）"
    return "源方暂不可达（平台未建索引）"


def availability(i: dict) -> str:
    """可用性说明：目录条目没接入索引 ≠ 不可用，如实告知取数途径。"""
    if i.get("linked_dataset_id"):
        return "已接入索引（%s）" % i["linked_dataset_id"]
    contact = (i.get("developer") or {}).get("contact", "")
    if "mcp" in contact.lower():
        return "可经源方 MCP 使用（%s）；平台暂无索引，不参与 route/proof" % contact
    url = i.get("data_url") or i.get("source_url") or i.get("catalog_url")
    if url:
        return "可经源方渠道使用（%s）；平台暂无索引，不参与 route/proof" % url
    return "平台暂无索引，不参与 route/proof"


def field_doc_review(item: dict, _normalized: dict | None = None) -> dict:
    """字段文档审查（收录/体检时逐字段核查）。

    回答一个问题：agent 不用点开源方，能不能预先看懂「每列是什么」。
    只查文档完备性，不猜字段含义；缺失如实标注并给出提示，绝不代填。
    等级：documented 齐全 / partial 部分缺失 / undocumented 未声明或全缺说明。

    ``_normalized`` 是给 ``record()`` 用的快路径：它已经 normalize 过一次，
    3.5 万行时再 normalize 一遍纯属白付（实测占 record() 的 12%）。
    """
    i = _normalized if _normalized is not None else normalize(item)
    fields = i.get("fields") or []
    if not fields:
        return {
            "level": "undocumented", "total": 0, "with_desc": 0, "with_example": 0,
            "coverage": 0.0, "missing": [],
            "note": "未声明字段字典（目录条目允许留空，但 agent 无法预知能拿到什么列）",
            "warning": "字段文档缺失：建议在收录审查时向提交方索取 name/description 字段说明",
        }
    # 只有列名、没有释义（0.35）：贵州省公共数据平台的门户条目就公开了信息项
    # 清单（「包含序号、所属区县、制卡银行…等信息项」），但不给每列的释义与
    # 口径。这比「没有字段」好一档，但绝不能算 documented——agent 知道有哪些
    # 列，仍然不知道「联系电话」是本人手机还是单位固话。
    if all((f.get("provenance") or "") == "portal_description" for f in fields):
        return {
            "level": "names_only", "total": len(fields), "with_desc": 0,
            "with_example": 0, "coverage": 0.0,
            "missing": [f.get("name", "") for f in fields],
            "note": f"{len(fields)} 列只有列名（源方公开的信息项清单），无逐列释义",
            "warning": "有列名无释义：知道有哪些列，不知道单位与口径；"
                       "取到数后需按列值自行判断，别按列名当口径",
        }
    missing = [f.get("name", "") for f in fields
               if not (f.get("description") or "").strip()]
    with_desc = len(fields) - len(missing)
    with_example = sum(1 for f in fields if (f.get("example") or "").strip())
    coverage = round(with_desc / len(fields), 2)
    if not missing:
        level, note, warning = "documented", f"字段文档齐全（{len(fields)} 列均有说明）", ""
    elif with_desc:
        level = "partial"
        note = f"{with_desc}/{len(fields)} 列有说明；缺：{', '.join(missing[:6])}" \
               + ("…" if len(missing) > 6 else "")
        warning = "字段说明不全：补齐缺失列的 description 可显著提升 agent 取数意愿"
    else:
        level, note = "undocumented", f"{len(fields)} 列均无说明"
        warning = "字段文档缺失：建议在收录审查时向提交方索取 name/description 字段说明"
    return {
        "level": level, "total": len(fields), "with_desc": with_desc,
        "with_example": with_example, "coverage": coverage, "missing": missing,
        "note": note, "warning": warning,
    }
EVIDENCE_LABELS = {
    "official_api": "官方 API 实测",
    "official_endpoint_probe": "官方端点实测",
    "official_page_snapshot": "官方页面快照",
    "official_press_screenshot": "官方通稿截图",
    "operator_curated": "操作员登记（接口未核验）",
    "third_party_aggregator": "第三方聚合（弱证据）",
    "": "未标注",
}
WEAK_EVIDENCE = {"third_party_aggregator", "operator_curated", ""}

_TOKEN = re.compile(r"[A-Za-z0-9_.\-]{2,}|[\u4e00-\u9fff]{2,}")

# 同一系列在候选里最多占几席（0.38）。2 席 = 允许 agent 看到「一组里有两条
# 代表」，又不至于让表号系列把整页占满。
SERIES_SLOTS = 2


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: str | Path | None = None, with_meta: bool = True) -> list[dict]:
    """读目录条目。``with_meta`` 时挂上平台原生元数据（categories/格式）。

    元数据以 ``_hub_meta`` 键挂在内存里，不改 listings.json（那是采集脚本的
    产物）；``save()`` 落盘前会剥掉它，所以编辑器改条目也不会把缓存写回去。
    """
    p = Path(path or DEFAULT_LISTINGS)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    clean = [normalize(i) for i in items if isinstance(i, dict) and i.get("name")]
    return enrich.decorate(clean) if with_meta else clean


def is_published(item: dict) -> bool:
    """上架开关。缺省即已上架——老档案不带这个字段，不能因为升级开关
    就把整座货架一键熄灭。只有显式 ``published: false`` 才是下架。
    """
    return item.get("published") is not False


def visible(items: list[dict]) -> list[dict]:
    """面向平台用户/agent 的可见集。下架条目对后台管理仍完整可见。"""
    return [i for i in items if is_published(i)]


def load_visible(path: str | Path | None = None, with_meta: bool = True) -> list[dict]:
    """agent 侧唯一的目录读取入口。所有工具面都必须走这里。"""
    return visible(load(path, with_meta=with_meta))


def _decl_granularity(item: dict) -> str:
    """条目声明的粒度文本（「年度·分省」「国家」…），取不到就空串。"""
    decl = item.get("capability") or {}
    return str(decl.get("granularity") or "")


def shelf_tier(item: dict) -> str:
    """这条货在哪一层（0.32）。上架开关决定「看不看得见」，这里决定
    「今天能不能取到数」——两件事，不能混。

    - ``content``：平台手里有货（实测打开过，或至少有可调的端点/下载地址）。
    - ``gated``：平台没打开过内容，但实地试过入口、确认墙在哪（Wind 这类
      付费库）。值得让 agent 去谈，档案里有闸门位置与受理方。
    - ``registry_only``：只在册。官方清单登记了它存在，平台没探过入口、
      也没有可查的产品页。它证明这条路存在，不证明今天能走。
    """
    pre = item.get("shelf_tier")
    if pre in ("content", "gated", "registry_only"):
        # 薄索引里存的是构建时算好的结果（见 bdsp/store.py）：薄记录不带
        # harvest/endpoint，现算会一律判成 registry_only。
        return pre
    hv = item.get("harvest") or {}
    status = str(hv.get("status") or "")
    endpoint = str((item.get("usage") or {}).get("endpoint") or "")
    holds_content = bool(item.get("data_url")) or status == "ok" or bool(
        status and status != "no_public_endpoint" and endpoint)
    if holds_content:
        return "content"
    return "gated" if hv.get("attempts") else "registry_only"


# ── 平台增益预期（0.35）─────────────────────────────────────────────────
# reports/naked_vs_mcp.md 第五节的实测结论：同一个条目，裸爬入口页能不能拿到
# 它，决定了平台在这条上到底值多少。把结论写成路由字段，是为了让 agent 在
# 决定去不去之前就知道——有些条目我们只是「省一步」，讲成「帮你找到」就是
# 虚报，兑现不了就是失信。
#
# 判据只有一句：**agent 自己能不能找到、能不能走通**。
#   找不到（页面是 SPA、明细 401）→ high，我们给的是可达性
#   找得到走不通（付费墙、要授权）→ medium，我们给的是闸门位置
#   找得到也走得通（直链文件、开放 API）→ low，我们给的是省一步
_NAKED_GAIN: dict[str, tuple[str, str]] = {
    "resource": ("high",
                 "裸爬入口页为 SPA，正文无条目名；明细接口实测 401（32/32 抽样）"),
    "primary": ("high",
                "同门户 SPA，条目间差别只在括号限定词，搜索分不出来（31/31 抽样）"),
    "redeveloped": ("high", "同门户 SPA，裸爬拿不到条目名（19/19 抽样）"),
    "exchange": ("medium",
                 "商品页摘要能被搜索索引，但字段与流程要登录；增益在申请路径"),
    "corpus": ("medium", "平台页可打开但无条目名，许可条件只在档案里"),
    "hydro": ("low", "水文数据介绍就在集团官网新闻正文里，裸爬能直接读到"),
    "platform": ("low", "门户入口本身就是公开可见的那一个地址"),
    "portal": ("high",
               "门户目录可拉、明细一律 401、入口页是 SPA；裸抓拿不到"
               "「这条是谁的、能不能取」"),
    "official_file": ("low",
                      "公告附件 xlsx 搜索引擎直接命中，比我们还快；只能提醒「清单≠数据」"),
}

_GAIN_LABEL = {"high": "强增益（裸爬拿不到可达性）",
               "medium": "中增益（省一次闸门摸索）",
               "low": "弱增益（只是省一步）"}

_GAIN_NEXT = {
    "high": "按档案走受理路径：这里裸抓一定失败，别让 agent 白跑一遍",
    "medium": "先看闸门位置再决定要不要谈；能谈成才有内容",
    "low": "用户可以自己找到；我们能补的是「清单/页面 ≠ 可用的那份数据」这句判断",
}


def expectation(item: dict) -> dict:
    """这条路牌值多少：裸爬对照下的平台增益，及其含义。

    有实测批次的按批次给（依据写在 ``basis`` 里，可复跑）；其余按形态与闸门
    推——推的也要标出来，别让 agent 把推断当实测。
    """
    grp = str(item.get("onboard_group") or "")
    usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
    form = str(usage.get("form") or "")
    gate = str(usage.get("gate") or "")
    if grp in _NAKED_GAIN:
        gain, why = _NAKED_GAIN[grp]
        basis = "probe"  # reports/naked_crawl.json
    elif form == "file" and (item.get("data_url") or usage.get("direct_url")):
        gain, why, basis = "low", "直链文件，源站与搜索都能直接拿到", "derived"
    elif gate in ("paid", "authorized"):
        gain, why = "medium", "内容在墙后：用户知道它存在，但要摸清闸门与受理方"
        basis = "derived"
    elif form in ("portal_api", "stat_api") and gate == "open":
        gain, why, basis = "low", "开放接口，文档与端点源方自己公开", "derived"
    elif form == "mcp":
        gain, why, basis = "low", "源方自带 MCP，挂载方式由源方给", "derived"
    elif form:
        gain, why, basis = "medium", "网页形态，需站内检索；增益在把检索路径写清", "derived"
    else:
        gain, why, basis = "", "", ""
    if not gain:
        return {}
    return {
        "gain": gain,
        "gain_label": _GAIN_LABEL[gain],
        "why": why,
        "next": _GAIN_NEXT[gain],
        "basis": basis,
        "naked_test": ("reports/naked_vs_mcp.md（裸爬对照，2026-11-10）"
                       if basis == "probe" else
                       "按 form/gate 推断，未做裸爬对照"),
    }


def save(items: list[dict], path: str | Path | None = None) -> None:
    p = Path(path or DEFAULT_LISTINGS)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "dataset-plazz-listings/1",
        "generated_at": now_iso(),
        "count": len(items),
        "rule": "目录条目只声明存在与来源，不代表已建立字段级索引；字段字典一律留空。",
        "items": [{k: v for k, v in i.items()
                   if not k.startswith("_") and k != "shelf_tier"} for i in items],
    }
    # 原子写（0.36）：货架已经涨到 31,533 条 / 218MB，`write_text` 先截断再写，
    # 写一半挂掉文件就是 0 字节——今天实测真出现过这个窗口（尾随的读进程拿到
    # 空表）。先写同目录临时文件再 rename，同一卷上是原子的，读方要么看到旧的
    # 完整文件，要么看到新的完整文件。
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(p)


def normalize(item: dict) -> dict:
    """Normalize optional blocks. Empty means unknown, never a fabricated fact."""
    out = dict(item)
    out.setdefault("dataset_id", "")
    out.setdefault("name", "")
    out.setdefault("publisher", "")
    out.setdefault("publisher_level", "other")
    out.setdefault("developer", {})
    dev = dict(out["developer"] or {})
    out["developer"] = {
        "name": dev.get("name") or out["publisher"],
        "org_type": dev.get("org_type") or out["publisher_level"],
        "homepage": dev.get("homepage") or "",
        "contact": dev.get("contact") or "",
    }
    out["category"] = out.get("category") if out.get("category") in catalog.CATEGORY_IDS else "other"
    out["domain"] = list(out.get("domain") or [])
    out["modality"] = out.get("modality") or ""
    out["access_level"] = out.get("access_level") or "registered"
    out["price"] = out.get("price") or {}
    out["source_url"] = out.get("source_url") or ""
    out["data_url"] = out.get("data_url") or ""
    out["catalog_url"] = out.get("catalog_url") or out["source_url"]
    out["fields"] = list(out.get("fields") or [])  # deliberately empty for listings
    out["declared_records"] = out.get("declared_records") or 0
    out["evidence"] = dict(out.get("evidence") or {})
    out["evidence"].setdefault("level", "")
    out["evidence"].setdefault("source_url", "")
    out["evidence"].setdefault("note", "")
    out["probe"] = dict(out.get("probe") or {})
    for k, v in (("upstream_status", "unchecked"), ("note", ""), ("http_status", 0),
                 ("checked_at", ""), ("scope", ""), ("scope_note", "")):
        out["probe"].setdefault(k, v)
    out["notes"] = out.get("notes") or ""
    out["collected_at"] = out.get("collected_at") or ""
    out["linked_dataset_id"] = out.get("linked_dataset_id") or ""
    out["connect_error"] = out.get("connect_error") or ""
    # 上架/下架开关：显式 false 才下架；缺字段一律按已上架处理。
    out["published"] = out.get("published") is not False
    out["published_at"] = out.get("published_at") or ""
    out["publish_note"] = out.get("publish_note") or ""
    out["onboard_batch"] = out.get("onboard_batch") or ""
    out["onboard_group"] = out.get("onboard_group") or ""
    out["kind"] = "catalog_only"
    out["content_status"] = "indexed" if out["linked_dataset_id"] else "catalog_only"
    out["indexed"] = bool(out["linked_dataset_id"])
    return out


def access_shape(item: dict) -> dict:
    """列表行上的形态标签：不 build 档案，只读已经落盘的 form/gate。

    record() 被 discover / stats / admin 调上千次，这里不能跑 derive 或
    validate——两个 dict 取值就够列表显示「这个源能不能自动取」。
    """
    u = item.get("usage") if isinstance(item.get("usage"), dict) else {}
    form = str(u.get("form") or "")
    spec = formsmod.FORMS.get(form) or {}
    return {"access_form": form,
            "access_form_label": spec.get("label", ""),
            "access_gate": str(u.get("gate") or "")}


def record(item: dict, link: dict | None = None) -> dict:
    """Shape-compatible with catalog.records() rows, so the UI needs no fork.

    ``link`` 是 ``hubs.load_links()["relations"]`` 里该条目的挂载关系。显式
    注入而不在函数内自查，是为了让 record() 保持纯函数（无 IO）——它被
    catalog / discover / stats / admin 各调上百次，加文件读取会把列表接口
    拖慢一个量级。
    """
    i = normalize(item)
    acc = i["access_level"]
    probe = i["probe"]
    cls = tx.classify(i)
    rel = link or {}
    _scope = probe_scope(i["source_url"])
    return {
        "dataset_id": i["dataset_id"],
        "name": i["name"],
        "category": i["category"],
        "category_label": catalog.CATEGORY_LABELS[i["category"]],
        "category_source": "listing",
        "developer": i["developer"],
        "publisher": i["publisher"],
        "publisher_level": i["publisher_level"],
        "domain": i["domain"],
        "modality": i["modality"],
        "license": "",
        "update_frequency": "",
        "access_level": acc,
        "access_label": catalog.ACCESS_LABELS.get(acc, acc),
        # 接入形态与准入门槛（0.24）：列表上就要能看出「点进去能自动取，还是
        # 只有网页」。这两个键与 taxonomy 的 form（M1–M7 内容形态）不同轴，
        # 所以不叫 form，叫 access_form。
        **access_shape(i),
        "is_paid": acc == "paid",
        "price": i["price"],
        "status": "catalog_only",
        "status_label": status_label(i),
        "availability": availability(i),
        "endorsement": endorsement(i),
        "upstream_status": probe["upstream_status"],
        "upstream_label": catalog._upstream_label(probe["upstream_status"]),
        # 这次可达证明了多少：站点根 vs 落地页。站根 200 只说明站还活着。
        # 读的时候现算（scope 是 source_url 的纯函数），不依赖是否跑过巡检——
        # 否则没跑巡检的条目会显示空，看起来像“没测过”。
        "upstream_scope": _scope,
        "upstream_scope_note": scope_note(
            _scope, i.get("evidence", {}).get("level") in WEAK_EVIDENCE),
        # 取数面（data_url）探针结果：source_url 可达 ≠ agent 实际走的数据端点可达
        "data_endpoint": probe.get("data_endpoint") or {},
        "last_checked": probe["checked_at"],
        "last_error": probe["note"] if probe["upstream_status"] not in ("reachable", "local", "skipped") else "",
        "checks": {"snapshot_file": False, "snapshot_hash": False,
                   "indexed": False, "claims": False},
        "enabled": True,
        # 上架开关（后台逐条控制）：这个字段决定该源是否出现在 agent 的工具面。
        "published": i["published"],
        "published_label": "已上架" if i["published"] else "已下架",
        "published_at": i["published_at"],
        "publish_note": i["publish_note"],
        "onboard_batch": i["onboard_batch"],
        "version": "",
        "fields_count": len(i["fields"]),
        "field_names": [f.get("name", "") for f in i["fields"]],
        "field_doc": field_doc_review(i, i),
        "declared_records": None,
        "actual_records": None,
        "cells": None,
        "claim_status": "not_applicable",
        "index_digest": "",
        "snapshot_sha256": "",
        "source_url": i["source_url"],
        "data_url": i["data_url"],
        # 门户的公开目录检索端点（0.12 已逐条核实；data_url 必须保持空，
        # 否则 taxonomy.kind_of 会把门户判成数据集、整棵挂载树会散）。
        "portal_api_url": (i.get("portal_api_url") or "").strip(),
        "portal_api_kind": (i.get("portal_api_kind") or "").strip(),
        "portal_api_basis": (i.get("portal_api_basis") or "").strip(),
        "portal_api_checked_at": (i.get("portal_api_checked_at") or "").strip(),
        "catalog_url": i["catalog_url"],
        "added_at": i["collected_at"],
        "retrieved_at": "",
        "kind": "catalog_only",
        "content_status": i["content_status"],
        "content_label": "已接入索引" if i["indexed"] else "目录条目 · 未建索引",
        "linked_dataset_id": i["linked_dataset_id"],
        "connect_error": i["connect_error"],
        "evidence": i["evidence"],
        "evidence_label": EVIDENCE_LABELS.get(i["evidence"]["level"], i["evidence"]["level"]),
        "weak_evidence": i["evidence"]["level"] in WEAK_EVIDENCE,
        "notes": i["notes"],
        # 0.40：进门之后能干什么（许可）与「有多新」（源方声明的时间口径）。
        # 两者都只转述源方声明，缺一律 unknown；basis 说明这一格的来历。
        "license_summary": termsmod.license_summary(i),
        "source_metadata": termsmod.metadata(i),
        "probe": probe,
        # ── 类型体系（三轴）与平台挂载：agent 判断"该不该去"的依据 ──
        # 注意与上层 "kind"（catalog_only/indexed）区分：这里讲的是**实体**
        # 是数据集还是平台/门户，那个讲的是一条**记录**是索引还是目录条目。
        "entity_kind": cls["kind"],
        "entity_kind_label": cls["kind_label"],
        "l1": cls["l1"],
        "l1_name": cls["l1_name"],
        "l1_gb": cls["l1_gb"],
        "sub": cls["sub"],
        "sub_name": cls["sub_name"],
        "form": cls["form"],
        "form_name": cls["form_name"],
        "form_basis": cls["form_basis"],
        "use": cls["use"],
        "use_basis": cls["use_basis"],
        "taxonomy_confidence": cls["confidence"],
        "taxonomy_reason": cls["reason"],
        "parent_hub": rel.get("parent_hub", ""),
        "parent_relation": rel.get("relation", ""),
        # ── 平台原生元数据（源方声明的，不是我们判的）──
        "native_topics": enrich.native_labels(i),
        "native_topic_labels": [c.get("en") or c.get("id") or ""
                                for c in enrich.categories_of(i)],
        "distributions": enrich.distribution_summary(i) if i.get("_hub_meta") else None,
        "summary": enrich.summary_of(i),
        "summary_lang": enrich.summary_lang(i),
        "native_keywords": enrich.native_keywords(i),
        "meta_source": ((i.get("_hub_meta") or {}).get("matched_by") or ""),
    }


def profile_block(item: dict, harvest: dict | None = None) -> dict:
    """详情页要用的三件事：档案、缺项、这道墙怎么过。

    分开返回而不是全塞进一个 dict，是因为它们回答的是三个人的问题：

    * ``access_profile`` —— 给调用方照做的（form/gate/endpoint/auth/limits/examples）；
    * ``profile_errors`` / ``profile_weak`` —— 给运营补的待办（portal_api 缺
      query_param 这类具体一项，不是含糊的「未登记」）；
    * ``access_terms`` —— 给用户看的（多少钱、去哪办、付费前能不能验货）。

    混成一个，前端只能靠猜哪个键属于谁；分开后详情页三块各取所需。
    """
    prof = profilemod.build(item)
    return {
        "access_profile": profilemod.handoff_view(prof),
        "profile_errors": profilemod.validate(prof),
        "profile_weak": formsmod.recommended_missing(prof),
        "access_terms": _access_block(item, harvest, str(prof.get("gate") or "")),
    }


def detail(item: dict, link: dict | None = None) -> dict:
    """Public detail for a catalogue-only entry. No invented fields or hashes."""
    i = normalize(item)
    r = record(i, link)
    acc = r["access_level"]
    return {
        "ok": True,
        "kind": "catalog_only",
        "dataset_id": r["dataset_id"],
        "name": r["name"],
        "published": i["published"],
        "published_label": "已上架" if i["published"] else "已下架",
        "published_at": i["published_at"],
        "publish_note": i["publish_note"],
        "onboard_batch": i["onboard_batch"],
        "category": r["category"],
        "category_label": r["category_label"],
        "category_source": "listing",
        "category_reason": "目录条目按发布者/主题归类；未经源方逐条确认",
        "tags": r["domain"],
        "developer": r["developer"],
        "publisher": r["publisher"],
        "publisher_level": r["publisher_level"],
        "domain": r["domain"],
        "modality": r["modality"],
        "license": "",
        "update_frequency": "",
        "version": "",
        "status": "catalog_only",
        "status_label": status_label(i),
        "availability": r["availability"],
        "endorsement": r["endorsement"],
        "upstream_status": r["upstream_status"],
        "upstream_label": r["upstream_label"],
        "last_checked": r["last_checked"],
        "last_error": r["last_error"],
        "checks": r["checks"],
        "connection": {
            "endpoint": r["source_url"],
            "method": "GET",
            "auth_scheme": "none" if acc == "open" else "operator_supplied",
            "credential_required": acc != "open",
            "content_type": "",
        },
        "access": {
            "level": acc,
            "price": r["price"],
            "proof_free": True,
            "no_hit_no_charge": True,
            "auth_url": r["catalog_url"],
            "payment_url": "",
        },
        "access_label": r["access_label"],
        "evidence": r["evidence"],
        "evidence_label": r["evidence_label"],
        "weak_evidence": r["weak_evidence"],
        "evidence_policy": {"proof_free": True, "no_hit_no_charge": True, "max_proof_chars": 0},
        "coverage": {"declared_records": None, "time_range": [], "entity_scope": ""},
        # 能力声明：目录条目最缺的就是「这到底能干什么」。后台详情把它
        # 放在基本信息之后，运营一眼能看出这条是数据、还是只是个门户，
        # 以及这份说明是发布方给的还是我们推的。
        "capability": i.get("capability") or capmod.declare(i),
        # 取数实测：平台有没有真的打开过它、看到哪些字段。与 evidence
        # （来源证据等级）是两个维度，不能混用。
        "harvest": {
            "status": (i.get("harvest") or {}).get("status", ""),
            "note": (i.get("harvest") or {}).get("note", ""),
            "records": (i.get("harvest") or {}).get("records", 0),
            "field_count": (i.get("harvest") or {}).get("field_count", 0),
            "fields": [f.get("name") for f in (i.get("harvest") or {}).get("fields", [])][:20],
            "endpoint": (i.get("harvest") or {}).get("endpoint", ""),
            "probed_at": (i.get("harvest") or {}).get("probed_at", ""),
            **_harvest_scope((i.get("usage") or {}).get("endpoint", ""),
                             i.get("harvest") or {}),
        } if i.get("harvest") else {},
        "commitment": {},
        "representative_queries": [],
        "fields": list(i["fields"]),
        "field_source": "operator_documented" if i["fields"] else "",
        "field_doc": field_doc_review(i, i),
        "fields_count": len(i["fields"]),
        "row_key": [],
        "ingest": {},
        "source_url": r["source_url"],
        "data_url": r["data_url"],
        "portal_api_url": r["portal_api_url"],
        "portal_api_kind": r.get("portal_api_kind", ""),
        "portal_api_basis": r["portal_api_basis"],
        "portal_api_checked_at": r["portal_api_checked_at"],
        "catalog_url": r["catalog_url"],
        "credential_required": acc != "open",
        "credential_configured": False,
        "credential_masked": "",
        "snapshot_sha256": "",
        "raw_file": "",
        "retrieved_at": "",
        "added_at": r["added_at"],
        "notes": r["notes"],
        "index": {},
        "indexed": False,
        "content_status": r["content_status"],
        "content_label": r["content_label"],
        "linked_dataset_id": r["linked_dataset_id"],
        "connect_error": r["connect_error"],
        "probe": r["probe"],
        # 类型体系与挂载：详情页要能回答"这是什么、属于哪、上级门户是谁"
        "entity_kind": r["entity_kind"],
        "entity_kind_label": r["entity_kind_label"],
        "l1": r["l1"], "l1_name": r["l1_name"], "l1_gb": r["l1_gb"],
        "sub": r["sub"], "sub_name": r["sub_name"],
        "form": r["form"], "form_name": r["form_name"],
        "use": r["use"], "use_basis": r["use_basis"],
        "taxonomy_confidence": r["taxonomy_confidence"],
        "taxonomy_reason": r["taxonomy_reason"],
        "parent_hub": r["parent_hub"],
        "parent_relation": r["parent_relation"],
        # ── 接入档案：详情页的「怎么用」这一层（0.24）──
        # 目录条目过去只讲「存在/可达/要不要钱」，agent 看完仍不知道往哪发请求。
        # 档案补的正是那一步：形态、准入门槛、协议、入口、鉴权、限额、示例。
        # 平台仍不代取：示例里的凭证一律是占位符，调用方自己换。
        **profile_block(i, i.get("harvest")),
    }


def _name_key(name: str) -> str:
    """同名判定用的名字键：只抹掉版本号与「查询/产品/数据/信息/接口」这类尾巴。

    不抹括号里的限定词——「（累计缴存年限）」和「（最近缴存日）」是两条不同
    的产品，抹掉就把它们并成一条了。这里只回答一个问题：**名字层面能不能
    区分**。不能区分（「企业失信情况查询V2.1 / V2.2」），排位就只能按
    dataset_id 定序，得把这件事如实告诉 agent。
    """
    s = re.sub(r"[Vv]\d+(\.\d+)*", "", str(name or ""))
    s = re.sub(r"(?<![A-Za-z])\d+(\.\d+)+", "", s)      # 裸版本号：产品2.0
    s = re.sub(r"(?i)new|新版|旧版", "", s)
    s = re.sub(r"\s+", "", s).strip("·-_（）()")
    for _ in range(3):
        t = re.sub(r"(数据信息|信息数据|数据产品|数据|信息|查询产品|查询|产品|接口)$", "", s)
        t = t.strip("·-_（）()")
        if t == s:
            break
        s = t
    return s


def _series_key(name: str, publisher: str = "") -> str:
    """系列键：同一份统计的「表 340-45001 / 45011 / 45021」算一个系列。

    `_name_key` 回答的是「两条名字是否相同」，抹的是版本号；这里回答的是
    「这两条是不是同一份长表的不同分册」——香港政府统计处把《货币及金融
    市场的统计数字》拆成几十条表号，名字前半截完全一致，只有表号不同。
    名字层面它们各自可区分，排位上却是一件事：给 agent 十条同一个系列的表，
    等于给了一条路和九次重复。

    判据是「同一发布方 + 名字前 8 个字」。不能用「到第一个数字为止」：表号
    出现得早，「表 315-…」与「表 340-…」是同一份统计的不同章节，得一起折叠；
    而 8 个字足够把「Total electricity」和「Total energy」分开，不误并。
    """
    return "%s|%s" % (str(publisher or "")[:40], _name_key(name)[:8])


def _pick_with_series_cap(rows: list[dict], limit: int, series_of: dict[str, str],
                          slots: int = SERIES_SLOTS,
                          family_slots: int = 0) -> tuple[list[dict], dict, dict]:
    """按排序结果取候选，但同一系列最多占 `slots` 席。

    为什么要这一步（0.38）：货架从 2,027 条扩到 35,418 条后，香港统计处的
    表号系列（几千条同前缀条目）把「A股实时行情 金融终端」的候选位整页占满，
    把真正的答案 Wind（付费终端，档案里连买法都写了）挤出前排——付费墙事故
    （0.21）在更大的语料上重演了一遍。这不是分数算错，是**同一件事重复出现**，
    配额比调权重更可靠：权重会连带压掉别的真匹配。

    0.39 再加一维 ``family_slots``：同一**源方家族**（dataset_id 第一段）在
    一次大结果集里也该有上限。实证「欧盟工厂的大气污染物排放数据」前 8
    有 7 条都是 EDGAR GHG 2024 按气体/部门切出来的世行指标（名字各不相同，
    series 键折叠不到），EU 空气质量条目从第 9 位起连中 28 条却一条都进不了
    答案页。跨语言词表本身是生效的，坏的是「同一份数据库霸占整页」。
    只在 limit >= 8 时启用（小结果集本来就该给最对题的几条），且配额取 5，
    保证至少 3 个席位留给别的源。

    席位用完的系列/家族不丢弃，溢出条目在候选不够时按原顺序补回，保证
    limit 优先于配额。
    """
    picked: list[dict] = []
    overflow: list[dict] = []
    dropped: dict[str, str] = {}
    used: dict[str, int] = {}
    used_family: dict[str, int] = {}
    for c in rows:
        if len(picked) >= limit:
            break
        key = series_of.get(c.get("dataset_id") or "", "")
        if used.get(key, 0) >= slots:
            overflow.append(c)
            dropped[c.get("dataset_id") or ""] = "series_cap"
            continue
        fam = (c.get("dataset_id") or "").split("_", 1)[0]
        if family_slots and used_family.get(fam, 0) >= family_slots:
            overflow.append(c)
            dropped[c.get("dataset_id") or ""] = "family_cap"
            continue
        used[key] = used.get(key, 0) + 1
        if family_slots:
            used_family[fam] = used_family.get(fam, 0) + 1
        picked.append(c)
    if len(picked) < limit:
        back = overflow[: limit - len(picked)]
        picked.extend(back)
        # 递补回来的不算落榜：可解释性不能把「确实出现在首屏」说成被刷。
        for c in back:
            dropped.pop(c.get("dataset_id") or "", None)
    counts = Counter(series_of.get(c.get("dataset_id") or "", "") for c in rows)
    return picked, dict(counts), dropped


def _discovery_keywords(item: dict) -> list[str]:
    """条目自身的发现词面：name + 门户名前缀 + domain + 操作员策展补充词。"""
    kws = [item["name"]]
    # 门户名常写成「国家统计局 · 国家数据」，整串不会出现在任何提问里，但
    # 前半截（机构名）会。「找国家统计局的数据」必须能命中它，否则端口条目
    # 在自己的名字上永远输给一堆泛匹配的统计表。
    for part in re.split(r"\s*[·—|]\s*|\s+-\s+", item.get("name") or ""):
        part = part.strip()
        if len(part) >= 2 and part != item["name"]:
            kws.append(part)
    kws.extend(item["domain"])
    kws.extend(item.get("discovery_keywords") or [])
    return [k.lower().strip() for k in kws if k and k.strip()]


# 策展式中英领域词表（操作员维护，非运行时语义猜测）：货架上大量条目的
# name/domain 是外文（EU 来源多为英文），中文自然语言提问必须先经此表
# 扩展出对应外文词，正向匹配才有可能命中。键是中文词元，值是条目文本里
# 实际会出现的外文词面。新增外文领域时在此登记即可。
_LEXICON_CN_EN = {
    "贸易": ["trade", "comtrade", "import", "export"],
    "进口": ["import", "comtrade"],
    "出口": ["export", "comtrade"],
    "中国": ["china"],
    "利率": ["interest rate", "federal funds", "fed funds"],
    "美联储": ["federal reserve", "fed"],
    "联邦基金": ["federal funds"],
    "货币政策": ["monetary policy", "federal reserve"],
    "央行": ["central bank", "federal reserve"],
    "人均": ["per capita"],
    "预期寿命": ["life expectancy"],
    "死亡率": ["mortality"],
    "识字率": ["literacy"],
    "环境": ["environment", "environmental"],
    "环保": ["environment", "environmental"],
    "大气": ["air quality", "air emission", "air"],
    "空气": ["air quality", "air"],
    "排放": ["emission", "emissions"],
    "污染": ["pollution", "emission", "air quality"],
    "气候": ["climate"],
    "健康": ["health", "healthcare"],
    "医疗": ["health", "healthcare", "hospital"],
    "卫生": ["health"],
    "疾病": ["disease", "health"],
    "肿瘤": ["cancer", "oncology", "tumor"],
    "交通": ["transport", "traffic", "mobility"],
    "运输": ["transport"],
    "能源": ["energy"],
    "电力": ["energy", "electricity"],
    "农业": ["agriculture", "agricultural", "farm"],
    "渔业": ["fishery", "fisheries", "fishing"],
    "经济": ["economy", "economic"],
    "宏观": ["macro", "economy", "economic"],
    "教育": ["education", "school"],
    "人口": ["population", "demographic"],
    "统计": ["statistics", "statistical"],
    "贫困": ["poverty"],
    "扶贫": ["poverty"],
    "收入": ["income"],
    "就业": ["employment", "labour", "labor"],
    "养老": ["pension"],
    "住房": ["housing"],
    "税收": ["tax", "taxation"],
    "犯罪": ["crime", "justice"],
    "水质": ["water quality"],
    "供水": ["water", "water supply"],
    "企业": ["company", "business", "enterprise"],
    "工业": ["industry", "industrial"],
    "气象": ["weather", "meteorological"],
    "暴雨": ["rain", "precipitation", "storm"],
    "灾害": ["disaster", "hazard"],
    "英国": ["united kingdom", "uk"],
    "德国": ["germany"],
    "法国": ["france"],
    "意大利": ["italy"],
    "西班牙": ["spain"],
    "葡萄牙": ["portugal"],
    "荷兰": ["netherlands"],
    "比利时": ["belgium"],
    "奥地利": ["austria"],
    "希腊": ["greece"],
    "波兰": ["poland"],
    "捷克": ["czechia", "czech"],
    "爱尔兰": ["ireland"],
    "立陶宛": ["lithuania"],
    "拉脱维亚": ["latvia"],
    "卢森堡": ["luxembourg"],
    "斯洛文尼亚": ["slovenia"],
    "乌克兰": ["ukraine"],
    "欧洲": ["europe", "eu "],
    "欧盟": ["europe", "eu "],
}


# 0.39 §9：词表扩出来的**通用修饰语**不是主题。「人均」→ ``per capita``
# 在 3.5 万条货架上把世行的人均 GDP / 人均消费 / 人均能源一整批抬成高分，
# 「各国人均寿命对比」的 OWID 预期寿命被挤出前 8。修饰语只说明「按人算」，
# 不说明「是什么数据」，命中时降到 1/6 权重——不删词，免得真需要它的英文
# 条目一个都召回不到。新增通用修饰语在此登记即可。
GENERIC_MODIFIERS = frozenset({"per capita"})


def _lexicon_expand(ql: str) -> list[str]:
    """中文提问 → 外文领域词扩展。查询里出现词表键（子串），就登记其外文
    等价词面作为附加检索词。纯查表，确定性。"""
    extra: list[str] = []
    for cn, ens in _LEXICON_CN_EN.items():
        if cn in ql:
            extra.extend(ens)
    return extra


def _access_block(item: dict, harvest: dict | None, gate: str = "") -> dict:
    """付费墙的结构化形态：墙在哪、多少钱、怎么进、付款前能不能验货。

    只给「需付费」三个字时，agent 只能把它当「没有数据」。实测「A股实时
    行情 金融终端」：Wind 明细能接（J 金融业），却因为整批候选没有能力
    全中而被判 shelf_gap + literal_only——把**收费**误报成**不存在**。
    这两件事对用户的下一步完全不同：一个是「换问题」，一个是「掏钱或换源」。

    ``gate`` 比 ``access_level`` 更具体：开放源也可能被 WAF 挡住、或本机出
    不去。这种情况下说「直接调用」就是骗人——按闸门给下一步。
    """
    lvl = (item.get("access_level") or "").strip()
    label = catalog.ACCESS_LABELS.get(lvl, lvl or "未标注")
    step, note = _ACCESS_NEXT.get(lvl, ("check_source", ""))
    g = _GATE_NEXT.get(str(gate or ""))
    if g:
        step, note = g
    usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
    mcp = usage.get("mcp") if isinstance(usage.get("mcp"), dict) else {}
    if step == "direct" and (mcp.get("command") or mcp.get("url")):
        # 源方有 MCP：下一步不是「调接口」，是「挂 MCP」。参数与结果组织归源方。
        step, note = ("use_mcp",
                      "源方提供 MCP：挂上它并用它自己的工具取数；参数名、翻页、"
                      "结果组织由源方 MCP 负责，平台不转录也不代调。")
    h = harvest or {}
    # 付费前能否验货：只有真取到过样本（status=ok 且有记录）才算能。
    # 商业数据库一律不能——这正是「付费墙」的定义。
    sampled = h.get("status") == "ok" and int(h.get("records") or 0) > 0
    price = item.get("price") or None
    if isinstance(price, dict) and not price:
        price = None
    return {
        "level": lvl,
        "label": label,
        "is_paid": lvl == "paid",
        "needs_auth": lvl in ("registered", "authorized", "paid"),
        "price": price,
        # 源方入口：付费/授权类只能由源方受理，平台不代销不代收
        "buy_at": (item.get("source_url") or "").strip()
                  if lvl in ("paid", "authorized") else "",
        "next_step": step,
        "paywall_note": note,
        "verifiable_before_pay": sampled,
    }


# 闸门比访问级别更靠前：被 WAF 挡住时，先解决闸门，再谈要不要注册/付费。
_GATE_NEXT: dict[str, tuple[str, str]] = {
    "waf": ("use_browser_ua",
            "公开但设了访问闸门（HTTP 403 / 人机验证 / 地域限制）。换浏览器 UA "
            "或登录后再试；被挡住不等于源里没有数据。"),
    "unreachable": ("retry_elsewhere",
                    "本机两条路都不通（DNS / 超时 / 证书）。换网络或换节点重试；"
                    "平台判「不可达」只说明这台机器出不去，不说明源已下线。"),
}


_ACCESS_NEXT: dict[str, tuple[str, str]] = {
    "open": ("direct", ""),
    "registered": ("register", "免费注册/申领 key 后可取；平台不代管账号"),
    "authorized": ("request_authorization",
                   "需向源方申请授权或凭证；平台不持有内容、不能代为申请"),
    "paid": ("contact_source",
             "源方商业订阅（账号/终端/API 按机构授权计费）；平台不代销、"
             "不代收、不代持凭证，也不保证墙后有你要的那个指标"),
}


def _freshness(hv: dict) -> dict:
    """实测结论的年龄。缓存快照会过期，agent 有权知道这张卡有多旧。

    0.13 之前 discover 只透 status/records，于是「三天前取到过」和「刚才
    取到过」在路牌上长得一模一样；本次跨领域实测就撞上一条目录写 ok、
    现网 8 秒超时的条目。没有 probed_at，这个差别根本看不见。
    """
    at = str(hv.get("probed_at") or "")
    if not at:
        return {"probed_at": "", "probed_days_ago": None, "stale": True}
    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = max(0, (datetime.now(timezone.utc) - dt).days)
    except (ValueError, TypeError):
        return {"probed_at": at, "probed_days_ago": None, "stale": True}
    return {"probed_at": at, "probed_days_ago": days, "stale": days >= 7}


def _harvest_scope(usage_endpoint: str, hv: dict) -> dict:
    """这条实测结论说的是哪个地址——往往不是接入档案里的取数入口。

    0.20 起的目录条目有两套地址：``data_url``（平台去敲门的地方，可能是
    meta.json、门户首页、单表示例）和 ``usage.endpoint``（档案里交给 agent
    的接入契约）。harvest 只探前者。两者不同时，把 ``unparsed`` /
    ``timeout`` 直接摆出来会被读成「这个源取不到数」——2026-11-09 实测
    BeaconMFG：meta.json 是单个 JSON 对象（unparsed），而 /v1/search 匿名
    档一次返回 20 条、total_matched=703。状态本身没错，错的是它被当成了
    接入入口的结论。这里把适用范围写明，不改动原始 status。
    """
    probed = str(hv.get("endpoint") or "").strip()
    access = str(usage_endpoint or "").strip()
    if probed and access and probed != access:
        scope = "different"
    elif probed and access:
        scope = "same"
    else:
        scope = "unknown"
    out = {"endpoint_scope": scope, "probed_url": probed, "access_url": access}
    if scope == "different":
        out["scope_note"] = (
            f"该状态只说明平台实测的地址 {probed}；接入入口是 {access}，"
            "两者不是同一地址——不能据此判定接入入口不可用，按 usage 里的 "
            "request 自行取数后再说。")
    return out


def discover(query: str, items: list[dict], limit: int = 8,
             links: dict | None = None, need: dict | None = None,
             recall=None, recall_k: int = 400) -> dict:
    """Catalogue-level discovery. Returns candidates, NEVER proof or values.

    两条路（0.18 起主人定调的架构）：

    - **主路径**：``need`` 传 agent 在分级地图（dp_map）上归好类的结构化
      坐标（topics/subtopics/sectors/entities/measures/geo/year…）。平台只
      做结构校验，然后在大类里用名片排出候选——query 可以为空，任何语言
      都不需要平台分词。
    - **兜底路径**：不传 need 时才走 ``parse_need`` 词面解析（控制台直连
      用）。词面绑定在地图节点上，有限集合，不再增长。

    ``links`` 是平台挂载关系（``hubs.load_links()["relations"]``）；不传则
    自查一次。discover 是"每次提问跑一次"的低频路径，多读一个小文件换来
    候选自带 ``parent_hub``，比让 agent 再查一次门户划算。
    """
    q = str(query or "").strip()
    # 兜底防线：调用方传进来的可能是全量货架（后台/回归脚本），但下架的源
    # 不该出现在任何候选里。可见性在数据层再挡一次，不依赖每个调用点都记得
    # 先过 visible()。
    items = visible(items)
    # 0.40：元数据覆盖率必须在召回**之前**对全量算。用召回后的 400 条去算
    # 会得出「99% 条目都声明了许可」这种假精确——它只是碰巧都来自香港。
    _metadata_coverage = termsmod.coverage(items)
    # 召回层（0.36）：3.5 万条时逐条精算是 5.5s，先倒排取 top-K 再精算降到
    # 90ms（实测 60 问，命中零损失，见 reports/routing_efficiency.md）。
    # 只在调用方传了 recall 且**传进来的就是索引对应的那份全集**时生效——
    # 传子集（回归脚本的调试口径）下标会错位，宁可不加速也不给错候选。
    _recall = recall if (recall is not None and len(items) == getattr(recall, "n", -1)) else None
    # 同名兄弟统计（0.33）：见 picked 处的说明。整份货架算一次，别放进循环。
    _name_counts = (_recall.name_counts() if _recall is not None
                    else Counter(_name_key(i.get("name") or "") for i in items))
    # 0.29：两条路要在返回值里说清楚。词面兜底没解析出的轴根本不参与比对，
    # 字面分容易把不相干条目抬成 partial（实测「农产品批发价格」兜底把一份
    # 上海企业名录判成 partial，主路径认出行业轴后如实判 gap）。所以降级
    # 必须是硬约束，而不是一句提示。
    route_source = "agent" if need is not None else "fallback"
    if need is not None:
        coords, errs = mapmod.validate_need(need)
        if errs:
            return {"ok": False, "reason": "invalid_need", "errors": errs,
                    "NEXT": "按 dp_map 的合法码修正 need 后重试。"}
        if not any([coords["topics"], coords["subtopics"], coords["sectors"],
                    coords["entities"], coords["measures"]]):
            return {"ok": False, "reason": "empty_need",
                    "NEXT": "need 至少要有一个归类坐标（topics/subtopics/"
                            "sectors/entities/measures），见 dp_map。"}
        ned = capmod.need_from_coordinates(coords)
    else:
        ned = capmod.parse_need(q)
    # 0.29 收敛：降级只针对「这次没解析出坐标」的调用。解析出坐标的词面
    # 兜底和主路径用的是同一套 match()，结论本该一致；一刀切把控制台直连
    # 的真实匹配也打成 partial，会把「真实匹配不被误杀」的既有回归一起
    # 打死（0.29 单测实测 4 条）。route_source 仍全量透出，供调用方判断
    # 这次是谁在归类；unverified_axes 也随每个候选给出。
    degraded = bool(ned.get("weak_need"))
    if not q and need is None:
        return {"ok": False, "reason": "empty_query", "stage": "catalog_only"}
    # 召回先做（0.36）：`hubs.load_links(items)` 是 O(条目) 的，3.5 万条要
    # 1.5s 左右——放在缩小之后只剩 400 条的规模。顺序错了加速就白给。
    _pre_need = ned
    # 0.43：问的是省级（「上海GDP」）时，把「分省」加进召回扩展词。
    # 不加这一步，分省表只能靠「gdp」一个高频词被召回，在一个上万的
    # posting 里挤不进前 400，agent 永远看不到它——实测「2024年上海GDP」
    # 前 400 名全是世行国家级条目，分省表一条都没有。
    # 这是**按需求坐标**扩展，不是往条目里堆省名：条目并没有声称覆盖
    # 每一个省，只是分类树说它是分省口径。
    _expand: list[str] = []
    _geo_need = str(ned.get("geo") or "")
    if "/" in _geo_need and any(_pv in _geo_need
                               for _pv in locatormod.PROVINCES):
        _expand.append("分省")
    # 0.44：agent 投递的指标列名（need.indicator）进召回。主路径下 query
    # 可以为空，此时召回只剩坐标轴权重，指标词一步都没用上——实测 6 个
    # 「只给坐标」的提问，目标表 top3 命中 0/6，且前两名在所有提问里相同
    # （农产品生产价格指数 / 建筑业劳动生产率），说明不是「排得不准」，
    # 是**根本没有区分度**。指标名已在 item_terms 里（locator_terms），
    # 所以整词能召回到真正有这一列的表。
    _ind = [str(x).strip() for x in (ned.get("indicator") or []) if str(x).strip()]
    # 召回索引的 key 是 2-gram（recall.terms_of 对 4+ 汉字切 2 字），所以
    # expand 必须传同样粒度的词元，不能传整词——第一版传整词，实测
    # 「粮食产量」「农林牧渔业总产值」「年末常住人口」三个都召不回目标表，
    # 而「地区生产总值」之所以能进前 8，是因为它恰好被别的路径捎带上了。
    for _w in _ind:
        for _t in _TOKEN.findall(_w.lower()):
            if re.fullmatch(r"[\u4e00-\u9fff]{4,}", _t):
                for _j in range(len(_t) - 1):
                    _g = _t[_j:_j + 2]
                    if _g not in _expand:
                        _expand.append(_g)
            elif _t not in _expand:
                _expand.append(_t)
    if _recall is not None and len(items) > 2 * recall_k:
        _keep = _recall.recall(q, _pre_need, k=recall_k, expand=_expand)
        if _keep:
            items = [items[i] for i in _keep]
    if links is None:
        from . import hubs
        links = hubs.load_links(items)["relations"]
    # 系列键（0.38）：候选里同一份统计的不同分册要折叠成两席（见
    # `_pick_with_series_cap`）。在缩小到召回集之后算，只几百次字符串操作。
    _series_of = {i["dataset_id"]: _series_key(i.get("name") or "",
                                               i.get("publisher") or "")
                  for i in items}
    ql = q.lower()
    # 需求坐标：主体 / 度量 / 行业 / 地理。这一步之前，discover 只能拿用户
    # 的原词去撞条目名称；命中不了就返回空，agent 以为货架上没有。解析出
    # 坐标之后，「苏州汤面」可以匹配到一份声明覆盖餐饮业的企业名录——即使
    # 那份数据的名字里没有「汤面」两个字。
    need = ned
    # 这次提问是不是要「省/市」这一层。用于粒度适配加分（见下）。
    _geo_ask = str(need.get("geo") or "")
    _need_subnational = bool("/" in _geo_ask) and any(
        _p in _geo_ask for _p in locatormod.PROVINCES)
    terms = [t.lower() for t in _TOKEN.findall(q)]
    if not terms:
        terms = [ql] if ql else []
    # 0.44：指标列名并进打分词元。它是提问的一部分，且比 query 的字面更
    # 明确——「我要地区生产总值」这句话在原话里可能一个字都没有（agent 已
    # 把它译成列名）。不并进来，主路径（只给坐标）就一个内容词都没有，
    # 排序只剩能力分的固定偏序。
    for _w in _ind:
        for _t in _TOKEN.findall(_w):
            _t = _t.lower()
            if _t not in terms:
                terms.append(_t)
    # 中文长词切 2-gram：没有分词器时，「肝癌术后复发率」作为一个整体词元，
    # 在任何数据集名称里都不会连续出现；但「肝癌」「复发」各自都是强信号。
    # 这是确定性的字面切片，不是语义猜测——它只负责让中文长句有机会命中，
    # 判断「是不是这条路」仍由能力坐标做。
    grams: list[str] = []
    for t in terms:
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", t):
            for i in range(len(t) - 1):
                g = t[i:i + 2]
                if g not in terms and g not in grams:
                    grams.append(g)
    # 策展词表扩展：中文词 → 外文领域词（货架上大量外文条目的正向匹配前提）
    for t in _lexicon_expand(ql):
        if t not in terms:
            terms.append(t)
    scored: list[tuple[int, float, dict]] = []
    for item in items:
        i = normalize(item)
        # 声明在落盘时就生成好了；只有旧数据（尚无 capability）才现算。
        decl = i.get("capability") or capmod.declare(i)
        hv = i.get("harvest") or {}
        cm = capmod.match(decl, need, fields=i.get("fields"), name=i["name"])
        # 货架分层：平台手里有没有这条货。目录条目（源方未公开端点、平台也
        # 没打开过内容）是「有这条路」，不是「这条路今天能走」。0.32 全量
        # 上架贵州 531 条目录后实测：问「深圳的电子厂」首条变成一份贵州
        # 餐饮证照，问「地表水水质」被判完全满足——都是在拿「登记在册」冒充
        # 「能取数」。所以两条口径都要改：排序分层，且没货的条目一律不许判
        # complete（档案照给，取数请先过源方授权）。
        tier = shelf_tier(i)
        holds_content = tier == "content"
        # 两层排序：1=这是一条能走的路（有货，或平台探过入口、知道墙在哪）；
        # 0=只在册。同一层内仍按能力分数排，登记层不会因为名字里带个地名
        # 就抢到内容层前面。
        shelf_rank = 0 if tier == "registry_only" else 1
        if cm and not holds_content:
            cm = dict(cm)
            cm["complete"] = False
            if cm.get("quality") == "strong":
                cm["quality"] = "partial"
            cm["not_held"] = ("平台未持有该条内容，源方也未公开取数端点："
                              "这是登记在册的授权目录条目，不是今天就能取的货")
        # 表内指标名（0.41）：召回用 item_terms 时已经把指标名算进去了，
        # 打分文本却漏了它们——同一个条目「靠指标名被召回、却只按条目名得分」，
        # 于是「摩托车交通事故死了多少人」能召回到表、却排到首屏之外。
        _loc_terms = [str(x) for x in (i.get("locator_terms") or []) if x]
        if not _loc_terms:
            # 0.44：`locator_terms` 只有在落盘时写过才有；实测 37,847 条里
            # **一条都没有**（0 条），于是 0.41 那个「列名命中」补丁在真数据
            # 上一直是空转——索引侧 recall.item_terms 有 fallback，打分侧
            # 没有，同一条目「靠指标名被召回、却拿不到指标分」。这里补上，
            # 与 recall.item_terms 的取法保持一致。
            _lo = i.get("locator") if isinstance(i.get("locator"), dict) else {}
            _loc_terms = [str(x.get("name") or "") for x in
                          (_lo.get("indicators") or [])
                          if isinstance(x, dict) and x.get("name")]
        # 0.44：hay 刻意**不含**表内指标名。0.41 的本意是让指标名参与打分，
        # 但 locator_terms 从没落盘（0/37847），所以那行一直是死代码；补上
        # fallback 后它会突然生效，而效果是「有 locator 的 2,504 张 NBS 表
        # 在 hay 里凭空多出 40 个词，任何 query 词元都能蹭 +0.7」——实测
        # A 组口语盲测 hit1 5→4、hit10 14→12，「用电量」「死亡人数」这类
        # 后缀词被抬到第一。列名匹配只走下面有阈值的 column_hit，不走泛词。
        hay = " ".join([
            i["name"], i["dataset_id"], i["publisher"], i["developer"]["name"],
            " ".join(i["domain"]), i["notes"], i["source_url"],
        ]).lower()
        score = 0.0
        curated_hits = 0  # 条目自身策展词面被整句命中的次数（分层排序用）
        name_term_hit = False  # 0.39 闸门位保底用：名字真的对得上这次提问
        # 覆盖声明 × 提问：数据集自己说"我覆盖 GB/T 哪些门类"。分数已经由
        # capability.match 统一计入（声明就是从 coverage 派生的），这里只
        # 保留它给的对话式 why 与 not_included，纯做说明用。
        cov = sectormod.match(i, q)
        if cm:
            score += cm["score"]
        # 0.44：条目名里出现了 agent 投递的**完整指标列名**。注意方向与
        # name_hit 相反：name_hit 是「条目名出现在提问里」（用户念了表名），
        # 这里是「提问要的列出现在条目名里」（用户没念表名，只说要哪一列）。
        # 名字含「地区生产总值」的条目有两条（本体与「指数」），这里都给分，
        # 让列名覆盖率（下面 column_hit）去区分口径——不在这里猜谁更准。
        ind_name_hit = bool(_ind) and any(w in i["name"] for w in _ind)
        if ind_name_hit:
            score += 4.0
        # 粒度适配（0.43）：问的是省级，就该优先声明「分省」口径的条目。
        # 0.43 实测「2024年上海GDP」前 5 名全是世行国家级序列（它们含中国
        # 一行，geo 轴照样算命中），真正的分省表靠「gdp」一个高频词勉强进
        # 第 7，被首屏截掉。粒度不是内容轴，但它是「这一格数在不在里面」
        # 的前提：国家级序列给不出上海的值，再像也不是答案。
        # 只加分、不扣分——国家级条目在省级提问下未必错（有些确实带分省列）。
        if _need_subnational and re.search(
                r"分省|省级|省·|分地市|分地区|区县", _decl_granularity(i)):
            score += 3.0
        for t in terms:
            # 通用修饰语只降权、不删除：它仍是「按人算」的有效线索，只是
            # 不该和主题词等价（3.0 → 0.5 分）。
            generic = t in GENERIC_MODIFIERS
            if t in i["name"].lower():
                score += 0.5 if generic else 3.0
                name_term_hit = True
            if t in " ".join(i["domain"]).lower():
                score += 0.3 if generic else 2.0
            if t in i["publisher"].lower() or t in i["developer"]["name"].lower():
                score += 0.2 if generic else 1.0
            if t in hay:
                score += 0.2 if generic else 0.7
        # 2-gram 只认名称命中，且权重低于整词：它是「同一个词的一部分」，
        # 不该和完整词面等价。
        for g in grams:
            if g in i["name"].lower():
                score += 1.5
                name_term_hit = True
        # 列名命中（0.41）：用户念的可能是某一列的名字（「摩托车」「行人乘车人」），
        # 而条目名里只有表名。这和 name_hit 同源——都是「我要的就是它」，
        # 只是粒度细一层。按 2-gram 覆盖率判定，≥0.5 才算，一次只加一次。
        column_hit = False
        exact_ind = False
        if _loc_terms:
            _qg = set(grams)
            # 0.44：阈值分两条路。agent 投递的列名是**译好的**，覆盖率天然
            # 高，0.5 就够；口语问句里 0.5 太松——实测「车祸死亡人数」会把
            # 「传染病死亡人数」拉到第一、「用电量」会把「农村水电站及用电量」
            # 拉到第一，A 组 hit1 从 5 掉到 4、D 组目标从 rank1 掉到 rank4。
            # 口语路径只认近乎整串（0.85），保住 0.41 的原意（用户念全列名）
            # 而不让「死亡人数」这类后缀词到处加分。
            _thr = 0.5 if _ind else 0.85
            _best = 0.0
            for _nm in _loc_terms[:40]:
                _ng = {_nm[_j:_j + 2] for _j in range(len(_nm) - 1)}
                if _ng:
                    _best = max(_best, len(_ng & _qg) / len(_ng))
            if _best >= _thr:
                score += 5.0
                column_hit = True
            # 0.44：agent 投递的词与表内某一列**逐字相同**时再补一档。
            # 覆盖率过阈值不够：「地区生产总值」与「地区生产总值指数」都能
            # 过 0.5（后者 0.86），但一个是总量、一个是以某年为 100 的指数，
            # 取错了整条答案都是错的。只对 indicator 追加，不动 query 路径
            # 的既有基线（77 题盲测就是拿那条路量的）。
            if _ind:
                _norm = lambda s: re.sub(r"\s+", "", s)     # noqa: E731
                if any(_norm(w) == _norm(_nm)
                       for w in _ind for _nm in _loc_terms[:40]):
                    score += 3.0
                    column_hit = True
                    exact_ind = True
        # 反向包含：条目词面 ∈ 整句 query。中文无分词器时，正向「query 词元 ∈
        # 条目文本」对付不了「找一家广州的正宗川菜饭店」这种整句——连续汉字段
        # 会被切成一整个词元。把条目自身的关键词（饭店/餐饮/气象…）反过来在
        # 整句里找，纯字符串包含，确定性，不引入语义猜测。
        for kw in _discovery_keywords(i):
            if len(kw) >= 2 and kw in ql:
                score += 2.0
                curated_hits += 1
        # 专名命中（0.38）：整句里出现了条目自己的名字，就是「我要的就是它」。
        # 0.18 踩过一次（「找国家统计局的数据」被 statistics 泛匹配的欧盟条目
        # 整页占满），这次在 3.5 万条语料上以另一种形式复发：世行 SPI 系列
        # （Statistical Performance Indicators）名字里带 statistics，靠泛词
        # 命中把国家统计局门户本身挤出第一。泛词永远比不过专名——专名命中
        # 直接进强层，不再参与「要有内容轴才算强」的判定。
        #
        # 名字常带副标题（「国家统计局 · 国家数据」），所以除了全名，还要拿
        # 主名（第一个分隔符之前那段）去比：用户说的「国家统计局」指的是主名。
        _qflat = re.sub(r"\s+", "", ql).lower()
        _nflat = re.sub(r"\s+", "", i["name"]).lower()
        _core = re.split(r"[·|｜/（(—]+", _nflat)[0].strip("-_ ")
        name_hit = bool(_qflat) and (
            (len(_nflat) >= 4 and _nflat in _qflat)
            or (len(_core) >= 4 and _core in _qflat))
        if name_hit:
            score += 6.0
        if score <= 0:
            continue
        r = record(i, links.get(i["dataset_id"]))
        _acc = _access_block(i, hv, str((i.get("usage") or {}).get("gate") or ""))
        contact = (r["developer"] or {}).get("contact", "")
        notes = (i.get("notes") or "").strip()
        # 「为什么是它」：能力匹配给的 why 最完整（命中了哪些轴、缺哪一轴），
        # 覆盖声明给的次之（只有门类）。两个都没有时留空——不编理由。
        why = (cm or {}).get("why") or (cov or {}).get("why", "")
        if not why and decl.get("role") == "portal":
            why = "数据门户入口：本身不持有记录，需站内检索后回源取数"
        # 分层：强意图（能力轴真命中，或没有能力轴时靠策展词整句命中）整体
        # 优先。泛匹配不允许把强命中挤出前排——审计教训：「找国家统计局
        # 的数据」曾被 statistics 泛匹配的 EU 条目整页占满。
        #
        # 0.20 收紧：策展词命中不能绕过能力轴。实测「帮我找找上海衡山路附近
        # 的咖啡店」，上海门户与一条弱证据聚合条目都因为名字/关键词里有
        # 「上海」「企业」而进强层（3.5 分），把真正对题的 BeaconMFG（8.0 分、
        # H 门类 + 该类实测咖啡馆 9540 条）压到第 3。地名是**范围**，不是
        # 内容证据：只有主体/行业/主题轴命中才算强，纯 geo/度量的命中
        # 一律留在强层外按分数排。
        axes = set((cm or {}).get("verified_axes") or [])
        content_axes = axes & {"entity", "sector", "topic"}
        # 列名命中与专名命中同级（0.41）：用户念出「摩托车交通事故死亡人数」
        # 或「行人乘车人」时，指向的就是这张表的这一列，和念出表名一样确定。
        # 只加分不进层的话，11 分的条目会排在 4.5 分的强层条目之后——这正是
        # 「指得到但排不上」的机制本身。
        strong = 1 if (name_hit or column_hit or ind_name_hit or content_axes
                       or (not axes and curated_hits)
                       or (cm and cm["complete"] and cm["score"] >= 5)) else 0
        # 接入档案只算一次：validate 要扫 header 里有没有误写的真实凭证。
        _prof = profilemod.build(i)
        _prof_errors = profilemod.validate(_prof)
        # 登记层不硬性沉底（否则问「贵州省婚姻状态」也找不到那条登记），
        # 但压一档分数：只有名称/坐标确实对得上，才压得过手里有货的条目。
        effective = score if shelf_rank else score - 4.0
        scored.append((strong, effective, {
            "dataset_id": r["dataset_id"],
            "name": r["name"],
            "score": round(score, 2),
            # 0.39 闸门位保底的两个判据：名字真对得上（不是靠 domain/notes
            # 蹭分），且这条是「墙后有货」的付费/授权源。只在候选里没有一条
            # complete 时才启用，见下面 gate_slot。
            "name_term_hit": name_term_hit,
            "column_hit": column_hit,
            # 0.44：这一列是**逐字**对上的（agent 投递的指标名 == 表内某列名），
            # 还是只靠 2-gram 覆盖率近似对上的。前者可以直接信，后者要自验口径
            # ——「地区生产总值」与「地区生产总值指数」就只差这一点。
            "exact_indicator": exact_ind,
            "gate_worthy": bool(_acc.get("level") in ("paid", "authorized")
                                and tier != "registry_only"),
            # 「没验证过的轴」显式进候选：agent 一眼能看出这条 partial 是
            # 字面分撑起来的，而不是能力轴对上了。
            "unverified_axes": (cm or {}).get("unknown_axes", []),
            # 0.29 排序补丁用：卡没声明度量、提问却要量，且结构轴确实命中。
            # 这类条目（捷克货运量）真有数据，只是卡上没写度量，按度量排序
            # 会沉出 top3；给它们一次保底露脸的机会，但不许判 complete。
            "unknown_measure": bool(cm and not decl.get("measures")
                                    and (need.get("measures") or [])
                                    and (axes & {"entity", "sector", "topic"})),
            "topic_lead": bool(set(decl.get("topics") or [])
                               & set(need.get("topics") or [])),
            "why": why,
            # 能力说明：agent 判断「值不值得去」的首要依据。what 一句话，
            # asks 是它真能接住的问题句，basis 说明这份说明是发布方给的
            # 还是我们按目录元数据推的（推的要回源核对）。
            "capability": {
                "what": decl.get("what", ""),
                # 4 条而不是 3 条：BeaconMFG 的名片第 4 句恰好是「查某地有
                # 哪些门店、开在哪」——找店类提问最该看到的一句，此前被截掉，
                # agent 只能看到工商登记与「企业分布」（0.20 实测）。
                "asks": (decl.get("asks") or [])[:4],
                "basis": decl.get("basis", ""),
                "role": decl.get("role", "data"),
            },
            # 实测状态：平台有没有真的打开过它。basis=probed 说明字段是看来的；
            # harvest.status != ok 则说明这次没打开（0 条记录 / 要凭证 / 网页 /
            # 超时）。指路依然成立，但 agent 得知道去了可能取不到数，别白跑。
            # 注意与 evidence_label 区分：那是来源证据等级，这是本次取数实测。
            "harvest": ({"status": hv.get("status", ""),
                         "note": hv.get("note", ""),
                         "records": hv.get("records", 0),
                         "field_count": hv.get("field_count", 0),
                         "sample_only": (bool(hv.get("sample_only"))
                                         or (hv.get("status") == "ok"
                                             and hv.get("records", 0) <= 1)
                                         or "preview" in str(hv.get("endpoint") or "").lower()),
                         **_freshness(hv), **_harvest_scope(
                             (i.get("usage") or {}).get("endpoint", ""), hv)}
                        if hv else {}),
            # 命中/缺失：complete=False 时说明这条路只能部分满足，缺的那轴
            # 写在 missing 里，agent 不必自己推断「有没有点评数据」。
            # 只保 complete/missing——hit 与 why 内容重复，留着白烧 token。
            "capability_match": ({"complete": cm["complete"],
                                  "missing": cm["missing"],
                                  "quality": cm.get("quality", ""),
                                  # 0.32：命中但没有货——档案对得上，取数要过
                                  # 源方授权。与 missing 分开写：前者是「这条路
                                  # 不对」，这是「路对，闸门在源方手里」。
                                  **({"not_held": cm["not_held"]}
                                     if cm.get("not_held") else {}),
                                  "verified_axes": cm.get("verified_axes", []),
                                  "unknown_axes": cm.get("unknown_axes", []),
                                  # 不影响「这条路对不对」的使用提示（精度/口径），
                                  # 与 missing 分开：missing 决定放弃，advisories
                                  # 决定怎么用（0.22）。
                                  "advisories": cm.get("advisories", []),
                                  # 结构化命中证据（含「该类实测：咖啡馆 6232
                                  # 共 9540 条」）。这是名片上最有说服力的一
                                  # 行：agent 能据此判断「它真的有这类记录」，
                                  # 而不是只看一句概括。0.20 起随卡片透出，
                                  # shelf_gap 判定也改用它（此前判据读 hit，
                                  # 但响应里已剥离，导致判定永远落空）。
                                  "evidence": cm.get("hit", [])}
                                 if cm else None),
            "coverage_match": ({"sectors": cov["sectors"],
                                "not_included": cov["not_included"]}
                               if cov else None),
            # 0.42：表内定位器摘要。agent 在候选阶段就要判断「这张表里有没有
            # 我要的那一格、能不能取到」——等走到卡片再发现「只验了最新一期」
            # 就等于白跑一趟。候选只有 8–30 条且都由完整档案重排过，这条是
            # 现算的，不占薄索引（9,500 张表的坐标不进索引，见 store._KEYS）。
            "locator_view": (locatormod.describe(i.get("locator"))
                             if isinstance(i.get("locator"), dict) else {}),
            "category_label": r["category_label"],
            "domain": i["domain"],
            # 类型三轴：agent 拿它判断"是不是我要的那类东西"，比 domain 可靠
            "l1": r["l1"], "l1_name": r["l1_name"],
            "sub_name": r["sub_name"], "form": r["form"], "form_name": r["form_name"],
            "use": r["use"], "use_basis": r["use_basis"],
            "parent_hub": r["parent_hub"],
            # 「能提供什么」：来源说明摘要 + 字段清单（agent 判断值不值得去的依据）
            "notes_brief": notes[:200] + ("…" if len(notes) > 200 else ""),
            "fields": [{"name": f.get("name", ""), "description": (f.get("description") or "").strip()}
                       for f in (i.get("fields") or [])][:12],
            "access_label": r["access_label"],
            # 付费墙的结构化形态（0.21）：「需付费」三个字不够——agent 撞墙后
            # 得知道墙在哪、多少钱、怎么进、付款前能不能验货。只透出「需付费」
            # 时，agent 只能把它当「没有数据」处理（实测「A股实时行情 金融终端」
            # 就是这样：Wind 明明能接，却被整批判成 shelf_gap）。
            "access": _acc,
            # 接入档案摘要（0.22）：这是「怎么用这个源」的答案，不是数据本身。
            # 平台指路到此为止——endpoint 是源方的，不是平台代理；凭证由调用方
            # 自己去拿；翻页、清洗、判精度都是调用方的事。档案 basis 标明是
            # 源方文档（source）、操作员实测（operator）还是从目录字段推的
            # （derived，只有入口和鉴权类型，没有示例）。
            # 要完整请求示例（curl / MCP 配置）调 dp_usage(dataset_id)。
            "usage": {**profilemod.brief(_prof),
                      **({"errors": _prof_errors} if _prof_errors else {})},
            "upstream_label": r["upstream_label"],
            "evidence_label": r["evidence_label"],
            "weak_evidence": r["weak_evidence"],
            "source_url": r["source_url"],
            "catalog_url": r["catalog_url"],
            # 直链取数地址（若有）：agent 免二跳，availability 文本的配套结构化字段
            "data_url": (i.get("data_url") or "").strip(),
            # 门户本身不持有记录，取数入口在 portal_api_url。0.12 核实并
            # 落盘了 7 个公开目录检索端点（现网 7/7 可达），但此前路牌层
            # 从不透出，agent 只看到 data_url="" 就以为没地址。必须一起交出去。
            "portal_api_url": (i.get("portal_api_url") or "").strip(),
            "portal_api_kind": (i.get("portal_api_kind") or "").strip(),
            "portal_api_basis": (i.get("portal_api_basis") or "").strip(),
            "portal_api_checked_at": (i.get("portal_api_checked_at") or "").strip(),
            # 数据端点实测状态（取数面双探）：reachable/auth_failed/unreachable/空=未双探
            "data_endpoint_status": (r.get("data_endpoint") or {}).get("status", ""),
            "content_status": r["content_status"],
            # 货架分层（0.32）：content=平台真有货可交；gated=商业数据服务，
            # 墙后有产品、要过源方闸门；registry_only=只在册（政府清单登记，
            # 没有可查的产品入口）。agent 拿这个字段就知道该不该现在就派人
            # 去取——不必读完档案再判断。
            "shelf_tier": tier,
            # 0.40：许可与时间口径随候选透出。agent 在「要不要去」的那一刻
            # 就该知道「能不能再分发」「源方说多久更新一次」，而不是取完数
            # 才发现用途不合规。
            "license_summary": r["license_summary"],
            "source_metadata": r["source_metadata"],
            # 平台增益（0.35）：裸爬对照下这条路值多少。high=裸抓根本拿不到、
            # low=用户自己就能找到。放进候选而不是只放详情，是为了让 agent
            # 在「要不要去」那一刻就知道我们补的是什么，不必读完档案才判断。
            "expected_gain": (expectation(i) or {}).get("gain", ""),
            # 交接提示：源方自有 MCP 时直接给出挂载方式，agent 无需再猜取数途径
            "mcp_hint": contact.strip() if contact.strip().lower().startswith("mcp") else "",
            # 可用性：怎么取数（源方 MCP / 接口 / 下载），路牌的核心交付物
            "availability": r["availability"],
            # 字段文档质量信号：undocumented 的源 agent 要有心理预期（去了才知道列含义）
            "field_doc_level": r["field_doc"]["level"],
            # 巡检背书随候选透出：agent 在「问路」的瞬间就知道这条路通不通
            "endorsement_label": r["endorsement"]["label"] if r.get("endorsement") else "",
        }))
    # 分层排序：强意图层整体优先；同为强层时，手里真有货的排在只有登记的
    # 前面（层内按分数、dataset_id 定序）。
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]["dataset_id"]))
    # 系列配额（0.38）：同一张长表的表号系列最多占 SERIES_SLOTS 席，见
    # `_pick_with_series_cap` 的说明。0.39 再加同源家族配额：要 8 条以上
    # 候选时，一个源方家族最多 5 席，至少留 3 席给别的源。这里只影响
    # 「给多少条」，不改分数与判定。
    _family_slots = 5 if limit >= 8 else 0
    picked, _series_counts, _dropped_reasons = _pick_with_series_cap(
        [c for _, _, c in scored], limit, _series_of,
        family_slots=_family_slots)
    for c in picked:
        n = _series_counts.get(_series_of.get(c.get("dataset_id") or "", ""), 1)
        if n > SERIES_SLOTS:
            # 被折叠掉的不是「不存在」，是「同一个系列里的其他页」：agent 要
            # 全量可以用 dp_catalog 按发布方/前缀取，但不能指望首屏列全。
            c["series_count"] = n
            c["series_note"] = (
                f"同系列另有 {n - SERIES_SLOTS} 条（同一份统计的不同表号/分册），"
                "首屏只列代表条目；要全量用 dp_catalog 按发布方取")
    # 同名兄弟（0.33）：贵州三张清单（资源/初级/再开发）交叉登记，货架上有
    # 17 组同名或只差版本号的条目（36 条）。名字一样时路由没法定序——排序
    # 只能按 dataset_id，agent 拿到的那条可能不是用户看见的那一条。这里如实
    # 透出「同名还有几条」，让 agent 取数前对一眼版本，而不是赌第一条。
    for c in picked:
        n = _name_counts.get(_name_key(c.get("name") or ""), 1)
        if n > 1:
            c["same_name_count"] = n
            c["same_name_note"] = (
                f"货架上另有 {n - 1} 条同名/仅版本号不同的条目（清单交叉登记）；"
                "取数前用 dp_card 对一眼版本与发布方，别赌第一条")
    # 0.29 保底位次：卡没声明度量的真数据，按度量排序会沉到 top3 之外
    # （「快递物流 货运量」货架里唯一的捷克货运量就是这么被判 gap 的）。
    # 只救「主题轴与提问相交 + 结构轴已命中」的那一条，且只占第 3 位，
    # 带 rescued_by 标记——它仍要过 substantive / shelf_gap 的诚实判定，
    # 不会因为露脸就被说成「接得住」。
    # 0.32：保底位次只给「手里有货」的条目。只在册的目录条目没有可取的数，
    # 把它抬到第 3 位只是把「登记在册」摆到显眼处——它不是这条提问的答案。
    lead = [c for _, _, c in scored
            if c.get("unknown_measure") and c.get("topic_lead")
            and c.get("shelf_tier") != "registry_only"]
    if limit >= 3 and lead and not any(
            c.get("unknown_measure") and c.get("topic_lead")
            and c.get("shelf_tier") != "registry_only" for c in picked[:3]):
        best = lead[0]
        already = any(c["dataset_id"] == best["dataset_id"] for c in picked)
        # 它可能已经排在 6、7 位（捷克货运量就是），先摘掉再插到第 3 位，
        # 避免同一条候选出现两次。0.39：它若原本在榜外，插入就是**换一个
        # 席位**而不是把结果集撑大——原先靠 `picked[:limit]` 兜底，会把
        # 尾部那条 `complete` 的 OWID 二氧化碳卡裁掉，于是「各国二氧化碳
        # 排放量」明明有真匹配却 `fully_matched=0`。让位时优先挑末位非
        # complete、且不是别的保底位的候选；一条都挑不出就不救，绿灯优先。
        if not already and len(picked) >= limit:
            victim = next((c for c in reversed(picked)
                           if not (c.get("capability_match") or {}).get("complete")
                           and not c.get("rescued_by")), None)
            if victim is None:
                best = None
            else:
                picked.remove(victim)
                _dropped_reasons[victim["dataset_id"]] = "rescue_displaced"
        if best is not None:
            best["rescued_by"] = "unknown_measure+topic_lead"
            picked = [c for c in picked if c["dataset_id"] != best["dataset_id"]]
            picked.insert(min(2, len(picked)), best)
    # 0.39 闸门位保底（§8）：0.21 立过「收费不是没有数据」，0.38 扩容后
    # 这条承诺被压穿了——系列配额压掉香港表号刷屏，Wind 仍在前 6 之外
    # （名字只吃到 2-gram「金融」的 1.5 分），回答变成 shelf_gap=「无可
    # 接住的能力声明」，等于告诉 agent「没有」。
    # 判据三件套：① 这条是付费/授权且墙后有货（registry_only 的授权目录
    # 不算，0.33 已证明把在册清单当付费路会说谎）；② 名字真对得上这次
    # 提问；③ 候选里没有一条 complete，也没有一条 substantive。
    # 与「度量未声明保底第 3 位」同思路：只给一个候选位，不抬分数、不改
    # 判定、不解锁 complete，且带 rescued_by 让 agent 看得见这是保底位。
    _has_complete = any((c.get("capability_match") or {}).get("complete")
                        for c in picked)
    _has_substantive = any(
        (m.get("quality") == "strong"
         or "topic" in (m.get("verified_axes") or [])
         or (m.get("quality") == "partial"
             and "entity" in (m.get("verified_axes") or [])))
        for m in [c.get("capability_match") or {} for c in picked])
    if limit >= 3 and not _has_complete and not _has_substantive:
        _ids = {c["dataset_id"] for c in picked}
        gate_lead = [c for _, _, c in scored
                     if c.get("gate_worthy") and c.get("name_term_hit")
                     and c["dataset_id"] not in _ids]
        if gate_lead:
            best = gate_lead[0]     # scored 已按分数排序：取最对题的那条墙后源
            best["rescued_by"] = "gate_slot"
            if not best.get("capability_match"):
                # 能力比对没跑出结果时补一个**明确标成占位**的空结构：
                # paywalled 判定要求卡非空，否则整条回答仍会说「货架没有」。
                # gate_slot_only 保证它不会被误读成能力证据。
                best["capability_match"] = {
                    "complete": False, "quality": "weak", "missing": [],
                    "verified_axes": [], "unknown_axes": [], "advisories": [],
                    "evidence": [], "gate_slot_only": True}
            # 不挤掉高分候选：只替换掉排在最后、且本身不是付费路的那条。
            drop = next((c for c in reversed(picked)
                         if not c.get("gate_worthy")), None)
            if drop is not None:
                picked.remove(drop)
                _dropped_reasons[drop["dataset_id"]] = "gate_slot_displaced"
            else:
                for c in picked[limit - 1:]:
                    _dropped_reasons[c["dataset_id"]] = "gate_slot_displaced"
                picked = picked[: limit - 1]
            picked.insert(min(2, len(picked)), best)
    # 两个保底位都只该「换一个席位」，不该把结果集撑大。0.39 加了同源家族
    # 配额后，原本就在 picked 里的保底候选可能落到榜外，insert 会让
    # limit=8 返回 9 条。这里统一收口：保底位插在 ≤2，裁掉尾部不会误伤。
    for c in picked[limit:]:
        _dropped_reasons.setdefault(c["dataset_id"], "score_cut")
    picked = picked[:limit]
    # 落榜原因（0.40）：`gap` 说「这批候选普遍缺什么轴」，但没有回答
    # 「刚才那条看起来更对题的为什么没进首屏」。这里按原始排序取落榜里
    # 位次最高的 3 条，给出**已经在代码里发生过**的剔除原因，不改分数、
    # 不重排、不追加候选。系列/家族配额、两个保底位让位都是真实事件，
    # 其余一律 `score_cut`——不猜、不编理由。
    _final_ids = {c["dataset_id"] for c in picked}
    _REJECT_NOTE = {
        "series_cap": "同一系列（同一份统计的不同表号/分册）已有代表条目在榜，按系列配额让位",
        "family_cap": "同一源方家族在本次大结果集中已占满席位，按家族配额让位",
        "rescue_displaced": "度量未声明保底位插入时，让出了末位席位",
        "gate_slot_displaced": "闸门位保底插入时，让出了末位席位",
        "score_cut": "分数/层位排在 limit 之外，未被本次配额或保底位剔除",
    }
    rejected: list[dict] = []
    for _s, _score, c in scored:
        if c["dataset_id"] in _final_ids:
            continue
        why = _dropped_reasons.get(c["dataset_id"], "score_cut")
        note = _REJECT_NOTE[why]
        if why == "score_cut" and c.get("shelf_tier") == "registry_only":
            # 分层只影响排序（registry_only −4 分），不是一道独立的剔除规则。
            # 所以原因仍是 score_cut，只在说明里补一句，别编一道没发生过的闸门。
            note += "；该条目只在册（registry_only），层位也低于有货的候选"
        rejected.append({
            "dataset_id": c["dataset_id"], "name": c.get("name", ""),
            "score": round(float(_score), 2), "reason": why, "note": note,
        })
        if len(rejected) >= 3:
            break
    # 0.29 兜底降级：没拿到结构化坐标时，不许下「完全满足」的结论。
    # 兜底可以比主路径召回得宽，但不能比主路径更敢下判断。
    if degraded:
        for c in picked:
            cm = dict(c.get("capability_match") or {})
            cm["complete"] = False
            if cm.get("quality") == "strong":
                cm["quality"] = "partial"
            cm["degraded"] = True
            c["capability_match"] = cm
    # 门类体系之外的主题（军事 / 国防）：GB/T 4754-2017 里没有对应门类，
    # 平台**不能**拿几个字面候选冒充「对上了」。要指标/统计的提问没有主体
    # 意图时，所有候选一律不许 complete，并在返回值里明说这是主题缺口；
    # 要主体名录的提问（「军工企业名录」）仍可由工商登记数据接住，只附说明。
    unmapped = need.get("unmapped_topic") or ""
    subject_asked = any(e in ("company", "store", "person", "household")
                        for e in (need.get("entities") or []))
    topic_gap = ""
    if unmapped and not subject_asked:
        topic_gap = (f"「{unmapped}」不在 GB/T 4754-2017 门类体系内，货架上没有"
                     "能声明覆盖该主题的数据集；本次不返回字面候选，"
                     "该主题不在本货架覆盖范围内，也不构成答案存在性证明")
        for c in picked:
            cm = dict(c.get("capability_match") or {})
            cm["complete"] = False
            cm["topic_unverified"] = True
            c["capability_match"] = cm
        # 既然已经确认「货架没有能声明覆盖该主题的数据集」，就不该再交
        # 5 个字面相关的 stranger 给 agent——它们只会把人引去错误的地方。
        # capability.py 开头的约定就是：四条轴都对不上叫缺口，要明说。
        picked = []
    elif unmapped:
        topic_gap = (f"「{unmapped}」不在门类体系内；本次按主体（企业/机构）"
                     "匹配工商登记类数据，不代表覆盖该主题的业务指标")
    # 一条能力声明都接不住时，必须说「货架上没有」。字面相似不等于能力
    # 匹配：CPI 指到空气质量限值、失业率指到识字率、发明专利指到贸易流，
    # 都是「5 个候选但一个都对不上」。候选保留下来只为让 agent 看见有哪些
    # 沾边条目，但每条都要打上 literal_only，不许被当成答案来源。
    substantive: list[dict] = []
    for c in picked:
        m = c.get("capability_match") or {}
        verified = set(m.get("verified_axes") or [])
        # 只在册的条目（平台没持有内容、源方没公开端点）不算「接得住」：
        # 档案对得上，但它今天取不到数。它走下面同一道闸门口径，别混进
        # 「绿灯」。0.32 实测：全量上架后「地表水水质监测断面」被判
        # 完全满足、「社会组织 基金会 登记名录」连缺口都没了——都在拿
        # 官方清单的在册记录冒充已到手的货。
        if m.get("not_held"):
            continue
        # 细粒度主题轴（T13 供水污水）命中就是硬证据：它比 GB/T 门类可靠得多，
        # 不该因为「统计」这类弱度量没对上就被判 mismatch 丢掉。0.14 实测
        # 「法国 城市污水处理率」已召回 Eurostat 真数据、verified 含 topic，
        # 却仍被标「货架上没有」，就是卡在这里。
        # 0.44：主题命中要「没有具名缺口」才算接得住。0.14 加这条时，
        # 命中的是法国污水那种 geo+sector+topic 全中、missing=[] 的真数据；
        # 0.41 接入 2,504 张 NBS 表后，卡片把统计局的树分类**翻译**成大类名
        # （T02/T04…）就标 topic 命中，但同一张卡还明写着「缺产量与产值」。
        # 只认主题、不看缺口，会让「2024 年电影票房」「城镇调查失业率」冒出
        # 8 条 partial 候选、shelf_gap 清空，tool_errors 直接判 `code=OK`——
        # 0.40 盲测守住的「货架无路」信号被一批泛化能力卡冲掉。
        if m.get("quality") == "strong":
            substantive.append(c)
        elif "topic" in verified and not m.get("missing"):
            substantive.append(c)
        # 只有「度量」轴命中的 partial 不算接得住：CPI 的 price 度量能命中
        # 一堆价格表，但主体/行业都没验证过，那不是一条能走的路。
        elif m.get("quality") == "partial" and "entity" in verified:
            substantive.append(c)
        # 行业轴带实测样本的 partial 也算接得住：「贵阳民宿推荐几家」的
        # 本质是找 H 住宿餐饮业门店，全行业商户名录的行业命中带着实测
        # 样本（「旅游饭店 6110 共 5549 条」），结构轴全对、只缺口碑
        # 增强数据。不认它，字面最强的正主会被 shelf_gap 劝退（0.19）。
        # 前提是样本必须真实（evidence 里出现「该类实测」），没样本的纯
        # 词面行业命中仍不算——那可能只是名字里带行业词。
        elif (m.get("quality") == "partial" and "sector" in verified
              and any("该类实测" in h for h in (m.get("evidence") or []))):
            substantive.append(c)
    shelf_gap = ""
    access_gap = ""
    # 付费/授权源单列：它们不是「字面相关」，是「真有这条路，只是要过闸门」。
    # 把收费误报成不存在，agent 就会去换词重试或直接说没有——用户永远不知道
    # 有个付费产品能解决他。反过来，也不能说成「能接住」：墙后有什么没验过。
    # 0.32 起「只在册」的授权目录条目也归这一档：清单在册、端点未公开、
    # 平台不持有内容，和付费源对 agent 的含义是同一句——要谈，不能白跑。
    # 只算「有资格被当成一条路」的那些：连能力比对都没跑出结果（四条轴全不
    # 沾）的授权条目，是字面撞上来的噪音，不该把整条回答改成「要过闸门」。
    # 0.33 实测：「城镇调查失业率」被两条贵州社保登记条目（1.5 分、零轴命中）
    # 顶成 access_gap，而货架上确实没有失业率数据——该说「没有」，不能说
    # 「有，但要授权」。反之，只要对上了哪怕一条轴（哪怕 quality=mismatch），
    # 用户问的那条就在里面，必须给闸门提示。
    paywalled = [c for c in picked
                 if ((c.get("access") or {}).get("level") in ("paid", "authorized")
                     or (c.get("capability_match") or {}).get("not_held"))
                 and c.get("capability_match")]
    if picked and not substantive:
        if paywalled:
            access_gap = (
                "命中 " + str(len(paywalled)) + " 条需付费/需授权的源（"
                + "、".join(c["name"] for c in paywalled[:3])
                + "）：它们在这个方向上确有登记或产品，但取数要过源方闸门；"
                "平台不代销、不代收、不代持凭证，也不保证墙后就有你要的那个指标。"
                "要判断值不值得走，先看 capability_match.not_held、"
                "access.verifiable_before_pay 与 harvest.note")
        else:
            shelf_gap = ("货架上没有能接住这个问题的能力声明；下列候选仅为字面"
                         "相关，平台不认为它们能回答该问题")
        for c in picked:
            if c not in paywalled:
                c["literal_only"] = True
    # 首条就是要过闸门的那条：哪怕候选里另有「能接住」的条目，也得先说清
    # 「你问的这个，排第一的要过源方闸门」。否则 agent 把排第一、看着最对题
    # 的那条当成可直接取数的源。0.33 实测（贵州新源 100 问）：问「铜仁公积金
    # 封存情况」，前三条全是要授权的产品，只因第 4 条是官方供给清单 xlsx
    # 被判「接得住」，整条回答连一句闸门提示都没有——清单能下载不等于那份
    # 业务数据能拿到。
    if not access_gap and picked:
        head = picked[0]
        if head in paywalled:
            head["gate_first"] = True
            access_gap = (
                "排第一的「" + str(head.get("name") or "") + "」是需付费/需授权的源："
                "它最对题，但取数要过源方闸门；平台不代销、不代收、不代持凭证。"
                "候选里若另有可直接打开的条目，先确认它是「清单/目录本身」还是"
                "「你要的那份业务数据」——这两件事在档案里分得很清（"
                "capability_match.not_held / harvest.status）。")
    # 缺口汇总：候选里普遍缺哪一轴。这不是「没找到」，而是「货架在这个方向
    # 上是薄的」——两者对 agent 的下一步完全不同：前者换词重试，后者换路。
    gaps: dict[str, int] = {}
    for c in picked:
        for m in (c.get("capability_match") or {}).get("missing", []):
            gaps[m] = gaps.get(m, 0) + 1
    complete = [c for c in picked
                if (c.get("capability_match") or {}).get("complete")]
    if topic_gap and not picked:
        gap_out = {"主题未覆盖": 1}
    elif shelf_gap:
        gap_out = dict(gaps)
        gap_out.setdefault("无可接住的能力声明", 1)
    elif access_gap:
        gap_out = dict(gaps)
        gap_out.setdefault("需付费/需授权", len(paywalled))
    elif len(complete) < len(picked):
        gap_out = gaps
    else:
        gap_out = {}
    # 一条候选都没有时说清楚「为什么空」。0.28 回归实测：「城镇调查失业率」
    # 返回 total=0、shelf_gap=""，NEXT 却叫 agent 去读 candidates[].capability
    # ——那里什么都没有，agent 只能猜「是不是世上没这数据」。空结果必须带
    # 原因，且原因要区分「货架没有」与「平台没听懂」。
    if not picked and not (topic_gap or shelf_gap or access_gap):
        shelf_gap = (f"{len(items)} 条条目逐条比对后，没有任何一条的能力声明或"
                     "词面能对上这次提问。这不等于「世上没有这份数据」，只等于"
                     "本货架没有可指的路。")
    # 头名过不了闸门时，必须在**顶层**说，而不是只藏在 candidates[0].access 里。
    # 0.28 全量回归实测：agent 取 candidates[0] 就走，撞 WAF / 超时之后才回头
    # 看档案；而档案里 access.level=open（源方不要凭证）与 usage.gate=waf
    # （本机被挡）本身并不矛盾，agent 却容易把「我出不去」读成「这条源坏了」
    # 或「源里没这份数据」。指路指到一条当场走不通的路，就得同时说清楚：
    # 卡在哪、怎么绕、还有没有不用绕的替代。
    gate_first: dict | None = None
    if picked:
        _top = picked[0]
        _acc = _top.get("access") or {}
        _step = _acc.get("next_step") or ""
        if _step and _step != "direct":
            _alt = [c["dataset_id"] for c in picked[1:]
                    if ((c.get("access") or {}).get("next_step") == "direct")]
            gate_first = {
                "dataset_id": _top["dataset_id"],
                "name": _top.get("name", ""),
                "next_step": _step,
                "blocked_by": ((_top.get("usage") or {}).get("gate") or ""),
                "source_level": _acc.get("level"),
                "reason": _acc.get("paywall_note") or "",
                "alternatives_next_step_direct": _alt,
                "note": ("排序第一的这条内容最对题，但**现在这条路走不通**：照 "
                         "next_step 办。source_level 是源方的门槛（open=不要凭证），"
                         "不等于本机取得到——被 WAF 挡或不达是**本机视角**，"
                         "换出口/换 UA 后重试，不要据此告诉用户「源方没有这份数据」。"),
            }
    # 0.39：3.5 万条货架上，扁平按分数取前 5 会被**同一个源方家族**刷屏。
    # 实测「快递物流 货运量」的前 5 条线索全是香港政府物流服务署的表号，
    # 真正想让 agent 看见的跨源线索（捷克货运量 EU 条目）排到第 212 位。
    # 线索位不是给同一家族发 8 张名片：按 dataset_id 的源方前缀每家最多
    # 2 席、总共 8 席，剩下的名额按分数递补给别的家族。给 2 席而不是 1 席
    # 是为了保住「同族里第二条才真对题」的情形（捷克货运量在 eu_ 家族里
    # 排第 2）。只改「给哪几条」，不改分数、判定与 rescued 保底位（那个仍
    # 取全量 scored 里最对题的一条）。
    _lead_family: dict[str, int] = {}
    _lead_out: list[dict] = []
    for _, _, c in scored:
        if not (c.get("unknown_measure") and c.get("topic_lead")):
            continue
        fam = (c["dataset_id"] or "").split("_", 1)[0]
        if _lead_family.get(fam, 0) >= 2:
            continue
        _lead_family[fam] = _lead_family.get(fam, 0) + 1
        _lead_out.append({
            "dataset_id": c["dataset_id"], "name": c["name"],
            "score": c["score"],
            "unknown_axes": (c.get("capability_match") or {}).get("unknown_axes", [])})
        if len(_lead_out) >= 8:
            break
    # 0.40：约束只对**最终入选**的候选出警告。对全量 3.5 万条出警告没有
    # 意义，对召回集出又会随召回波动；agent 要判断的始终是首屏这几条。
    _constraints = termsmod.check_constraints(
        need, picked, _metadata_coverage)
    return {
        "ok": True,
        "query": q,
        "stage": "catalog_only",
        "indexed_sources_searched": False,
        # 平台怎么理解这次提问：把坐标摊开，agent 能立刻看出是不是解析错了。
        # 解析错比匹配错更常见，而它一眼可辨。
        "need": {
            "entities": need["entities"],
            "entity_labels": [capmod.ENTITY_LABEL.get(e, e) for e in need["entities"]],
            "measures": need["measures"],
            "measure_labels": [capmod.MEASURE_LABEL.get(m, m) for m in need["measures"]],
            "sectors": [{"code": c, "name": sectormod.SECTOR_NAME.get(c, c)}
                        for c in need["sectors"]],
            "geo": need["geo"],
            # 0.44：agent 投递的层级照实回显，并把它与字面的分歧摆出来。
            # 回显的意义是让 agent 核对「平台是不是按我说的判」——它若给了
            # county 而平台按 province 走了，这里一眼能看见。
            "geo_level": need.get("geo_level") or "",
            "geo_level_conflict": need.get("geo_level_conflict") or None,
            "indicator": list(need.get("indicator") or []),
            # 街道路名（「衡山路」）：平台不做地名解析，但这条约束是真的，
            # 必须让 agent 看见它没被下推到检索里——取到城市级结果后要自己
            # 按地址字段再筛一遍，别把整城的店当成「附近」。
            "geo_detail": need.get("geo_detail") or "",
            "geo_note": ("街道级地名「" + need["geo_detail"] + "」未结构化：平台不按街名匹配，"
                         "只按城市/区县出候选。候选若带 lat/lng 字段，可自行按距离筛，"
                         "但要先看字段说明里的精度（推断值需逐条复核）"
                         if need.get("geo_detail") else ""),
            "topics": list(need.get("topics") or []),
            "year_from": need.get("year_from"),
            "year_to": need.get("year_to"),
            "unmapped_topic": unmapped,
            "weak_need": bool(need.get("weak_need")),
            # 0.40：6 类需求约束原样摊开。解析错了 agent 一眼能看出来，
            # 不用去猜平台是不是把「免费」当成了某个行业码。
            "freshness_requirement": need.get("freshness_requirement") or "",
            "quality_requirement": need.get("quality_requirement") or [],
            "budget_limit": need.get("budget_limit") or {},
            "purpose": need.get("purpose") or [],
            "granularity": need.get("granularity") or {},
            "regions": need.get("regions") or [],
        },
        # 0.40：约束如何被处理（只警告、不硬过滤；哪些验了、哪些没验）。
        "constraints": _constraints,
        "route_source": route_source,
        "degraded": degraded,
        "need_quality": ("unmapped" if unmapped else
                         "weak" if need.get("weak_need") else "normal"),
        "topic_gap": topic_gap,
        "shelf_gap": shelf_gap,
        # 付费墙信号（0.21）：与 shelf_gap 互斥。shelf_gap=「这条路不存在」，
        # access_gap=「这条路存在但要过闸门」。两者对用户的下一步完全不同。
        "access_gap": access_gap,
        # 头名要过闸门时的顶层提醒（见上面 gate_first 的来由）。无闸门时为 null。
        "gate_first": gate_first,
        "literal_only_candidates": bool(shelf_gap),
        "total": len(picked),
        "fully_matched": len(complete),
        # 0.29：卡没声明度量的「可能接得住但没法验证量」的条目单独列一份。
        # 它们按度量排序会沉到榜外（捷克货运量），不塞进 top3 冒充答案，
        # 但必须让 agent 看得见——看不见才会被读成「货架一条都没有」。
        "unknown_measure_leads": _lead_out,
        "candidates": picked,
        # 0.40：首屏之外的可解释性。只有「确实被本次挑选剔除」的条目，
        # 且原因码来自挑选过程，不是事后猜的语义理由；最多 3 条。
        "rejected": rejected,
        # 有候选但都不完全匹配时，明确说缺什么；没有候选时也一样。
        # 空 dict 表示「该有的都有」，不是「没算」。
        "gap": gap_out,
        "warning": ((topic_gap + "；") if topic_gap else "")
                   + ((shelf_gap + "；") if shelf_gap else "")
                   + ((access_gap + "；") if access_gap else "")
                   + ((f"头名（{gate_first['name'][:24]}）需先过闸门："
                       f"{gate_first['next_step']}；{gate_first['reason']}；")
                      if gate_first else "")
                   + (("坐标缺失已降级：本次没拿到可信的结构化坐标，候选 "
                       "complete 一律 false、quality 上限 partial；请按 dp_map "
                       "归类后带 need 重调 dp_discover，那才是主路径。")
                      if degraded else "")
                   + "目录级发现：仅说明『可能存在这个数据集』，不构成答案存在性证明，不返回值、不出证据包。",
        "NEXT": (
                 # 空结果与「没听懂」是两个不同的死胡同，走法也不同：
                 # 空结果该去门户层找（dp_hub_route），没听懂该回地图重新归类。
                 ("候选为空，" + (topic_gap or shelf_gap)
                  + " 不要读 candidates（没有）；也不要把空结果当成「没有数据」。"
                  "下一步：用 dp_hub_route(query) 在门户层找可检索入口，"
                  "或先用 dp_map 把需求归到 topics/subtopics，带 need 重调 dp_discover。")
                 if not picked else
                 (("need.weak_need=true：平台没从原句里认出主体/度量/主题，"
                   "这次排序基本靠字面，可能把不对题的条目排到前面。"
                   "先用 dp_map 看大类，把需求归类后带 need 重调 dp_discover——"
                   "那才是主路径，query 只作字面辅助。")
                  if need.get("weak_need") else "")
                 + (("gate_first 非空：头名要过闸门（" + gate_first["next_step"]
                     + "），先照它办——换出口/换 UA 重试、挂源方 MCP、或走源方"
                     "授权入口——而不是直接 fetch；这条走不通也不等于源里没有数据。")
                    if gate_first else "")
                 + "读 candidates[].capability.asks 判断这条路能不能接住你的问题；"
                 "capability_match.missing 列出它缺的那一轴（如『数据（缺口碑与评价）』），"
                 "缺的轴不会因为换个词就出现；capability_match.advisories 是使用时"
                 "要注意的精度/口径问题——**不**代表这条路不成立，怎么处理由你按源方"
                 "规范决定。"
                 "**取数看 candidates[].usage**：protocol/endpoint/method 是源方接入"
                 "契约，auth.how_to_get 说明凭证怎么拿，limits 是配额与分页上限。"
                 "要完整交接单调 dp_usage(dataset_id)——它返回启动配置并记账。"
                 "**usage.form=mcp（或带 usage.mcp）时，先挂源方 MCP**：用它的工具、"
                 "按它的 tools/list 参数调用；参数名、翻页、结果组织、字段口径都归"
                 "源方 MCP，档案里的 alternate_http 只是「源方另有 HTTP 接口」的指针，"
                 "不要拿它绕过 MCP（参数名通常不一致）。"
                 "**平台只指路、不代取**：不发请求、不持凭证、不落快照，"
                 "调不通、字段对不上、精度不够都由你按源方规范自行解决。"
                 "topic_gap / shelf_gap 非空时表示货架"
                 "没有能接住这次提问的能力声明：直接告诉用户，改走外部检索，"
                 "不要把 literal_only 的字面候选当答案来源。"
                 "rejected 列出首屏之外位次最高的 3 条及剔除原因码"
                 "（series_cap / family_cap / rescue_displaced / "
                 "gate_slot_displaced / score_cut）："
                 "它是可解释性，不是重算后的第二排名，别拿它当备选答案。"
                 "access.level=paid/authorized 时**不要**当成没有数据：那是源方"
                 "收费墙，看 access.buy_at（源方受理入口）、access.next_step、"
                 "access.verifiable_before_pay（付费前能否验货）；平台不代销、"
                 "不代收，替用户掏钱或索要账号都是越界。harvest.status 说明平台"
                 "有没有真的打开过这条数据：不是 ok 就先确认取数可达（原因见 "
                 "harvest.note），harvest.probed_at / stale 说明这份结论有多旧，"
                 "sample_only 表示只取到一行/预览样本。harvest.endpoint_scope="
                 "different 时这条状态只针对 probed_url（平台敲门的那个地址），"
                 "不是 usage.endpoint 的可用性结论——见 scope_note，别因为一个 "
                 "meta.json 不可解析就放弃能正常取数的接入入口。门户条目的 data_url 为空，"
                 "取数入口在 portal_api_url：portal_api_kind=searchable 才能按关键词"
                 "检索目录，fixed_table / fixed_dataflow / fixed_indicator 只是单表、"
                 "固定数据流或单一指标的示例地址，搜不到结果不等于门户没有数据。"
                 "need 是平台对你这次提问的解析，解析错了就换个说法重问。"),
    }


def card(item: dict, link: dict | None = None) -> dict:
    """单张条目卡：agent 判断「值不值得去」所需的最小字段集。

    比 ``detail()`` 小一个量级（detail 是后台管理视图，100+ 字段）。卡里
    每个字段都要能回答一个决策问题：是什么类、什么形态、能不能取证、
    怎么取、有什么坑。答不上的字段就不放进来——留空的键只是噪音。
    """
    i = normalize(item)
    r = record(i, link)
    cls = tx.classify(i)
    return {
        "dataset_id": r["dataset_id"],
        "name": r["name"],
        "publisher": r["publisher"],
        "entity_kind": r["entity_kind"],
        "l1": r["l1"], "l1_name": r["l1_name"],
        "sub": r["sub"], "sub_name": r["sub_name"],
        "form": r["form"], "form_name": r["form_name"],
        "form_basis": r["form_basis"],
        "use": r["use"], "use_basis": r["use_basis"],
        "taxonomy_confidence": r["taxonomy_confidence"],
        "taxonomy_reason": r["taxonomy_reason"],
        "native_topics": r["native_topics"],
        "native_topic_labels": r["native_topic_labels"],
        "distributions": r["distributions"],
        "summary": r["summary"],
        "summary_lang": r["summary_lang"],
        "native_keywords": r["native_keywords"],
        "meta_source": r["meta_source"],
        "parent_hub": r["parent_hub"], "parent_relation": r["parent_relation"],
        "access_label": r["access_label"], "is_paid": r["is_paid"],
        "proof_available": bool(i.get("linked_dataset_id")),
        "evidence_label": r["evidence_label"], "weak_evidence": r["weak_evidence"],
        "upstream_status": r["upstream_status"],
        "upstream_scope": r["upstream_scope"],
        "upstream_scope_note": r["upstream_scope_note"],
        "endorsement_label": (r.get("endorsement") or {}).get("label", ""),
        "fields_count": r["fields_count"],
        "field_doc_level": r["field_doc"]["level"],
        "declared_records": i.get("declared_records") or 0,
        # 覆盖声明：这条数据"能回答哪一类问题"的唯一权威来源。有它，agent
        # 不必靠名称猜；没有它就留空，不替数据集编造覆盖范围。
        "coverage": i.get("coverage") or {},
        # 0.40：卡片是 agent 决定「要不要去」的最后一站，许可与时间口径
        # 必须在这里就能看见，别等撞了墙再回头查。
        "license_summary": r["license_summary"],
        "source_metadata": r["source_metadata"],
        # 0.41：表内定位器摘要。卡片是「要不要去」的最后一站，若这张表已经
        # 登记了表内坐标，就把「几个指标、时间范围、请求形态是否验通」摆出来。
        "locator_view": locatormod.describe(i.get("locator") or {}),
        # 能力声明：比 coverage 更宽的一层——不只行业，还包括主体、度量、
        # 地理，以及「能接住什么问题」的现成句子。agent 拿它直接判断值不值
        # 得去，不必读完名称再猜。basis 标明这份说明是发布方给的（declared）
        # 还是按目录元数据推的（derived）——推出来的要回源核对。
        "capability": i.get("capability") or capmod.declare(i),
        "notes_brief": (i.get("notes") or "")[:200],
        "data_url": r["data_url"], "source_url": r["source_url"],
        "portal_api_url": r["portal_api_url"],
        "portal_api_kind": r.get("portal_api_kind", ""),
        "portal_api_basis": r["portal_api_basis"],
        "catalog_url": r["catalog_url"],
        # 完整接入档案 + 闸门状态（0.22）：卡片是「要不要去、去了怎么连」的
        # 最终依据。这里给全量示例（curl / MCP 配置），因为已经是单条详情，
        # 不存在候选列表的 token 预算问题。
        "usage": profilemod.handoff_view(profilemod.build(i)),
        "usage_errors": profilemod.validate(profilemod.build(i)),
        "access": _access_block(
            i, i.get("harvest") if isinstance(i.get("harvest"), dict) else {},
            str((i.get("usage") or {}).get("gate") or "")),
        # 平台增益（第三条建议）：交易所与官方附件两类，裸爬几乎与我们等价。
        # 卡上必须写清「我们补的是什么」——不写，agent 就会照着「帮你找到」
        # 去讲，兑现不了。空 dict = 没有实测也没有可推的形态，不硬凑。
        "expectation": expectation(i),
        # 401 证据链：把「要登录」从结论变成可复现的记录。agent 撞墙时才分得
        # 清「是我找错了」还是「本来就要授权」。
        "access_evidence": i.get("access_evidence") or {},
        "availability": r["availability"],
        "keywords": list(i.get("discovery_keywords") or [])[:8],
    }


def _family_summary(name: str, summary: str, limit: int = 90) -> str:
    """族卡上的一句话摘要，只在它比名称多说了东西时才给。

    169 条有描述，其中 43 条只是把名称复述一遍（"Statistiques garderie
    périscolaire depuis septembre 2015"）。族卡是压缩层，平均 1.9KB/子类，
    每条都挂摘要会涨四成——所以重复的直接不挂，有增量的截 90 字。
    """
    s = (summary or "").strip()
    if not s:
        return ""
    norm = lambda x: re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", (x or "").lower()).strip()  # noqa: E731
    ns, nn = norm(s), norm(name)
    if nn and nn[:22] in ns:
        return ""
    return s if len(s) <= limit else s[:limit].rstrip(" ,;:") + "…"


def index_view(items: list[dict], domain: str | None = None,
               sub: str | None = None, limit: int = 30) -> dict:
    """三级读取：门类清单 → 子类族卡 → 单张卡。

    这是把「237 条全量卡片」压成「按需读一条路」的关键。实测全量卡
    29,824 字，族归并后 15,056 字（2.0×），而只读一个子类的族卡约 500 字——
    **压缩来自分层读取，不来自分得更细**（细分只会让条目散到更多桶里）。

    三级之间用 ``NEXT`` 串起来，agent 不需要记住调用顺序。
    """
    rows = [(i, tx.classify(i)) for i in items]
    D = [(i, c) for i, c in rows if c["kind"] == "D"]

    # ── 一级：门类清单 ──
    if not domain:
        out = []
        for code in tx.L1_ORDER:
            if code == "T00":
                continue
            g = [(i, c) for i, c in D if c["l1"] == code]
            out.append({
                "domain": code, "name": tx.L1_NAME[code], "scope": tx.L1_DEF[code],
                "datasets": len(g),
                "indexed": sum(1 for i, _c in g if i.get("linked_dataset_id")),
            })
        t00 = [(i, c) for i, c in D if c["l1"] == "T00"]
        return {
            "ok": True, "level": 1, "total_datasets": len(D),
            "domains": out,
            **({"unsorted": [{"dataset_id": i["dataset_id"], "name": i["name"]}
                             for i, _c in t00[:10]]} if t00 else {}),
            "note": "indexed 列 = 已建字段级索引、能出证据的条数；其余只能指路。",
            "NEXT": "选定门类后调 dp_index(domain=门类码) 看子类与族规模。",
        }

    code = str(domain).strip().upper()
    if code not in tx.L1_NAME:
        return {"ok": False, "reason": "unknown_domain", "domain": code,
                "valid": [c for c in tx.L1_ORDER if c != "T00"],
                "NEXT": "不带参数调 dp_index() 可取全部门类。"}
    g = [(i, c) for i, c in D if c["l1"] == code]

    # ── 二级：子类清单（带族规模，让 agent 知道下一层要读多少）──
    if not sub:
        subs = []
        for s in tx.subs_of(code):
            sg = [(i, c) for i, c in g if c["sub"] == s["sub"]]
            if not sg and s["fallback"]:
                continue
            subs.append({
                "sub": s["sub"], "name": s["name"], "fallback": s["fallback"],
                "datasets": len(sg),
                "families": len({c["family"] for _i, c in sg}),
                "indexed": sum(1 for i, _c in sg if i.get("linked_dataset_id")),
            })
        return {
            "ok": True, "level": 2, "domain": code, "name": tx.L1_NAME[code],
            "scope": tx.L1_DEF[code], "datasets": len(g),
            "subs": subs,
            "note": "families 是去重后的族数（同指标不同年份/阈值算一个族）；"
                    "读族卡比逐条读省 2 倍。",
            "NEXT": f"调 dp_index(domain='{code}', sub='子类码') 取族卡。",
        }

    sub_code = str(sub).strip().upper()
    sg = [(i, c) for i, c in g if c["sub"] == sub_code]
    if not sg:
        return {"ok": False, "reason": "unknown_sub", "domain": code, "sub": sub_code,
                "valid": [s["sub"] for s in tx.subs_of(code)],
                "NEXT": f"dp_index(domain='{code}') 列出该类子类。"}

    # ── 三级：族卡 ──
    fams: dict[str, list[tuple[dict, dict]]] = {}
    for i, c in sg:
        fams.setdefault(c["family"] or i["dataset_id"], []).append((i, c))
    cards = []
    for _key, members in sorted(fams.items(), key=lambda kv: -len(kv[1])):
        # 族代表条目：优先能出证据的，其次有直链的，最后取名字最短的
        best, bc = max(members, key=lambda m: (
            bool(m[0].get("linked_dataset_id")),
            bool(m[0].get("data_url")),
            -len(m[0].get("name") or "")))
        cards.append({
            "dataset_id": best["dataset_id"],
            "name": best["name"],
            "variants": len(members),
            "form": bc["form"], "form_name": bc["form_name"],
            "use": bc["use"],
            "summary": _family_summary(best["name"], enrich.summary_of(best)),
            "proof_available": bool(best.get("linked_dataset_id")),
            "access_label": catalog.ACCESS_LABELS.get(best.get("access_level") or "", ""),
            "data_url": (best.get("data_url") or "").strip(),
            # 薄索引不带 evidence（那是来源证据链，档案里才有）：缺就是「未标注」，
            # 与 normalize 的口径一致，不能因为读的是薄记录就 KeyError。
            "weak_evidence": ((best.get("evidence") or {}).get("level", "")
                              in WEAK_EVIDENCE),
        })
    cards = cards[:limit]
    return {
        "ok": True, "level": 3, "domain": code, "domain_name": tx.L1_NAME[code],
        "sub": sub_code, "sub_name": tx.SUB_NAME.get(sub_code, "—"),
        "families": len(fams), "shown": len(cards),
        "cards": cards,
        "NEXT": "选定 dataset_id 后 dp_card(dataset_id) 看完整卡；"
                "能出证据的直接 dp_proof，否则连源方并先 dp_pitfalls。",
    }


def stats(items: list[dict]) -> dict:
    # 0.40：这里原本是 `[record(i) for i in items]`——为了数 8 个布尔字段，
    # 把 3.5 万条完整目录行（含分类、源方许可、字段文档）全造了一遍，
    # 在 build_shelf 里独占 ~17s。下面直接数原始条目上的同一批字段：
    # normalize() 对这些键的默认值已逐一对齐（published 缺省=True、
    # access_level 缺省=registered、probe.upstream_status 缺省=unchecked、
    # evidence.level 缺省=''），统计结果与旧实现逐键相等。
    c: Counter = Counter()
    for i in items:
        c["published" if i.get("published") is not False else "unpublished"] += 1
        c["indexed" if i.get("linked_dataset_id") else "catalog_only"] += 1
        probe = i.get("probe") or {}
        st = probe.get("upstream_status") or "unchecked"
        if st == "reachable":
            c["reachable"] += 1
            if probe_scope(i.get("source_url") or "") == "site_root":
                c["reachable_site_root"] += 1
        elif st in ("unreachable", "auth_failed"):
            c[st] += 1
        acc = i.get("access_level") or "registered"
        if acc == "paid":
            c["paid"] += 1
        if acc != "open":
            c["gated"] += 1
        if ((i.get("evidence") or {}).get("level") or "") in WEAK_EVIDENCE:
            c["weak_evidence"] += 1
    # 接入档案覆盖率（0.22）：这是运营待办，不是给 agent 看的。
    # derived 表示只有入口和鉴权类型——够指路，不够照着调；source/operator
    # 才是真登记过。
    prof_basis = {}
    for i in items:
        b = str((profilemod.build(i) or {}).get("basis") or "derived")
        prof_basis[b] = prof_basis.get(b, 0) + 1
    return {
        "total": len(items),
        # 上架账：后台看全量，agent 只看 published。两个数分开给，避免
        # 「后台有 775 条、agent 只见 700 条」时没人能说清差在哪。
        "published": c["published"],
        "unpublished": c["unpublished"],
        "indexed": c["indexed"],
        "catalog_only": c["catalog_only"],
        "reachable": c["reachable"],
        # 可达但只打到站点根的，单列出来：这批发 "reachable" 但不证明条目存在
        "reachable_site_root": c["reachable_site_root"],
        "unreachable": c["unreachable"],
        "auth_failed": c["auth_failed"],
        "paid": c["paid"],
        "gated": c["gated"],
        "weak_evidence": c["weak_evidence"],
        "profile_basis": prof_basis,
        # 接入形态的覆盖账：多少条能「按关键词自己检索」、多少条只有网页、
        # 多少条本机验证不了。这是「档案补齐到哪一步」的可见进度。
        "profile_forms": (lambda t: {"forms": {k: v["count"] for k, v in t["forms"].items()},
                                     "gates": t["gates"],
                                     "complete": t["complete"],
                                     "total": t["total"]})(
            __import__("bdsp.forms", fromlist=["taxonomy"]).taxonomy(
                [i.get("usage") or {} for i in items])),
    }


def probe_scope(url: str) -> str:
    """探针打的是哪一层：站点根 / 具体落地页。

    这个区分不是为了好看：19 条 ``third_party_aggregator`` 条目的
    ``source_url`` 是聚合站首页，首页**永远** 200，所以探针结果恒为
    ``reachable``——19 条记录共享同一个值，这本身就是"没测到东西"的信号。
    站点根可达只证明"这个站还活着"，不证明"这条数据存在"。
    """
    p = urlparse(url or "")
    return "detail_page" if (p.path.strip("/") or p.query) else "site_root"


def scope_note(scope: str, weak: bool) -> str:
    """这次可达值多少。空串表示「打了落地页，结果就该这么读」。"""
    if scope != "site_root":
        return ""
    # 不改 upstream_status（它答的就是"上游站此刻通不通"），而是把这次可达的
    # **证明力**写在旁边。两个字段各答各的问题，不互相冒充。
    return ("站点根可达只说明该站存活；不构成这条数据存在的证据"
            + ("（且证据本身来自第三方聚合站，首页恒 200）" if weak else ""))


def probe_one(item: dict, timeout: float = 6.0) -> dict:
    """轻量可达性巡检（不拉数据内容）。记账背书依据：

    - streak：连续可达天数（同日复检不重复累加）
    - history：最近 14 次检查（date/ok/http_status）
    - last_ok_at：最近一次可达时间（用于降级标注）
    - scope / scope_note：探的是站点根还是落地页，以及这次可达值多少
    """
    i = normalize(item)
    code, note, status = catalog.probe_endpoint(i["source_url"], timeout=timeout)
    ok = code == "reachable"
    today = now_iso()[:10]
    prev = dict(item.get("probe") or {})
    prev_hist = [h for h in (prev.get("history") or []) if isinstance(h, dict)]
    counted_today = any(h.get("date") == today and h.get("ok") for h in prev_hist)
    scope = probe_scope(i["source_url"])
    weak = i.get("evidence", {}).get("level") in WEAK_EVIDENCE

    probe = {
        "upstream_status": code,
        "note": note,
        "http_status": status,
        "checked_at": now_iso(),
        "scope": scope,
    }
    if ok:
        probe["scope_note"] = scope_note(scope, weak)
    if ok:
        base = int(prev.get("streak") or 0)
        probe["streak"] = base if counted_today else base + 1
        probe["last_ok_at"] = now_iso()
    else:
        probe["streak"] = 0
        if prev.get("last_ok_at"):
            probe["last_ok_at"] = prev["last_ok_at"]
    hist = [h for h in prev_hist if h.get("date") != today]
    hist.append({"date": today, "ok": ok, "http_status": status})
    probe["history"] = hist[-14:]

    # 取数面双探（FRED 案例教训）：source_url 可达 ≠ agent 实际走的 data_url
    # 可达（UA 敏感 / 需凭证 / 404 都可能只发生在 data 面）。双探分开记录，
    # 不翻转主背书——主背书仍锚定 source_url（发现面），data 面作为补充情报。
    data_url = (i.get("data_url") or "").strip()
    if data_url and data_url != i["source_url"]:
        d_code, d_note, d_status = catalog.probe_endpoint(data_url, timeout=timeout)
        probe["data_endpoint"] = {
            "status": d_code, "http_status": d_status, "note": d_note[:80],
        }
    return probe


def endorsement(i: dict) -> dict:
    """巡检背书：只背「源可达性」，绝不背「内容质量」。

    这是路牌模式的信任来源——平台不持有数据，但用每日巡检的实测记录
    告诉 agent：这个路牌指向的源现在通不通、通了多久。与数据体量无关，
    与内容正确性无关（后者永远以源方与许可为准）。
    """
    probe = i.get("probe") or {}
    if i.get("linked_dataset_id"):
        return {"level": "indexed", "label": "平台实测索引",
                "note": "内容已建字段级索引，参与 route/proof"}
    st = probe.get("upstream_status")
    streak = int(probe.get("streak") or 0)
    if st == "reachable":
        if streak >= 7:
            return {"level": "patrol_stable",
                    "label": f"在检 · 连续 {streak} 天可达",
                    "note": "每日巡检实测；只代表源可达，不代表内容质量"}
        return {"level": "patrol_new",
                "label": f"在检 · 可达（观察第 {max(streak, 1)} 天）",
                "note": "连续 7 天可达后升级为稳定背书"}
    if st in ("unreachable", "auth_failed", "error"):
        if probe.get("last_ok_at"):
            return {"level": "patrol_degraded",
                    "label": "巡检异常",
                    "note": f"最近一次可达 {probe['last_ok_at'][:10]}；保留路牌并持续观察"}
        return {"level": "patrol_down", "label": "巡检不可达",
                "note": "从未探通；路牌保留，谨慎使用"}
    if st in ("local", "skipped"):
        return {"level": "local", "label": "本地源 / 免检", "note": ""}
    return {"level": "unchecked", "label": "未巡检",
            "note": "尚无巡检记录，不构成任何背书"}
