"""把旧项目目录迁移成 {YYYYMMDD-HHMM}_{slug} 格式。

旧目录名是项目 id（prj_xxx），新格式按创建时间 + 英文名。
英文名：有 slug 用 slug，没有就 LLM 翻译标题，失败则用 id 尾巴。

用法（容器内）：
    docker exec sdf-api python /app/migrate_dirs.py [--dry-run]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from pipeline import db
from pipeline import script as llm
from pipeline.config import cfg

DRY = "--dry-run" in sys.argv
OUT = Path(cfg.get("settings", "paths.output_dir", "/output") or "/output")


def gen_slug(prj: dict) -> str:
    title = prj.get("title") or ""
    try:
        s = llm.translate(title, kind="slug")
        if s and re.fullmatch(r"[a-z0-9-]+", s):
            return s
    except Exception as e:
        print(f"    翻译失败({e})，回退")
    # 回退 2：从标题里抠 ASCII（v02流程_1792579840 → v02-1792579840）
    ascii_part = re.sub(r"[^a-zA-Z0-9_]+", "", title).strip("_").lower()
    if ascii_part:
        return re.sub(r"_+", "-", ascii_part)[:40].strip("-")
    # 回退 3：id 尾巴
    return "p" + prj["id"].split("_")[-1][:8]


def main():
    db.init_db()
    projects = db.list_projects(include_archived=True)
    print(f"共 {len(projects)} 个项目\n")
    moved = skipped = failed = 0

    for p in projects:
        pid = p["id"]
        title = p.get("title") or ""
        cur_out = p.get("out_dir")
        slug = p.get("slug")

        # 判断是否需要迁移：目录名还是 prj_ 开头，或没有 out_dir
        old_name = cur_out or pid
        needs = (not cur_out) or old_name.startswith("prj_")

        src = OUT / old_name
        print(f"[{title}]")
        print(f"  当前目录: {old_name}  存在={src.exists()}")

        if not needs:
            print("  ✓ 已是新格式，跳过\n")
            skipped += 1
            continue

        if not slug:
            slug = gen_slug(p)
            print(f"  生成英文名: {slug}")

        created = p.get("created_at") or p.get("updated_at")
        new_name = db.make_out_dir_name(slug, created)
        dst = OUT / new_name
        print(f"  目标目录: {new_name}")

        if DRY:
            print("  [dry-run] 不执行\n")
            continue

        try:
            if src.exists():
                if dst.exists():
                    print(f"  ✗ 目标已存在，跳过")
                    failed += 1
                    print()
                    continue
                src.rename(dst)
                print(f"  ✓ 已重命名")
            else:
                print(f"  · 无产物目录，仅更新数据库")

            db.update_project(pid, slug=slug, out_dir=new_name)
            moved += 1
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            failed += 1
        print()

    print("=" * 50)
    print(f"迁移 {moved} / 跳过 {skipped} / 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
