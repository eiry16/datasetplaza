"""Catalogue rows are derived data. Build them once, offline, and reuse them.

0.39.2 added a per-process + on-disk cache for the console's directory rows.
It fixed steady-state requests but left two cold paths paying the full price:

* a fresh process with no `catalog_rows.pkl` waited **49.5s** while 35k
  listings were re-read (259 MB) and reshaped into rows;
* every publish / publish-batch rewrites `listings.json`, its mtime changes,
  the signature misses, and the next request pays the same 49.5s again —
  even though only a handful of `published` flags changed.

This module makes the cache a first-class build artifact and fixes both:

1. ``scripts/build_shelf.py`` calls :func:`prebuild` after rebuilding the
   shelf, so a normal deployment always has a valid cache before the console
   starts. Cold start becomes a single unpickle.
2. ``AdminService`` patches only the rows whose ids changed on publish and
   rewrites the cache under the new signature. No full recompute.

Signature discipline is the whole game: a cached row is only reusable if the
shelf, the index DB, the health snapshot, the source manifests **and the code
that shapes a row** are unchanged. ``RECORD_VERSION`` is the human-owned part
of that contract — bump it whenever ``listings.record()`` grows/renames a key.
The rule versions are included automatically, so a card rebuild invalidates
the rows without anyone remembering to bump a number.
"""
from __future__ import annotations

import json
import os
import pickle
from pathlib import Path

VERSION = "rows-2"
RECORD_VERSION = "record-0.40"     # bump when listings.record() shape changes
CACHE_NAME = "catalog_rows.pkl"


def _fingerprint() -> tuple:
    from . import capability, map as mapmod, source_terms, store
    return (RECORD_VERSION, capability.RULE_VERSION, capability.AXIS_VERSION,
            mapmod.MAP_VERSION, source_terms.VERSION, store.VERSION)


def _stat(p: Path) -> tuple:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def signature(sources: Path, listings_path: Path, db: Path,
              health_path: Path) -> tuple:
    """Everything a directory row depends on, flattened for JSON round-trips.

    mtime_ns + size rather than a content hash: hashing a 259 MB shelf costs
    more than the read it protects, and ``save()`` replaces the file
    atomically. Source manifests are enumerated explicitly because editing a
    file's *content* does not always change the parent directory's mtime.
    """
    sig: list = [VERSION, *_fingerprint()]
    for p in (listings_path, db, health_path, sources):
        sig.extend(_stat(Path(p)))
    manifests = []
    try:
        for f in sorted(Path(sources).glob("*/manifest.json")):
            manifests.extend((f.parent.name, *_stat(f)))
    except OSError:
        pass
    sig.extend(manifests)
    return tuple(sig)


def _blob(path: Path) -> dict | None:
    try:
        obj = pickle.loads(Path(path).read_bytes())
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError,
            ImportError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def read(path: Path, sig: tuple) -> list[dict] | None:
    """Valid cache for this exact signature, else None."""
    obj = _blob(path)
    if obj is None or obj.get("sig") != sig:
        return None
    rows = obj.get("rows")
    return rows if isinstance(rows, list) else None


def read_any(path: Path) -> tuple[tuple | None, list[dict] | None]:
    """Rows plus the signature they were built under, ignoring staleness.

    Used by incremental patching: after a publish the *shelf* signature has
    changed, but the rows are still valid for every unaffected id. The caller
    must check that the stored version matches before trusting the shape.
    """
    obj = _blob(path)
    if obj is None:
        return None, None
    sig = obj.get("sig")
    rows = obj.get("rows")
    if not isinstance(rows, list) or not isinstance(sig, (list, tuple)):
        return None, None
    return tuple(sig), rows


def write(path: Path, meta_path: Path, sig: tuple, rows: list[dict]) -> bool:
    """Atomic write plus a tiny sidecar (count only). Failure is never fatal."""
    tmp = Path(path).with_suffix(".pkl.tmp")
    try:
        tmp.write_bytes(pickle.dumps({"sig": sig, "rows": rows},
                                     protocol=pickle.HIGHEST_PROTOCOL))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        Path(meta_path).write_text(
            json.dumps({"sig": list(sig), "count": len(rows)}), encoding="utf-8")
    except OSError:
        pass
    return True


def build_rows(sources: Path, platform, health: dict, listings_path: Path) -> list[dict]:
    from . import catalog
    return catalog.records(sources, platform, health, listings_path)


def prebuild(root: Path, sources: Path | None = None,
             listings_path: Path | None = None, db: Path | None = None,
             runtime: Path | None = None) -> dict:
    """One-shot offline build for ``scripts/build_shelf.py``.

    Constructs the same Platform/health inputs the console uses, so the
    signature written here is the one ``AdminService`` will compute at start.
    Returns timing/count details for the build log; never raises on a missing
    index DB (``catalog.records`` already degrades to catalogue-only rows).
    """
    import time
    from .api import Platform

    root = Path(root)
    src = Path(sources or root / "sources")
    listings = Path(listings_path or root / "listings" / "listings.json")
    dbp = Path(db or root / "index.sqlite3")
    rt = Path(runtime or root / "runtime")
    hp = rt / "health.json"
    try:
        health = json.loads(hp.read_text(encoding="utf-8")) if hp.is_file() else {}
    except (OSError, json.JSONDecodeError):
        health = {}
    t0 = time.time()
    platform = Platform(sources=src, db=dbp, runtime=rt)
    try:
        rows = build_rows(src, platform, health, listings)
    finally:
        platform.close()
    t_rows = time.time() - t0
    sig = signature(src, listings, dbp, hp)
    cache = listings.parent / CACHE_NAME
    meta = cache.with_suffix(".meta.json")
    t0 = time.time()
    ok = write(cache, meta, sig, rows)
    t_write = time.time() - t0
    return {"ok": ok, "rows": len(rows), "rows_seconds": t_rows,
            "write_seconds": t_write, "cache": str(cache),
            "signature_head": list(sig[:6])}
