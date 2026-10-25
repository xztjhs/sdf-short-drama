"""Ref2VA 锁脸失效的根因对照实验（2026-10-25）。

背景：《最后一个房间》9 镜实测人脸明显漂移 —— 能看出是"同一类人"
（中年中国男性、眼镜、绿夹克），但不是参考照片里那个人。

已排除的：
  ✗ 参考图没接上 —— 实测 workflow 里 `ref_images.ref_image_0` 正确指向
    LoadImage 节点，ref2va_enabled=true，8 个有示例人物A的镜头全部注入参考图。

待验证的两个假设：
  H1【参考图被压缩损失细节】
     当前 ref2va_image_size="match"：按生成面积缩放，
     参考图 1800x1200 → 1244x829，人脸从 ~240px 降到 ~166px。
     身份编码器吃到的脸太小，细节不够。
     修法：改 "max"（短边 2048）→ 人脸 ~410px，2.5 倍细节。

  H2【文字锚点覆盖参考图】
     生产 prompt 写死"圆脸，黑色短发略硬，黑框半框眼镜…"。
     但参考照片本人的脸并非标准"圆脸"（有下颌线），
     文字把人往"通用中年圆脸男"方向拉 → 越跑越不像本人。
     修法：删掉外貌形容词，只写「长相严格参考 <Picture 1>」。

变体设计（同参考图 / 同镜头内容 / 同 seed / 生产分辨率 1344x768 / 6 秒）：
  A_base    长锚点 + match     ← 复现当前生产
  B_neutral 中性短提示 + match  ← 单独测 H2
  C_max     中性短提示 + max    ← 同时测 H1+H2

用法（api 容器内跑，避免宿主机依赖）：
    docker exec sdf-api python /app/test_face_rootcause.py \
        --ref /input/uploads/ref_1792680139540.jpg --variant A_base

跑完自动抽首帧并落 /output/_facetest/，宿主机可直接拉。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from pipeline import comfy, workflow  # noqa: E402

RESOLUTION = "1344x768"
DURATION = 6.0
SEED = 20261025

# 参考照片的真实特征（酒店木饰走廊，橄榄绿夹克 + 浅蓝条纹衬衫）
SCENE = ("酒店木饰走廊中段，深棕色竖向木纹护墙板，暖黄壁灯从画面上方斜照，"
         "深灰地毯带暗红回纹")

# 生产里用的长锚点（照抄《最后一个房间》实际 prompt 的人物段）
ANCHOR_VERBOSE = ("三十多岁中国男性示例人物A，圆脸，黑色短发略硬，黑框半框眼镜，"
                  "穿深橄榄绿连帽夹克内搭浅蓝细条纹衬衫，体型中等偏壮，表情克制")

VARIANTS = {
    "A_base": {
        "desc": "长锚点 + ref_image_size=match（复现当前生产）",
        "prompt": (f"{ANCHOR_VERBOSE}；他站在{SCENE}，正面朝向镜头，"
                   f"右手把黑色对讲机举到胸前，眼睛看向镜头；"
                   f"本镜景别：近景，镜头固定，一镜到底；写实电影感，浅景深"),
        "size": "match",
    },
    "B_neutral": {
        "desc": "中性短提示 + match（单独验证 H2 文字干扰）",
        "prompt": (f"人物长相严格参考 <Picture 1>，必须是同一个人；"
                   f"他站在{SCENE}，正面朝向镜头，"
                   f"右手把黑色对讲机举到胸前，眼睛看向镜头；"
                   f"本镜景别：近景，镜头固定，一镜到底；写实电影感，浅景深"),
        "size": "match",
    },
    "C_max": {
        "desc": "中性短提示 + max（同时验证 H1+H2）",
        "prompt": (f"人物长相严格参考 <Picture 1>，必须是同一个人；"
                   f"他站在{SCENE}，正面朝向镜头，"
                   f"右手把黑色对讲机举到胸前，眼睛看向镜头；"
                   f"本镜景别：近景，镜头固定，一镜到底；写实电影感，浅景深"),
        "size": "max",
    },
}


def run(vid: str, v: dict, ref_name: str, outdir: Path) -> dict:
    wf = workflow.build_h3_ref2va(
        prompt=v["prompt"], resolution=RESOLUTION, duration=DURATION,
        steps=8, seed=SEED, ref_images=[ref_name],
        ref_image_size=v["size"], turbo=True,
        filename_prefix=f"facetest_{vid}")
    probs = workflow.validate(wf)
    if probs:
        return {"id": vid, "ok": False, "error": "; ".join(probs)}

    t0 = time.time()
    pid = comfy.submit(wf)
    print(f"  提交 {pid}，等待（预计 12-20 分钟）…", flush=True)
    h = comfy.wait(pid, timeout=3000, poll=5)
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
        return {"id": vid, "ok": False, "error": "无输出", "elapsed_s": el}

    # 抽 1s / 3s 两帧，脸部区域裁剪放大，便于比对
    src = f"/output/{fn}"
    frames = []
    for sec in (1, 3):
        dst = outdir / f"{vid}_t{sec}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(sec), "-i", src,
             "-frames:v", "1", "-vf", "scale=900:-1", "-q:v", "2", str(dst)],
            capture_output=True, text=True)
        if r.returncode == 0 and dst.exists():
            frames.append(str(dst))
    return {"id": vid, "ok": True, "file": fn, "elapsed_s": el,
            "frames": frames, "size": v["size"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--variant", default="", help="A_base/B_neutral/C_max，留空=全跑")
    ap.add_argument("--outdir", default="/output/_facetest")
    a = ap.parse_args()

    if not comfy.alive():
        print("❌ ComfyUI 未运行")
        return 1
    ref = Path(a.ref)
    if not ref.exists():
        print(f"❌ 参考图不存在: {ref}")
        return 1

    ref_name = comfy.upload_image(str(ref))
    print(f"参考图已上传: {ref_name}")

    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    todo = [k for k in VARIANTS if not a.variant or k == a.variant]
    if not todo:
        print(f"❌ 没有匹配变体: {a.variant}")
        return 1

    results = []
    for vid in todo:
        v = VARIANTS[vid]
        print(f"\n[{vid}] {v['desc']}", flush=True)
        try:
            r = run(vid, v, ref_name, outdir)
        except Exception as e:
            r = {"id": vid, "ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        results.append(r)
        print("  " + str({k: r.get(k) for k in ("ok", "file", "elapsed_s", "error")}),
              flush=True)

    print("\n" + "=" * 60)
    for r in results:
        mark = "✅" if r.get("ok") else "❌"
        print(f"{mark} {r['id']}: {r.get('file') or r.get('error')}")
    print(f"\n帧文件在容器 {outdir}，宿主机对应 "
          f"/mnt/disks/nvme/docker/compose/sdf/output/_facetest/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
