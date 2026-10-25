"""v0.5 → v0.6 配置迁移（方案 A+B）。

v0.6 变更：
  1. prompts.storyboard_v2：新增场次（scene_id/scenes）、说话人（speaker）、
     人物锚点逐字复用、人数拆分、镜头数收敛到 20-24
  2. prompts.storyboard_rules.h3：同一场次连续对白合并
  3. settings.render：ref2va_enabled / ref2va_image_size / chain_same_scene_only

只做「明确、可回滚」的替换：
  - storyboard_v2 / storyboard_rules 整块替换（替换前若已含 scene_id 则跳过）
  - settings 只补缺失键
  - 写前 YAML 自检，写前备份

用法：
    docker exec sdf-api python /app/migrate_v06.py --config /config --dry-run
    docker exec sdf-api python /app/migrate_v06.py --config /config
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
import time

import yaml

DEFAULTS = (
    pathlib.Path("/opt/sdf-default-config"),
    pathlib.Path(__file__).resolve().parent.parent / "config_defaults",
    pathlib.Path(__file__).resolve().parent.parent / "config",
)


def _defaults() -> pathlib.Path | None:
    for c in DEFAULTS:
        if (c / "prompts.yaml").exists():
            return c
    return None


def _backup(p: pathlib.Path) -> str:
    bak = p.with_name(f"{p.name}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(p, bak)
    return bak.name


def _block(text: str, key: str) -> tuple[int, int]:
    """返回 YAML 文本里 `  key:` 整块的 [start, end) 偏移。"""
    m = re.search(rf"^  {re.escape(key)}:\s*$", text, re.M)
    if not m:
        return -1, -1
    rest = text[m.end():]
    nxt = re.search(r"^  [A-Za-z_][\w-]*:\s*$", rest, re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return m.start(), end


def patch_prompts(path: pathlib.Path, dflt: pathlib.Path, dry: bool) -> list[str]:
    notes: list[str] = []
    text = path.read_text(encoding="utf-8")
    src = (dflt / "prompts.yaml").read_text(encoding="utf-8")

    # v0.6：补 retrofit_v2（旧分镜补场次/说话人）
    if "retrofit_v2" not in text:
        d0, d1 = _block(src, "retrofit_v2")
        if d0 >= 0:
            text = text[:d0] if False else text
            # 插到 localize_v2 之前
            m = re.search(r"^  localize_v2:\s*$", text, re.M)
            at = m.start() if m else len(text)
            text = text[:at] + src[d0:d1] + "\n" + text[at:]
            notes.append("+ prompts.retrofit_v2（旧分镜补场次/说话人模板）")

    # storyboard_v2：老模板没有 scene_id → 整块替换为新版
    s0, s1 = _block(text, "storyboard_v2")
    if s0 < 0:
        notes.append("! 线上没有 storyboard_v2，跳过")
    else:
        cur = text[s0:s1]
        if "scene_id" in cur and "speaker" in cur:
            notes.append("✓ storyboard_v2 已含场次/说话人")
        else:
            d0, d1 = _block(src, "storyboard_v2")
            if d0 < 0:
                notes.append("! 默认模板里没有 storyboard_v2")
            else:
                text = text[:s0] + src[d0:d1] + text[s1:]
                notes.append("~ storyboard_v2 升级：场次卡 + 说话人 + 锚点复用 + 人数拆分 + 镜头数 20-24")

    # storyboard_rules.h3：补「同场次对白合并」
    r0, r1 = _block(text, "storyboard_rules")
    if r0 >= 0:
        cur_rules = text[r0:r1]
        if "合并到同一个镜头" not in cur_rules:
            d0, d1 = _block(src, "storyboard_rules")
            if d0 >= 0:
                text = text[:r0] + src[d0:d1] + text[r1:]
                notes.append("~ storyboard_rules.h3 补「同场次连续对白合并」")

    if any(n.startswith(("~", "+")) for n in notes):
        try:
            yaml.safe_load(text)
        except Exception as e:
            notes.append(f"!! YAML 校验失败，跳过写入: {e}")
            return notes
        if not dry:
            notes.append(f"备份 {_backup(path)}")
            path.write_text(text, encoding="utf-8")
    return notes


def patch_settings(path: pathlib.Path, dry: bool) -> list[str]:
    notes: list[str] = []
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^render:\s*$([\s\S]*?)(?=^\S|\Z)", text, re.M)
    if not m:
        return ["! settings.yaml 没有 render 段"]
    block = m.group(1)
    new = block
    add = {
        "ref2va_enabled": "true",
        "ref2va_image_size": "match",
        "chain_same_scene_only": "true",
    }
    for k, v in add.items():
        if not re.search(rf"^\s+{k}:\s*\S", new, re.M):
            new = new.rstrip("\n") + f"\n  # v0.6：{k}\n  {k}: {v}\n"
            notes.append(f"+ render.{k}: {v}")
    if new != block:
        try:
            yaml.safe_load(text[:m.start(1)] + new + text[m.end(1):])
        except Exception as e:
            notes.append(f"!! YAML 校验失败: {e}")
            return notes
        if not dry:
            notes.append(f"备份 {_backup(path)}")
            path.write_text(text[:m.start(1)] + new + text[m.end(1):],
                            encoding="utf-8")
    return notes or ["✓ settings.render 已含 v0.6 键"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/config")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = pathlib.Path(a.config)
    if not root.exists():
        print(f"❌ 配置目录不存在: {root}")
        return 1
    dflt = _defaults()
    if dflt is None:
        print("❌ 找不到默认配置目录")
        return 1

    total = 0
    for name, fn in (("prompts.yaml", lambda p, d: patch_prompts(p, dflt, d)),
                     ("settings.yaml", lambda p, d: patch_settings(p, d))):
        p = root / name
        if not p.exists():
            print(f"⚠️ {name} 不存在，跳过")
            continue
        try:
            notes = fn(p, a.dry_run)
        except Exception as e:
            print(f"❌ {name}: {type(e).__name__}: {e}")
            continue
        print(f"→ {name}")
        for n in notes:
            print("   " + n)
            if n[0] in "+~-":
                total += 1
    print(("（dry-run，未写入）" if a.dry_run else "") + f" 合计 {total} 处变更")
    return 0


if __name__ == "__main__":
    sys.exit(main())
