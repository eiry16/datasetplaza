"""从 `listings.json` 重建薄索引（`listings/shelf.jsonl`）、完整档案
（`listings/archive/*.jsonl`），以及控制台目录行缓存
（`listings/catalog_rows.pkl`）。三者都是派生数据。

  python scripts/build_shelf.py              # 全部重建（含目录行）
  python scripts/build_shelf.py --no-rows    # 只重建薄索引 + 档案
  python scripts/build_shelf.py --stats      # 只看现状（不重建）

货架每次被上下架脚本写过之后都要跑一次；忘了也不会静默出错——`load_shelf()`
发现档案落后于 `listings.json` 会自己重建。

0.40：目录行也是构建产物了。以前只有后台进程跑起来才第一次生成它，
冷启动要空等 49.5s；现在把这段一次性成本挪到构建期，控制台启动只需
反序列化一次。签名与 AdminService 共用 `bdsp.rows_cache`，两边不会算成
两套版本号。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bdsp import listings as L, rows_cache as rowscache, store  # noqa: E402


def mb(n: int) -> str:
    return "%.1f MB" % (n / 1048576)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--path", default="")
    ap.add_argument("--no-rows", action="store_true",
                    help="跳过控制台目录行缓存预生成")
    args = ap.parse_args()

    if args.stats:
        s = store.stats()
        if not s.get("built"):
            print("未构建")
            return 0
        print("条目 %d，薄索引 %s，档案 %d 片 / %s，新鲜=%s"
              % (s["count"], mb(s["shelf_bytes"]), len(s["shards"]),
                 mb(sum(x["bytes"] for x in s["shards"].values())), s.get("fresh")))
        for name, info in sorted(s["shards"].items(), key=lambda kv: -kv[1]["rows"]):
            print("  %-28s %6d 行  %s" % (name, info["rows"], mb(info["bytes"])))
        return 0

    t0 = time.time()
    items = L.load(args.path or None)
    t_load = time.time() - t0
    if not items:
        print("货架为空，不构建")
        return 1
    t1 = time.time()
    meta = store.build(items, source=args.path or None)
    t_build = time.time() - t1

    # 读回来验一遍：薄索引条数与条目一致、随机抽 3 条能按 id 取到完整档案
    t2 = time.time()
    shelf = store.load_shelf(auto_build=False)
    t_shelf = time.time() - t2
    t3 = time.time()
    sample = [shelf[i]["dataset_id"] for i in (0, len(shelf) // 2, len(shelf) - 1)]
    full = store.hydrate(sample)
    t_hyd = time.time() - t3

    print("条目 %d · 读全量 %.1fs · 构建 %.1fs" % (len(items), t_load, t_build))
    print("薄索引 %s（%d 条，读 %.2fs）· 档案 %s / %d 片"
          % (mb(meta["shelf_bytes"]), len(shelf), t_shelf,
             mb(sum(x["bytes"] for x in meta["shards"].values())), len(meta["shards"])))
    print("抽样 hydrate %d/%d 条，%.3fs" % (len(full), len(sample), t_hyd))
    ok = len(shelf) == len(items) and len(full) == len(sample)
    if not args.no_rows:
        t4 = time.time()
        info = rowscache.prebuild(ROOT)
        print("目录行缓存 %d 条：重排 %.1fs + 落盘 %.1fs → %s（%s）"
              % (info["rows"], info["rows_seconds"], info["write_seconds"],
                 "成功" if info["ok"] else "写入失败（不影响货架）",
                 Path(info["cache"]).name))
        ok = ok and info["ok"] and info["rows"] == len(items)
        print("目录行预生成共 %.1fs" % (time.time() - t4))
    print("自检：%s" % ("通过" if ok else "失败"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
