"""Ref2VA 工作流单镜验证（v0.6 方案 B 的上 GPU 实测）。

用法（api 容器内）：
    docker exec sdf-api python /app/test_ref2va.py --character 赤鸦
    docker exec sdf-api python /app/test_ref2va.py --all       # 每个有图的角色各跑一镜

只跑草稿档（544x960 / 3s / 6 步，约 4-5 分钟），目的是验证：
  1. Autogrow 输入（ref_image_0..N）在 API 格式里能被 ComfyUI 接受
  2. Ref2VA 节点 + BasicGuider + VAEDecodeAudio 图能跑完
  3. 产物有画面 + 音轨
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from pipeline import comfy, db, workflow  # noqa: E402
from pipeline.config import cfg  # noqa: E402


def pick_characters(name: str = "", limit: int = 0) -> list[dict]:
    chars = []
    for prj in db.list_projects(include_archived=True):
        for c in db.list_characters(prj["id"]):
            if not (c.get("ref_image") or "").strip():
                continue
            if name and c["name"] != name:
                continue
            chars.append(c)
    if limit:
        chars = chars[:limit]
    return chars


def resolve(path_or_rel: str) -> str | None:
    p = Path(path_or_rel)
    if not p.is_absolute():
        p = Path("/input") / path_or_rel.lstrip("/")
    return str(p) if p.exists() else None


def run_one(c: dict, *, steps: int = 6, resolution: str = "544x960",
            duration: float = 3.0) -> dict:
    ref = resolve(c.get("ref_image") or "")
    if not ref:
        return {"character": c["name"], "ok": False, "error": "参考图文件不存在"}
    # ComfyUI 的 LoadImage 只认自己 input 目录下的文件名，
    # 传绝对路径会报 Invalid file path（生产代码走 upload_image，同理）
    try:
        ref = comfy.upload_image(ref)
    except Exception as e:
        return {"character": c["name"], "ok": False,
                "error": f"参考图上传失败: {type(e).__name__}: {e}"[:300]}

    prompt = (
        f"人物定妆镜头：{c['name']}，"
        f"{(c.get('appearance_zh') or c.get('appearance') or '').strip()}。"
        f"人物长相严格参考 <Picture 1>。正面半身，面部清晰，光线均匀打亮五官，"
        f"背景简洁虚化，镜头缓慢推近，人物基本不动。"
    )
    wf = workflow.build_h3_ref2va(
        prompt=prompt, resolution=resolution, duration=duration,
        steps=steps, seed=12345, ref_images=[ref], ref_image_size="match",
        filename_prefix=f"ref2va_test_{c['id']}")
    problems = workflow.validate(wf)
    print(f"  图节点数 {len(wf)}，自检: {problems or '通过'}")
    if problems:
        return {"character": c["name"], "ok": False,
                "error": "图自检失败: " + "; ".join(problems)}

    t0 = time.time()
    try:
        pid = comfy.submit(wf)
    except Exception as e:
        return {"character": c["name"], "ok": False,
                "error": f"提交被拒: {type(e).__name__}: {e}"[:400]}
    print(f"  已提交 prompt_id={pid}，等待…")
    try:
        h = comfy.wait(pid, timeout=1800, poll=3)
    except Exception as e:
        return {"character": c["name"], "ok": False,
                "error": f"等待失败: {type(e).__name__}: {e}"[:300]}
    elapsed = round(time.time() - t0, 1)

    fn = None
    for kind in ("videos", "gifs", "images"):
        for item in comfy.outputs_of(h, kind):
            if item.get("filename"):
                sub = item.get("subfolder") or ""
                fn = f"{sub}/{item['filename']}" if sub else item["filename"]
                break
        if fn:
            break
    if not fn:
        return {"character": c["name"], "ok": False,
                "error": "无输出", "elapsed_s": elapsed}

    # 检查产物有没有音轨
    out_root = Path(cfg.get("settings", "paths.output_dir", "/output") or "/output")
    src = out_root / fn
    info = {}
    if src.exists():
        from pipeline import assemble
        try:
            info = assemble.info(str(src))
        except Exception as e:
            info = {"probe_error": str(e)[:150]}
    return {"character": c["name"], "ok": True, "elapsed_s": elapsed,
            "file": fn, "info": info}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--character", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--steps", type=int, default=6)
    ap.add_argument("--resolution", default="544x960")
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--out", default="/logs/ref2va_test.json")
    a = ap.parse_args()

    if not comfy.alive():
        print("❌ ComfyUI 未运行")
        return 1

    chars = pick_characters(a.character, 0 if a.all else 1)
    if not chars:
        print("❌ 没有带参考图的角色")
        return 1
    print(f"测试 {len(chars)} 个角色，草稿档 {a.resolution} / {a.duration}s / {a.steps} 步")

    results = []
    for i, c in enumerate(chars, 1):
        print(f"[{i}/{len(chars)}] {c['name']}")
        r = run_one(c, steps=a.steps, resolution=a.resolution,
                    duration=a.duration)
        print("  " + json.dumps(r, ensure_ascii=False))
        results.append(r)
        comfy.free_vram()
        time.sleep(2)

    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n结果：{ok}/{len(results)} 成功")
    Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"明细 {a.out}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
