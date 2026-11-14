"""把二进制容器里的表格**真的读出来**，再交给 harvest 造能力卡。

为什么单独一层
--------------
之前 ``binary`` 只有一句「返回 Excel/PDF/压缩包，需另做表格解析」。这不是
能力卡，是欠条：agent 照样不知道里面有几列、叫什么、能问什么。而把二进制
按文本行硬读更坏——会造出 ``paragraph_no/text`` 两个根本不存在的字段，
比没有卡更误导人（harvest.py 开头已经写过这条教训）。

所以这里只做一件事：**按容器格式解出真实的记录行**，产出与 CSV/JSON 完全
同构的 ``list[dict]``，由 ``build_evidence`` 统一数行、数列、抽样本值。

支持的容器（全部纯标准库，不引入 openpyxl/xlrd/pandas）
--------------------------------------------------------
* ``xlsx``  OOXML：zip + sharedStrings + worksheet XML
* ``xls``   OLE2 复合文档 + BIFF8 记录流（共享字符串 SST / LABELSST / NUMBER / RK / MULRK）
* ``dbf``   shapefile 压缩包里的属性表（也可直接是 .dbf）
* ``zip``   包内的 csv / json 表格
* ``pdf``   文本层里能稳定分列的表；抽不出表格就明说，不编字段

诚实边界
--------
* Excel 日期是数字序列号，这里**不做**日期换算（换算需要工作簿样式表，猜错
  比不猜更坏）。样本值里看到 44xxx 就是 44xxx。
* 扫描版 PDF / 纯图形表格抽不出文字行，返回 ``ok=False``，状态
  ``binary_nontable``，并把容器里实际看到的文件清单/文本行数写进 note，
  让「为什么补不了卡」可复核。
* 只读第一个工作表 / 前 ``MAX_ROWS`` 行。多表文件会在 note 里写明。
"""
from __future__ import annotations

import csv
import io
import json
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET

MAX_ROWS = 20_000
MAX_COLS = 60
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
FREE = 0xFFFFFFFF
END = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD


def _result(ok: bool, container: str, records: list[dict] | None = None,
            note: str = "", **extra) -> dict:
    out = {"ok": ok, "container": container, "records": records or [], "note": note}
    out.update(extra)
    return out


def _clean_header(value: str, idx: int) -> str:
    """列名必须像列名。解错格式的 BIFF/PDF 会吐出几千个乱码字符，拿它当
    字段比没有字段更坏——一律退回中性的 col_N，并在 note 里说明。"""
    v = "".join(ch for ch in str(value).strip() if ch.isprintable())
    if not v or len(v) > 60 or not re.search(r"[A-Za-z0-9\u0370-\u03ff\u4e00-\u9fff]", v):
        return f"col_{idx + 1}"
    odd = sum(1 for ch in v if not (
        "\x20" <= ch <= "\x7e" or "\u00c0" <= ch <= "\u024f"
        or "\u0370" <= ch <= "\u03ff" or "\u4e00" <= ch <= "\u9fff"
        or ch in "，。、（）()％%－-"))
    if odd / len(v) > 0.15:
        return f"col_{idx + 1}"
    return v


def _garbled(value: str) -> bool:
    """格式解错时常见的乱码：一大片 CJK 扩展区/谚文/私用区字符。
    留着当样本值会污染能力卡，直接置空比展示垃圾诚实。"""
    if len(value) < 20:
        return False
    odd = sum(1 for ch in value
              if ord(ch) > 0x2FFF and not (0x4E00 <= ord(ch) <= 0x9FFF))
    return odd / len(value) > 0.2


