"""Pinned snapshot -> field-level cells.

A cell is (dataset, version, row_key, column, value) plus the column's
declared definition / unit / type. A hit is therefore "which value, in
which row, under which definition" instead of "some page on the web".
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

from .contract import SnapshotMismatch


@dataclass(frozen=True)
class Cell:
    dataset_id: str
    version: str
    row_key: str
    column: str
    value: str
    field_desc: str
    unit: str
    field_type: str
    source_sha: str

    @property
    def citation(self) -> str:
        return f"{self.dataset_id}@{self.version}#{self.row_key}.{self.column}"

    def as_tuple(self) -> tuple:
        return (
            self.citation,
            self.dataset_id,
            self.version,
            self.row_key,
            self.column,
            self.value,
            self.field_desc,
            self.unit,
            self.field_type,
            self.source_sha,
        )


def flatten(record: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in record.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict):
            out.update(flatten(val, name))
        elif isinstance(val, (list, tuple)):
            out[name] = json.dumps(val, ensure_ascii=False, separators=(",", ":"))
        elif val is None:
            out[name] = ""
        else:
            out[name] = str(val)
    return out


def _resolve(doc, path: str | None):
    if not path:
        return doc
    for part in str(path).split("."):
        if isinstance(doc, list) and part.isdigit():
            doc = doc[int(part)]
        elif isinstance(doc, dict):
            doc = doc[part]
        else:
            raise ValueError(f"cannot resolve record_path {path!r}")
    return doc


def _records(src: dict, raw: bytes) -> list[dict]:
    ing = src["ingest"]
    fmt = ing["format"]
    text = raw.decode("utf-8-sig", errors="replace")
    if fmt == "lines":
        return [
            {"paragraph_no": str(i), "text": line.strip()}
            for i, line in enumerate(text.splitlines(), 1)
            if line.strip()
        ]
    if fmt == "json":
        records = _resolve(json.loads(text), ing.get("record_path"))
        return [r for r in records if isinstance(r, dict)]
    if fmt == "csv":
        return list(csv.DictReader(io.StringIO(text)))
    raise ValueError(f"unsupported format {fmt}")


def iter_cells(src: dict) -> list[Cell]:
    folder = Path(src["_dir"])
    raw = (folder / src["raw_file"]).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != src["sha256"]:
        raise SnapshotMismatch(
            f"{src['dataset_id']}: snapshot changed "
            f"(declared {src['sha256'][:12]}..., actual {actual[:12]}...)"
        )
    meta = {f["name"]: f for f in src["fields"]}
    cells: list[Cell] = []
    for index, record in enumerate(_records(src, raw), 1):
        flat = flatten(record)
        parts = [flat.get(name, "") for name in src["ingest"]["row_key"]]
        row_key = ":".join(parts) if any(parts) else f"row:{index}"
        for column, value in flat.items():
            desc = meta.get(column, {})
            if not value and column not in meta:
                continue
            cells.append(
                Cell(
                    dataset_id=src["dataset_id"],
                    version=str(src["version"]),
                    row_key=row_key,
                    column=column,
                    value=value,
                    field_desc=desc.get("description", ""),
                    unit=desc.get("unit", ""),
                    field_type=desc.get("type", "string"),
                    source_sha=actual,
                )
            )
    return cells


def summarize(src: dict, cells: list[Cell]) -> dict:
    """Re-measure what the publisher declared. This is the honesty check."""
    rows = {c.row_key for c in cells}
    rates = {}
    for field in src["fields"]:
        name = field["name"]
        present = sum(1 for c in cells if c.column == name and c.value.strip())
        rates[name] = round(present / len(rows), 4) if rows else 0.0
    return {
        "actual_records": len(rows),
        "actual_cells": len(cells),
        "fill_rates": rates,
    }
