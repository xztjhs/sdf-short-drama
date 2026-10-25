"""人脸保真对照实验（示例人物A项目问题定位）。

目的：区分「Ref2VA 本身锁不住脸」和「文本提示词把脸带偏了」。

三个变体，同一张参考照片、同一镜头内容：
  A  当前生产写法：长锚点（含"中年/圆脸/黑框眼镜"）+ turbo8 LoRA + 8 步
  B  中性短提示（去掉年龄/脸型等形容词，只留动作场景）+ turbo8 LoRA + 8 步
  C  中性短提示 + 不用 LoRA + 20 步（更高的采样保真度）

跑完抽帧，落 /tmp/face_test/*.png 供人工比对。

用法（api 容器内）：
    docker exec sdf-api python /app/test_face_fidelity.py --ref /input/uploads/ref_xxx.jpg
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from pipeline import comfy, workflow  # noqa: E402
from pipeline.config import cfg  # noqa: E402

# 生产里的长锚点（取自示例人物A项目实际数据）
ANCHOR = ("示例人物A：中年戴黑框眼镜的中国男士，黑色短发，圆脸，神态温和，"
          "浅蓝条纹衬衫+深灰色冲锋衣，写实真人")

SCENE = ("原木装饰的酒店走廊中段，两侧竖向木纹护墙板与黄铜壁灯，"
         "顶部暖黄色筒灯从画面上方偏左斜射，浅色木地板")

VARIANTS = [
    {"id": "A_current", "desc": "长锚点 + LoRA + 8步（当前生产写法）",
     "prompt": f"【人物定妆（必须严格保持）】{ANCHOR}。"
               f"【本镜】本镜景别：中景，镜头运动：固定："
               f"他正面站在{SCENE}，嘴角上扬微笑，身体微微后仰。4K写实真人，电影级画质。",
     "turbo": True, "steps": 8},
    {"id": "B_neutral", "desc": "中性短提示 + LoRA + 8步",
     "prompt": f"人物长相严格参考 <Picture 1>。"
               f"他正面站在{SCENE}，嘴角上扬微笑，身体微微后仰，中景固定镜头。"
               f"4K写实真人，电影级画质。",
     "turbo": True, "steps": 8},
    {"id": "C_nolora", "desc": "中性短提示 + 不用LoRA + 20步",
     "prompt": f"人物长相严格参考 <Picture 1>。"
               f"他正面站在{SCENE}，嘴角上扬微笑，身体微微后仰，中景固定镜头。"
               f"4K写实真人，电影级画质。",
     "turbo": False, "steps": 20},
]


def run(v: dict, ref_name: str, resolution: str, duration: float) -> dict:
    wf = workflow.build_h3_ref2va(
        prompt=v["prompt"], resolution=resolution, duration=duration,
        steps=v["steps"], seed=20261023, ref_images=[ref_name],
        ref_image_size="match", turbo=v["turbo"],
        filename_prefix=f"facetest_{v['id']}")
    probs = workflow.validate(wf)
    if probs:
        return {"id": v["id"], "ok": False, "error": "; ".join(probs)}
    t0 = time.time()
    pid = comfy.submit(wf)
    print(f"  提交 {pid}，等待…", flush=True)
    h = comfy.wait(pid, timeout=2400, poll=3)
    el = round(time.time() - t0, 1)
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
        return {"id": v["id"], "ok": False, "error": "无输出", "elapsed_s": el}
    return {"id": v["id"], "ok": True, "file": fn, "elapsed_s": el}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--resolution", default="544x960")
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--outdir", default="/tmp/face_test")
    ap.add_argument("--variant", default="",
                    help="只跑某个变体（A_current/B_neutral/C_nolora），留空=全跑")
    ap.add_argument("--tag", default="",
                    help="输出文件后缀标记，便于区分不同参考图")
    a = ap.parse_args()

    if not comfy.alive():
        print("❌ ComfyUI 未运行")
        return 1
    ref = Path(a.ref)
    if not ref.exists():
        print(f"❌ 参考图不存在: {ref}")
        return 1
    # LoadImage 只认 ComfyUI input 目录下的文件名
    ref_name = comfy.upload_image(str(ref))
    print(f"参考图已上传: {ref_name}")

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    todo = [v for v in VARIANTS
            if not a.variant or v["id"] == a.variant]
    if not todo:
        print(f"❌ 没有匹配的变体: {a.variant}")
        return 1
    for v in todo:
        print(f"\n[{v['id']}] {v['desc']}")
        try:
            r = run(v, ref_name, a.resolution, a.duration)
        except Exception as e:
            r = {"id": v["id"], "ok": False,
                 "error": f"{type(e).__name__}: {e}"[:300]}
        print("  " + str(r), flush=True)
        results.append(r)
        if r.get("ok"):
            src = Path(cfg.get("settings", "paths.output_dir", "/output") or "/output") / r["file"]
            png = out / f"{v['id']}{('_'+a.tag) if a.tag else ''}.png"
            if src.exists():
                subprocess.run(["/usr/bin/ffmpeg" if Path("/usr/bin/ffmpeg").exists()
                                else "ffmpeg", "-y", "-v", "error",
                                "-ss", "2.0", "-i", str(src), "-frames:v", "1",
                                "-q:v", "2", str(png)], check=False)
                print(f"  抽帧 → {png} ({png.stat().st_size if png.exists() else 0} bytes)")
        comfy.free_vram()
        time.sleep(2)

    print("\n=== 汇总 ===")
    for r in results:
        print(f"  {r['id']}: {'OK' if r.get('ok') else 'FAIL'} "
              f"{r.get('elapsed_s','')}s {r.get('error','')}")
    print(f"\n帧文件在 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
