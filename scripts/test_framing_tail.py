"""测试：把构图锁定放在提示词最末尾 + 5秒时长，能否压住参考图的构图收敛。

已知（2026-10-23 实测）：
  · 带参考图 Ref2VA  → 3s 内从全景推到正面大特写（复刻定妆照构图）
  · 不带参考图      → 全程稳定全景，但人物长相漂移（头发变灰、脸不对）
  · 换全身参考图    → 0.5s 全景、1.5s 中景、2.5s 仍推成特写

本测试（约 5 分钟）：
  全身参考图 + 构图锁定放在 prompt **最末尾** + 5 秒 → 看是否能全程保持全景。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from pipeline import comfy, workflow  # noqa: E402
from pipeline.config import cfg  # noqa: E402

RES = "544x960"
DUR = 5.0
SEED = 999
REF = "/input/uploads/fullbody_laochen.png"

BODY = (
    "示例人物B：45岁中国男性，方形脸，下颌线硬朗，浓眉，短寸头两鬓花白，"
    "戴细框金属眼镜，眼角有细纹和浅浅法令纹，深蓝色工装外套内穿灰色Polo衫，"
    "胸前挂着工牌，体型偏瘦，神态沉稳。 "
    "【本镜】本镜景别：全景，镜头运动：固定："
    "深夜的数据中心工位区，一个穿深蓝工装的中年男人从走廊尽头走进画面，"
    "脚步沉稳，手里拎着保温杯，在显示器墙前停下，冷蓝的屏幕光打在他侧脸上；"
    "固定机位广角展现空旷工位区与远处机房金属门；4K写实真人，电影级画质，浅景深 "
    "画面中只有示例人物B一个人，不要出现其他人物。 "
)

# 关键：构图锁定放最后（末尾 token 通常影响最大）
TAIL = (
    "【构图锁定（最高优先级，必须遵守）】"
    "这是全景镜头：人物全身在画面中，头顶到脚底都在画内，"
    "人物高度不超过画面的三分之二，画面大部分是机房空间。"
    "全程禁止推近、禁止放大人物、禁止变成特写或胸部以上镜头，"
    "相机位置和焦段从头到尾完全不变。"
    "参考图只用来确定人物长相，不要用参考图的构图和景别。"
)

PROMPT = BODY + "背景声：深夜机房底噪 " + TAIL


def main() -> int:
    if not comfy.alive():
        print("❌ ComfyUI 未运行")
        return 1
    up = comfy.upload_image(REF)
    wf = workflow.build_h3_ref2va(
        prompt=PROMPT, resolution=RES, duration=DUR, steps=8, seed=SEED,
        ref_images=[up], ref_image_size="match", filename_prefix="framing_tail")
    pid = comfy.submit(wf)
    print("submitted", pid, flush=True)
    t0 = time.time()
    h = comfy.wait(pid, timeout=2400, poll=3)
    print("elapsed", round(time.time() - t0, 1))
    fn = None
    for kind in ("videos", "gifs", "images"):
        for it in comfy.outputs_of(h, kind):
            if it.get("filename"):
                sub = it.get("subfolder") or ""
                fn = (sub + "/" if sub else "") + it["filename"]
                break
        if fn:
            break
    print("file", fn)
    src = Path(cfg.get("settings", "paths.output_dir", "/output") or "/output") / fn
    out = Path("/tmp/framing_test")
    out.mkdir(parents=True, exist_ok=True)
    for t in ("0.5", "2", "3.5", "4.8"):
        dst = out / f"tail_{t}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", t, "-i", str(src),
                        "-frames:v", "1", "-q:v", "2", str(dst)], check=False)
        print(dst, dst.stat().st_size if dst.exists() else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
