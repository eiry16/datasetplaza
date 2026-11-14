# -*- coding: utf-8 -*-
"""踩坑留言区（pitfall notes）：agent 众包的「遇坑记录」，平台只过手经验不过手数据。

设计红线（2026-11-07 与操作员定稿）：
  1. 写入必须走 MCP 工具（dp_report_pitfall），不给裸文件系统写权限——
     强制 agent 标识、时间戳、现象/绕行分栏，操作员可审计可清理（防污染）。
  2. 留言区只背「坑」（怎么绕开），不背「内容对错」——与巡检只背可达性
     同一条诚实原则，不得沦为平行数据源或评分区。
  3. 文件按需创建：pitfalls/<dataset_id>.md 存在即代表有真实踩坑记录，
     不存在的条目读取返回「尚无留言」，不预生成空文件。

md 节格式（机器可数、人可读）：
    ## 2026-11-07 21:50 · <agent>
    - 现象：...
    - 绕行：...
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "pitfalls"

_MAX = {"agent": 80, "issue": 2000, "workaround": 2000}
_SECTION = re.compile(r"^## 20\d{2}-\d{2}-\d{2}", re.M)

HEADER = """# 踩坑留言区 · {dataset_id}

> 本文件由 agent 经 dp_report_pitfall 写入、dp_pitfalls 读取：只记「怎么绕开」，
> 不评「内容对错」。每条必含时间与 agent 标识；绕行方法须自带实测依据。
> 操作员保留清理与合并权。格式：`## YYYY-MM-DD HH:MM · agent` + 现象/绕行。

"""


def pitfall_path(dataset_id: str, pit_dir: Path | None = None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(dataset_id))
    return (pit_dir or DEFAULT_DIR) / f"{safe}.md"


def read_pitfalls(dataset_id: str, pit_dir: Path | None = None) -> dict:
    p = pitfall_path(dataset_id, pit_dir)
    if not p.exists():
        return {"ok": True, "dataset_id": dataset_id, "count": 0,
                "entries": [], "note": "尚无留言：本条目还没有 agent 记录过坑。"}
    text = p.read_text(encoding="utf-8")
    # finditer 逐节提取：头行（## 20xx-xx-xx）到下一个头行之间；首个头之前的
    # 文件引言（HEADER）不算节。
    heads = list(_SECTION.finditer(text))
    entries = []
    for i, m in enumerate(heads):
        start = m.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        block = text[start:end].strip()
        head, _, body = block.partition("\n")
        entries.append({"header": head.strip(), "body": body.strip()})
    entries.reverse()  # 最新在前
    return {"ok": True, "dataset_id": dataset_id, "count": len(entries),
            "entries": entries, "file": str(p)}


def append_pitfall(dataset_id: str, agent: str, issue: str,
                   workaround: str = "", pit_dir: Path | None = None,
                   valid_ids: set[str] | None = None) -> dict:
    """追加一条踩坑记录。valid_ids 提供时校验条目存在（防垃圾目录膨胀）。"""
    ds = str(dataset_id or "").strip()
    ag = str(agent or "").strip() or "unknown-agent"
    iss = str(issue or "").strip()
    wa = str(workaround or "").strip()
    if not ds:
        return {"ok": False, "reason": "empty_dataset_id"}
    if not iss:
        return {"ok": False, "reason": "empty_issue",
                "hint": "现象描述必填：碰到什么错/什么边界，别只写「不行」。"}
    if valid_ids is not None and ds not in valid_ids:
        return {"ok": False, "reason": "unknown_dataset_id",
                "hint": "dataset_id 必须来自 dp_route/dp_discover/dp_catalog 的候选。"}
    for key, val in (("agent", ag), ("issue", iss), ("workaround", wa)):
        if len(val) > _MAX[key]:
            return {"ok": False, "reason": "too_long", "field": key, "max": _MAX[key]}

    p = pitfall_path(ds, pit_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = f"## {ts} · {ag}\n- 现象：{iss}\n"
    if wa:
        block += f"- 绕行：{wa}\n"
    if not p.exists():
        with p.open("w", encoding="utf-8", newline="\n") as f:
            f.write(HEADER.format(dataset_id=ds))
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(block + "\n")
    return {"ok": True, "dataset_id": ds, "file": str(p), "count": read_pitfalls(ds, pit_dir)["count"]}


def pitfall_counts(dataset_ids: list[str] | None = None,
                   pit_dir: Path | None = None) -> dict[str, int]:
    """扫描留言区目录 → {dataset_id: 条数}。不传 ids 返回全量。"""
    d = pit_dir or DEFAULT_DIR
    if not d.exists():
        return {}
    out: dict[str, int] = {}
    for f in d.glob("*.md"):
        if f.name == "README.md":
            continue
        ds = f.stem
        if dataset_ids is not None and ds not in dataset_ids:
            continue
        out[ds] = len(list(_SECTION.finditer(f.read_text(encoding="utf-8"))))
    return out