def _rows_to_records(rows: list[list], strict: bool = False) -> list[dict]:
    """首行当表头；前面若有标题行（单列大标题）先跳过。

    ``strict`` 给 PDF 用：表头必须短、像字段名，且至少两列，否则宁可返回
    空表让调用方标「不可还原」，也不把一整句报告文字拆成假列。
    """
    rows = [r for r in rows if any(str(v).strip() for v in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows[:20])
    if strict and width < 2:
        return []
    header_idx = None
    for idx, r in enumerate(rows[:12]):
        vals = [str(v).strip() for v in r[:MAX_COLS]]
        nonempty = [v for v in vals if v]
        if len(nonempty) < 2:
            continue
        if not any(not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", v) for v in nonempty):
            continue  # 全是数字的一行是数据或年份表头，不是字段名
        if any(re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", v) for v in nonempty):
            continue  # 夹着数字的一行多半是首条记录，不是表头
        ok = True
        for i, v in enumerate(vals):
            if not v:
                continue
            cleaned = _clean_header(v, i)
            if len(v) > 40:
                ok = False
                break
            # PDF 里整句报告文字常被空白切成「列」；列名里带长数字串
            # 说明那一行其实是数据，不是表头。
            if strict and (cleaned == f"col_{i + 1}" or re.search(r"\d[\d,.\s]{8,}", v)):
                ok = False
                break
        if ok:
            header_idx = idx
            break
    if header_idx is None:
        if strict:
            return []
        headers = [f"col_{i + 1}" for i in range(min(width, MAX_COLS))]
        body = rows
    else:
        head = [str(v).strip() for v in rows[header_idx][:MAX_COLS]]
        while len(head) < min(width, MAX_COLS):
            head.append("")
        headers = [(_clean_header(h, i) if h else f"col_{i + 1}")
                   for i, h in enumerate(head)]
        # 重名列会让后面的值互相覆盖，直接补后缀
        seen: dict[str, int] = {}
        for i, h in enumerate(headers):
            if h in seen:
                seen[h] += 1
                headers[i] = f"{h}_{seen[h]}"
            else:
                seen[h] = 0
        body = rows[header_idx + 1:]
    records: list[dict] = []
    for r in body[:MAX_ROWS]:
        rec = {}
        for i, h in enumerate(headers):
            v = r[i] if i < len(r) else ""
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            if isinstance(v, str):
                v = "".join(ch for ch in v if ch.isprintable())
                if len(v) > 200 or _garbled(v):
                    v = ""
            rec[h] = "" if v is None else v
        if any(str(v).strip() for v in rec.values()):
            records.append(rec)
    return records


# ── OOXML / xlsx ────────────────────────────────────────────────────────

def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(0, n - 1)


def _xlsx_shared(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    out = []
    for si in root.findall(f"{NS}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    return out


def _xlsx_sheet_names(zf: zipfile.ZipFile) -> list[str]:
    names: list[str] = []
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        for s in wb.iter(f"{NS}sheet"):
            names.append(s.get("name") or "")
    except (KeyError, ET.ParseError):
        pass
    return names


def _xlsx(raw: bytes) -> dict:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        return _result(False, "zip", note=f"ZIP 容器损坏：{str(exc)[:80]}")
    names = zf.namelist()
    if "xl/workbook.xml" not in names and not any(n.startswith("xl/worksheets/") for n in names):
        return _zip(raw, zf=zf)
    shared = _xlsx_shared(zf)
    sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    sheet_names = _xlsx_sheet_names(zf)
    if not sheets:
        return _result(False, "xlsx", note="OOXML 里没有工作表 XML")

    def read_sheet(path: str) -> list[list]:
        rows: list[list] = []
        try:
            root = ET.fromstring(zf.read(path))
        except (KeyError, ET.ParseError, zlib.error):
            return rows
        for row in root.iter(f"{NS}row"):
            cells: list = []
            for c in row.findall(f"{NS}c"):
                idx = _col_index(c.get("r") or "")
                if idx > MAX_COLS:
                    continue
                while len(cells) <= idx:
                    cells.append("")
                t = c.get("t") or "n"
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{NS}t"))
                else:
                    v = c.find(f"{NS}v")
                    val = "" if v is None or v.text is None else v.text
                    if t == "s":
                        try:
                            val = shared[int(val)]
                        except (ValueError, IndexError):
                            val = ""
                    elif t == "b":
                        val = "TRUE" if val == "1" else "FALSE"
                cells[idx] = val
            rows.append(cells)
            if len(rows) >= MAX_ROWS + 1:
                break
        return rows

    # 多表工作簿的第一张常常是 Table of Contents / 说明页。0.13 之前跳页
    # 词表漏了 "About"，爱尔兰税务统计（ITXS01）的 About 说明页被当成
    # 记录表，发出一张 ['Code', 'ITXS01'] 的 probed 能力卡。而且「取第一张
    # 勉强及格的表」本身就不稳：说明页也常有 2 列 25 行。改成在所有非说明
    # 页里挑**行列规模最大**的那张，说明页就再也抢不到位置。
    chosen, chosen_name, records = "", "", []
    best_size = -1
    for i, path in enumerate(sheets[:12]):
        name = sheet_names[i] if i < len(sheet_names) else path
        if re.search(r"contents|read.?me|info|note|cover|about|metadata|"
                     r"legend|glossary|sommaire|index|说明|目录|封面|凡例|"
                     r"注解|表头说明", name, re.I):
            continue
        recs = _rows_to_records(read_sheet(path))
        if not recs or len(recs[0]) < 2 or len(recs) < 2:
            continue
        size = len(recs) * len(recs[0])
        if size > best_size:
            best_size = size
            chosen, chosen_name, records = path, name, recs
    if not records:
        return _result(False, "xlsx", note="所有工作表都没有可还原的记录行"
                       "（说明页/目录页不算记录表）", sheet_count=len(sheets))
    note = f"OOXML 工作表「{chosen_name}」"
    if len(sheets) > 1:
        note += f"，共 {len(sheets)} 个工作表，取行列规模最大的记录表"
    note += "；Excel 日期序列号未做换算"
    return _result(True, "xlsx", records, note, sheet=chosen_name,
                   sheet_count=len(sheets))


# ── OLE2 / xls (BIFF) ───────────────────────────────────────────────────

def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _chain(fat: list[int], start: int, limit: int = 100_000) -> list[int]:
    out, cur, seen = [], start, set()
    while cur < len(fat) and cur not in (END, FREE, FATSECT) and cur not in seen:
        seen.add(cur)
        out.append(cur)
        cur = fat[cur]
        if len(out) > limit:
            break
    return out


def _ole_streams(raw: bytes) -> dict[str, bytes]:
    """解析 CFB 复合文档，返回 {流名: 字节}。只认 Workbook/Book 这类小流。"""
    if len(raw) < 512 or raw[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("不是 OLE2 复合文档")
    ssize = 1 << _u16(raw, 30)
    msize = 1 << _u16(raw, 32)
    cutoff = _u32(raw, 56)
    first_dir = _u32(raw, 48)
    first_minifat = _u32(raw, 60)
    num_fat = _u32(raw, 44)
    difat = [_u32(raw, 76 + 4 * i) for i in range(109)]

    def sector(sid: int) -> bytes:
        off = 512 + sid * ssize
        return raw[off: off + ssize]

    # DIFAT 超长时顺着链补
    cur, guard = _u32(raw, 68), 0
    while cur not in (END, FREE) and cur < len(raw) // ssize and guard < 100:
        s = sector(cur)
        difat.extend(_u32(s, 4 * i) for i in range(ssize // 4 - 1))
        cur = _u32(s, ssize - 4)
        guard += 1
    fat: list[int] = []
    for sid in difat[:num_fat]:
        if sid in (END, FREE, FATSECT):
            continue
        s = sector(sid)
        fat.extend(_u32(s, 4 * i) for i in range(ssize // 4))
    if not fat:
        raise ValueError("OLE2 FAT 为空")

    def read_chain(sids: list[int]) -> bytes:
        return b"".join(sector(s) for s in sids)

    dir_bytes = read_chain(_chain(fat, first_dir))
    entries = []
    for off in range(0, len(dir_bytes) - 127, 128):
        e = dir_bytes[off: off + 128]
        nlen = _u16(e, 64)
        obj = e[66]
        name = e[: max(0, nlen - 2)].decode("utf-16-le", "replace").rstrip("\x00")
        entries.append((name, obj, _u32(e, 116), struct.unpack_from("<Q", e, 120)[0]))
    root = next((e for e in entries if e[1] == 5), None)
    if not root:
        raise ValueError("OLE2 缺少 Root Entry")
    ministream = read_chain(_chain(fat, root[2]))[: root[3]]
    minifat: list[int] = []
    for sid in _chain(fat, first_minifat):
        s = sector(sid)
        minifat.extend(_u32(s, 4 * i) for i in range(ssize // 4))

    out: dict[str, bytes] = {}
    for name, obj, start, size in entries:
        if obj != 2 or not name or size == 0:
            continue
        if size < cutoff:
            ids = _chain(minifat, start)
            data = b"".join(ministream[i * msize:(i + 1) * msize] for i in ids)[:size]
        else:
            data = read_chain(_chain(fat, start))[:size]
        out[name] = data
    return out


def _biff_strings(stream: bytes, pos: int, ln: int) -> list[str]:
    """解析 SST 记录（含后续 CONTINUE 拼接）。返回一个尽量完整的字符串表。"""
    blob = bytearray(stream[pos: pos + ln])
    # 把紧随其后的 CONTINUE 记录内容拼上；字符串跨块时的 grbit 重置不做
    # 完美还原（Excel 会在块首重发一个压缩标志），遇此情况宁可少解几个。
    p = ln
    while pos + p + 4 <= len(stream):
        rid, rln = struct.unpack_from("<HH", stream, pos + p)
        if rid != 0x003C or pos + p + 4 + rln > len(stream):
            break
        blob += stream[pos + p + 4: pos + p + 4 + rln]
        p += 4 + rln
    if len(blob) < 8:
        return []
    total, unique = struct.unpack_from("<II", blob, 0)
    off, out = 8, []
    for _ in range(min(unique, 200_000)):
        if off + 3 > len(blob):
            break
        cch = _u16(blob, off)
        flags = blob[off + 2]
        off += 3
        rich = 0
        ext = 0
        if flags & 0x04:          # fRichSt
            rich = _u16(blob, off)
            off += 2
        if flags & 0x08:          # fExtSt
            ext = _u32(blob, off)
            off += 4
        # fHighByte = 1 表示 UTF-16LE；0 才是 8 位压缩串。写反会把整张
        # 共享字符串表解成 ``i\x00`` / ``ombre\t瀁愀爀爀漀焀甀椀愀`` 这种乱码。
        width = 2 if flags & 0x01 else 1
        need = cch * width
        if off + need > len(blob):
            break
        piece = bytes(blob[off: off + need])
        off += need
        out.append(piece.decode("utf-16-le" if width == 2 else "latin-1", "replace"))
    return out


def _rk_value(v: int) -> float | int:
    if v & 0x02:
        n = v >> 2
        if n & 0x20000000:
            n -= 0x40000000
        return n / 100 if v & 0x01 else n
    d = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return d / 100 if v & 0x01 else d


def _xls(stream: bytes) -> dict:
    pos = 0
    sst: list[str] = []
    cells: dict[tuple[int, int], str] = {}
    max_row = max_col = 0
    sheet_seen = 0
    while pos + 4 <= len(stream):
        rid, ln = struct.unpack_from("<HH", stream, pos)
        data = stream[pos + 4: pos + 4 + ln]
        pos += 4 + ln
        if rid == 0x0809 and len(data) >= 4:
            sub = _u16(data, 2)
            if sub == 0x0010:
                sheet_seen += 1
                if sheet_seen > 1:
                    break
            continue
        if rid == 0x00FC:
            # pos 已经推进到记录末尾；SST 的数据起点是 pos - ln（不是
            # 记录头位置 pos-4-ln），差 4 字节会把 total/unique 全读错。
            sst = _biff_strings(stream, pos - ln, ln)
            continue
        if rid == 0x000A:
            if sheet_seen:
                break
            continue
        if sheet_seen == 0 or len(data) < 6:
            continue
        row, col = struct.unpack_from("<HH", data, 0)
        if row > MAX_ROWS or col > MAX_COLS:
            continue
        val = ""
        if rid == 0x00FD and len(data) >= 10:  # LABELSST
            idx = _u32(data, 6)
            val = sst[idx] if idx < len(sst) else ""
        elif rid == 0x0203 and len(data) >= 14:  # NUMBER
            val = struct.unpack_from("<d", data, 6)[0]
        elif rid == 0x027E and len(data) >= 10:  # RK
            val = _rk_value(_u32(data, 6))
        elif rid == 0x00BD and len(data) >= 10:  # MULRK
            last = _u16(data, len(data) - 2)
            for k in range((len(data) - 6 - 2) // 6):
                xf, rk = struct.unpack_from("<HI", data, 4 + 6 * k)
                c = _u16(data, 2) + k
                if c <= last and c <= MAX_COLS:
                    cells[(row, c)] = _rk_value(rk)
                    max_row, max_col = max(max_row, row), max(max_col, c)
            continue
        elif rid == 0x0204 and len(data) >= 9:  # LABEL（BIFF8）
            cch = _u16(data, 6)
            flags = data[8]
            # BIFF8 的 grbit 只有 0x0D 这些位；BIFF5 没有这一字节，硬按
            # BIFF8 解会把后面整段记录流读成「一列有几千个乱码字符」。
            # 认不出就跳过这一格——少一个单元格远比造一个假字段好。
            if flags & ~0x0D or cch > 1024:
                continue
            width = 2 if flags & 0x01 else 1
            if len(data) < 9 + cch * width:
                continue
            piece = data[9: 9 + cch * width]
            val = piece.decode("utf-16-le" if width == 2 else "latin-1", "replace")
        elif rid == 0x0205 and len(data) >= 8:  # BOOLERR
            val = "TRUE" if data[7] else "FALSE"
        else:
            continue
        cells[(row, col)] = val
        max_row, max_col = max(max_row, row), max(max_col, col)
    if not cells:
        return _result(False, "xls", note="BIFF 记录流里没有可还原的单元格")
    rows = [[cells.get((r, c), "") for c in range(max_col + 1)]
            for r in range(max_row + 1)]
    records = _rows_to_records(rows)
    if not records:
        return _result(False, "xls", note="BIFF 单元格拼不出记录行")
    return _result(True, "xls", records,
                   f"OLE2/BIFF 工作表，{len(sst)} 个共享字符串；Excel 日期序列号未做换算",
                   sheet="第一个工作表")


# ── DBF（shapefile 属性表） ──────────────────────────────────────────────

def _dbf(raw: bytes) -> dict:
    if len(raw) < 33:
        return _result(False, "dbf", note="DBF 头太短")
    count = _u32(raw, 4)
    hlen = _u16(raw, 8)
    rlen = _u16(raw, 10)
    fields = []
    off = 32
    while off + 32 <= hlen and off + 32 <= len(raw) and raw[off] != 0x0D:
        name = raw[off: off + 11].split(b"\x00")[0].decode("latin-1", "replace").strip()
        ftype = chr(raw[off + 11])
        flen = raw[off + 16]
        fields.append((name or f"col_{len(fields) + 1}", ftype, flen))
        off += 32
    if not fields or not rlen:
        return _result(False, "dbf", note="DBF 字段描述符为空")
    records = []
    for i in range(min(count, MAX_ROWS)):
        start = hlen + i * rlen
        if start + rlen > len(raw):
            break
        row = raw[start: start + rlen]
        if row[:1] == b"*":
            continue
        rec, o = {}, 1
        for name, ftype, flen in fields[:MAX_COLS]:
            chunk = row[o: o + flen].strip()
            o += flen
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                text = chunk.decode("latin-1", "replace")
            if ftype in ("N", "F", "I", "O") and re.fullmatch(r"[-+]?\d+(\.\d+)?", text):
                rec[name] = float(text) if "." in text else int(text)
            else:
                rec[name] = text
        if any(str(v).strip() for v in rec.values()):
            records.append(rec)
    if not records:
        return _result(False, "dbf", note="DBF 里没有未删除的记录")
    return _result(True, "dbf", records,
                   f"shapefile 属性表 DBF，{len(fields)} 个字段；几何 .shp 不含在字段里")


# ── ZIP 内的表格 ─────────────────────────────────────────────────────────

def _zip(raw: bytes, zf: zipfile.ZipFile | None = None) -> dict:
    zf = zf or zipfile.ZipFile(io.BytesIO(raw))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    dbf = [n for n in names if n.lower().endswith(".dbf")]
    if dbf:
        try:
            r = _dbf(zf.read(dbf[0]))
            if r["ok"]:
                r["note"] += f"；来自压缩包 {dbf[0]}"
                r["container"] = "zip/dbf"
                r["archive_files"] = names[:20]
                return r
        except (KeyError, RuntimeError, OSError):
            pass
    csvs = [n for n in names if n.lower().endswith((".csv", ".tsv"))]
    jsons = [n for n in names if n.lower().endswith((".json", ".ndjson"))]
    for n in csvs[:1]:
        try:
            text = zf.read(n).decode("utf-8-sig", "replace")
            delim = "\t" if n.lower().endswith(".tsv") else ","
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
            recs = _rows_to_records(rows)
            if recs:
                return _result(True, "zip/csv", recs, f"压缩包内 {n}", archive_files=names[:20])
        except (KeyError, RuntimeError, OSError):
            pass
    for n in jsons[:1]:
        try:
            doc = json.loads(zf.read(n).decode("utf-8-sig", "replace"))
            if isinstance(doc, list) and doc and isinstance(doc[0], dict):
                return _result(True, "zip/json", doc[:MAX_ROWS], f"压缩包内 {n}",
                               archive_files=names[:20])
        except (KeyError, ValueError, OSError):
            pass
    return _result(False, "zip", note="压缩包里没有可还原的表格（" +
                   "、".join(names[:8]) + "）", archive_files=names[:20])


# ── PDF 文本层 ───────────────────────────────────────────────────────────

def _pdf_text_pypdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader  # 可选增强；没有就走纯标准库
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:12])
    except Exception:  # noqa: BLE001 - 损坏的 PDF 不该让整条实测崩掉
        return ""


def _pdf_text_builtin(raw: bytes) -> str:
    """不依赖第三方：解压 FlateDecode 内容流，取 (...)Tj / [...]TJ 文本算子。"""
    chunks: list[str] = []
    for m in re.finditer(rb"stream\r?\n", raw):
        start = m.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            continue
        blob = raw[start:end].rstrip(b"\r\n")
        try:
            data = zlib.decompress(blob)
        except zlib.error:
            data = blob
        if b"Tj" not in data and b"TJ" not in data and b"Td" not in data:
            continue
        # 按文本定位算子切行
        pieces = re.split(rb"(?:T\*|Td|TD|ET|\x27|\x22)", data)
        for piece in pieces:
            found = re.findall(rb"\(((?:\\.|[^\\)])*)\)", piece)
            if not found:
                continue
            line = "".join(_pdf_unescape(f) for f in found)
            if line.strip():
                chunks.append(line.strip())
        if len(chunks) > MAX_ROWS:
            break
    return "\n".join(chunks[:MAX_ROWS])


def _pdf_unescape(b: bytes) -> str:
    s = re.sub(rb"\\([nrtbf()\\])", lambda m: {
        b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
        b"(": b"(", b")": b")", b"\\": b"\\",
    }.get(m.group(1), m.group(1)), b)
    s = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 0xFF]), s)
    if s.startswith(b"\xfe\xff"):
        return s[2:].decode("utf-16-be", "replace")
    return s.decode("latin-1", "replace")


def _pdf(raw: bytes) -> dict:
    """PDF 是版面格式，不是数据表。

    试过按空白把文本层切成列，结果把「指标名 + 四个季度数值」这种排版报表
    切成了假字段名（``No of Indicators =`` / ``36``）。PDF 里没有单元格概念，
    分列全靠猜，猜出来的能力卡比没有卡更误导人。所以这里只报告**抽到了多少
    行文字**，不产出 records；需要字段就换成同数据的 CSV/XLSX 导出地址。
    """
    text = _pdf_text_pypdf(raw) or _pdf_text_builtin(raw)
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    preview = " / ".join(ln[:40] for ln in lines[:2])
    return _result(
        False, "pdf",
        note=(f"PDF 文本层抽到 {len(lines)} 行文字，但 PDF 没有单元格结构，"
              "按空白猜列会编造字段；请改用该数据的 CSV/XLSX 导出"
              + (f"（开头：{preview}）" if preview else "")),
        text_lines=len(lines), text_preview=lines[:5],
    )


# ── 入口 ────────────────────────────────────────────────────────────────

def _quality_ok(records: list[dict]) -> bool:
    """一张「实测出字段」的卡必须真的有内容。

    排版报表（合并单元格、大标题、合计行）常常只解出一两行、或者除第一列
    外全空。给它发一张「实测 1 行 × 10 字段」的卡，比标「不可还原」更坏。
    """
    if len(records) < 2 or len(records[0]) < 2:
        return False
    total = len(records) * len(records[0])
    filled = sum(1 for r in records for v in r.values() if str(v).strip())
    return filled >= max(3, int(total * 0.10))


def _guard(res: dict) -> dict:
    if res["ok"] and not _quality_ok(res["records"]):
        return _result(False, res["container"],
                       note=res["note"] + "；解出的记录行/非空值太少，判为排版报表而非记录表",
                       sheet=res.get("sheet", ""), sheet_count=res.get("sheet_count", 0))
    return res


def parse(raw: bytes, content_type: str = "") -> dict:
    """识别容器并解出记录。失败时 ``ok=False`` + 可复核的 note。"""
    if not raw:
        return _result(False, "empty", note="响应为空")
    if raw[:5] == b"%PDF-":
        return _pdf(raw)
    if raw[:4] == b"\xd0\xcf\x11\xe0":
        try:
            streams = _ole_streams(raw)
        except (ValueError, struct.error, IndexError) as exc:
            return _result(False, "xls", note=f"OLE2 容器解析失败：{str(exc)[:80]}")
        for key in ("Workbook", "Book"):
            if key in streams:
                return _guard(_xls(streams[key]))
        return _result(False, "xls", note="OLE2 里没有 Workbook 流（" +
                       "、".join(list(streams)[:6]) + "）")
    if raw[:4] == b"PK\x03\x04":
        return _guard(_xlsx(raw))
    if raw[:2] == b"\x1f\x8b":
        try:
            return parse(zlib.decompress(raw, 16 + zlib.MAX_WBITS), content_type)
        except zlib.error as exc:
            return _result(False, "gzip", note=f"GZIP 解压失败：{str(exc)[:80]}")
    if raw[:1] in (b"\x03", b"\x30") and len(raw) > 32 and raw[8:10] != b"\x00\x00":
        # dBase 头：版本字节 + 年月日 + 记录数 + 头长
        r = _dbf(raw)
        if r["ok"]:
            return _guard(r)
    if "pdf" in (content_type or "").lower():
        return _pdf(raw)
    return _result(False, "unknown", note="无法识别的二进制容器（前 8 字节 " +
                   raw[:8].hex() + "）")
