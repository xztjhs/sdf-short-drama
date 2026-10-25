"""把默认配置**合并**进运行时配置：只补缺失的键，绝不覆盖已有值。

解决的问题：
  rsync --ignore-existing 能保护用户改过的值，但新增的默认键也进不来；
  rsync --delete 能带进新键，却会冲掉用户改过的值。

  这里做「深合并」：
    - 目标缺的键 → 从默认补齐
    - 目标已有的键 → 保留目标值（用户改过的优先）
    - 默认里有、目标里被用户删掉的键 → 会重新出现（可接受）

用法（在 .253 上执行）：
    python3 /mnt/disks/nvme/docker/compose/sdf/scripts/merge_config.py \
        --defaults /tmp/sdf-defaults --target /mnt/disks/nvme/docker/compose/sdf/config \
        [--dry-run]
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import yaml


def deep_merge(default: dict, current: dict, path: str = "") -> tuple[dict, list[str]]:
    """返回 (合并结果, 新增键路径列表)。current 里已有的值一律保留。"""
    added: list[str] = []
    if not isinstance(current, dict):
        return default, [path or "<root>"]

    out = dict(current)
    for k, dv in default.items():
        key_path = f"{path}.{k}" if path else k
        if k not in out:
            out[k] = dv
            added.append(key_path)
        elif isinstance(dv, dict) and isinstance(out[k], dict):
            out[k], sub = deep_merge(dv, out[k], key_path)
            added.extend(sub)
        # 其余情况：保留 current 的值（用户改动优先）
    return out, added


def merge_file(default_path: Path, target_path: Path, dry: bool) -> list[str]:
    if not default_path.exists():
        return []
    if not target_path.exists():
        if not dry:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(default_path, target_path)
        return [f"{target_path.name}: 新建"]

    try:
        dflt = yaml.safe_load(default_path.read_text(encoding="utf-8")) or {}
        cur = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"  ⚠️ {target_path.name} 解析失败，跳过: {e}")
        return []

    if not isinstance(dflt, dict) or not isinstance(cur, dict):
        return []

    merged, added = deep_merge(dflt, cur)
    if not added:
        return []

    if not dry:
        # 备份后写入
        stamp = time.strftime("%Y%m%d_%H%M%S")
        bak = target_path.with_suffix(f".yaml.bak.{stamp}")
        shutil.copy2(target_path, bak)
        target_path.write_text(
            yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    return [f"{target_path.name}: {k}" for k in added]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--defaults", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ddir = Path(args.defaults)
    tdir = Path(args.target)
    tdir.mkdir(parents=True, exist_ok=True)

    total = 0
    for f in sorted(ddir.glob("*.yaml")):
        if ".bak." in f.name:
            continue
        added = merge_file(f, tdir / f.name, args.dry_run)
        if added:
            total += len(added)
            print(f"[{f.name}] 补 {len(added)} 项"
                  f"{'（dry-run）' if args.dry_run else ''}")
            for a in added[:12]:
                print(f"   + {a}")
            if len(added) > 12:
                print(f"   ... 还有 {len(added)-12} 项")

    print(f"\n共补齐 {total} 个配置项")
    if total and not args.dry_run:
        print("⚠️ 改完需重载配置：curl -X POST localhost:8080/api/admin/config/reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
