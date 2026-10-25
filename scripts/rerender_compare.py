"""重渲指定镜头并输出 A+B 方案对比信息。

用途：用同一集、同一批镜头，验证 v0.6（场次卡 + 说话人 + 锚点注入 + Ref2VA）
相比旧链路（v0.5 及以前）的实际差异。

做法：
  1. 重渲前把旧产物备份到 _before_v06/（脚本外已做，这里只记录路径）
  2. 逐镜串行重渲，记录：引擎、分辨率、耗时、是否有参考图、说话人、场次
  3. 输出对比 JSON，便于人工/脚本对照

用法（api 容器内）：
    docker exec sdf-api python /app/rerender_compare.py --episode ep_xxx --seqs 5,6,7
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from pipeline import comfy, db, worker  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", required=True)
    ap.add_argument("--seqs", default="", help="逗号分隔的镜头号；留空=全集")
    ap.add_argument("--tier", default="normal", help="normal|quality|draft")
    ap.add_argument("--out", default="/logs/rerender_compare.json")
    a = ap.parse_args()

    ep = db.get_episode(a.episode)
    if not ep:
        print(f"❌ 剧集不存在: {a.episode}")
        return 1
    shots = db.list_shots(a.episode)
    if a.seqs:
        wanted = {int(x) for x in a.seqs.split(",") if x.strip()}
        shots = [s for s in shots if int(s.get("seq") or 0) in wanted]
    if not shots:
        print("❌ 没有匹配的镜头")
        return 1

    scenes = {int(sc["id"]): sc for sc in db.get_episode_scenes(a.episode)}
    print(f"重渲 {len(shots)} 镜 · tier={a.tier} · 场次卡 {len(scenes)} 张")

    # 1) 备份旧产物
    outdir = worker._out_dir(ep["project_id"], ep["seq"])
    bak = outdir / "_before_v06"
    bak.mkdir(parents=True, exist_ok=True)
    before = {}
    for s in shots:
        old = s.get("output_path")
        if old and Path(old).exists():
            dst = bak / Path(old).name
            if not dst.exists():
                shutil.copy2(old, dst)
            before[s["seq"]] = {"path": str(dst),
                                "size": dst.stat().st_size}
    print(f"旧产物已备份 {len(before)} 个 → {bak}")

    results = []
    for i, s in enumerate(shots, 1):
        seq = s["seq"]
        refs = worker.character_ref_images(ep["project_id"], s)
        sc = scenes.get(int(s.get("scene_id") or 0)) or {}
        anchor = worker.anchor_text(ep["project_id"], s)
        print(f"\n[{i}/{len(shots)}] 镜{seq} · 场{s.get('scene_id')}"
              f"（{sc.get('name') or '-'}） · 参考图 {len(refs)} 张"
              f" · 说话人 {s.get('speaker') or '-'}")
        print(f"  锚点: {(anchor or '(无)')[:90]}")
        print(f"  台词: {(s.get('dialogue') or '(无)')[:40]}")

        # 重置状态以便重渲
        db.update_shot(s["id"], status="pending")
        t0 = time.time()
        s2 = db.get_shot(s["id"])
        try:
            r = worker.render_shot(s2, project_id=ep["project_id"],
                                   episode_seq=ep["seq"], tier=a.tier)
        except Exception as e:
            r = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        elapsed = round(time.time() - t0, 1)

        rec = {
            "seq": seq,
            "scene_id": s.get("scene_id"),
            "scene_name": sc.get("name"),
            "speaker": s.get("speaker"),
            "dialogue": s.get("dialogue"),
            "ref_images": len(refs),
            "anchor_used": bool(anchor),
            "engine": r.get("engine"),
            "resolution": r.get("resolution"),
            "status": r.get("status"),
            "elapsed_s": elapsed,
            "output": r.get("output_path"),
            "error": r.get("error"),
            "before": before.get(seq),
        }
        results.append(rec)
        print(f"  → {r.get('status')} {elapsed}s {r.get('resolution') or ''}"
              f" {r.get('error') or ''}")

    ok = sum(1 for r in results if r["status"] == "done")
    print(f"\n结果：{ok}/{len(results)} 成功")
    Path(a.out).write_text(json.dumps({"episode": a.episode,
                                       "tier": a.tier,
                                       "scenes": list(scenes.values()),
                                       "results": results},
                                      ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"明细 {a.out}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
