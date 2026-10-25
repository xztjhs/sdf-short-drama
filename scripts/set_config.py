"""显式设置指定配置值（用于把「旧默认」改成「新默认」）。

与 merge_config.py 的区别：
  merge  → 只补缺失键，已有值一律保留（保护用户改动）
  本脚本 → 明确覆盖指定键（用于版本升级时主动调整默认值）

用法：
  docker exec sdf-api python /app/set_config.py --file routing.yaml \
      --set resolutions.h3.normal=720x1280 --set resolutions.wan22.normal=720x1280
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import yaml


def set_path(data: dict, dotted: str, value: str):
    """按 a.b.c 路径设值，自动推断类型（数字/布尔/字符串）。"""
    parts = dotted.split(".")
    node = data
    for p in parts[:-1]:
        if p not in node or not isinstance(node[p], dict):
            node[p] = {}
        node = node[p]

    # 类型推断
    v = value
    if v.lower() in ("true", "false"):
        v = v.lower() == "true"
    else:
        try:
            v = int(v)
        except ValueError:
            try:
                v = float(v)
            except ValueError:
                pass
    node[parts[-1]] = v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="config 下的文件名，如 routing.yaml")
    ap.add_argument("--set", action="append", default=[],
                    metavar="KEY=VALUE", help="可重复")
    ap.add_argument("--target", default="/config")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    p = Path(args.target) / args.file
    if not p.exists():
        print(f"❌ 文件不存在: {p}")
        return 1
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    for kv in args.set:
        if "=" not in kv:
            print(f"❌ 格式错误: {kv}")
            return 1
        k, v = kv.split("=", 1)
        old = None
        node = data
        for part in k.split("."):
            if isinstance(node, dict) and part in node:
                old = node[part]
                node = node[part]
            else:
                old = None
                break
        print(f"  {k}: {old!r} → {v!r}")
        if not args.dry_run:
            set_path(data, k, v)

    if args.dry_run:
        print("\n(dry-run，未写入)")
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = p.with_suffix(f".yaml.bak.{stamp}")
    shutil.copy2(p, bak)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    print(f"\n✅ 已写入 {p.name}，备份 {bak.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
