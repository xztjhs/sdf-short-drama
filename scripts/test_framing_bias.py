"""验证「参考图会拉近构图」假设（低成本快速实验）。

背景：第1镜设定全景，实测 3 次重渲都会在 4-5 秒后推成正面大特写，
和定妆照构图一致。加「一镜到底」指令无效。

假设：Ref2VA 的参考图参与每一步采样，模型不只是抄了脸，也抄了构图。

实验（同 prompt、同 seed、3 秒低分辨率，约 3 分钟/条）：
  R1  带定妆照参考图（Ref2VA）
  R2  不带参考图（纯文本 + 场景卡）

判读：R2 若全程保持全景，说明假设成立 →
      可按景别分流：全景不用 Ref2VA，近景/特写才用。

用法（api 容器内）：
    docker exec sdf-api python /tmp/test_framing.py
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
DUR = 3.0
SEED = 777

# 第1镜的完整提示词（含全景锁定 + 一镜到底 + 单人约束）
PROMPT = (
    "示例人物B：45岁中国男性，方形脸，下颌线硬朗，浓眉，短寸头两鬓花白，"
    "戴细框金属眼镜，眼角有细纹和浅浅法令纹，深蓝色工装外套内穿灰色Polo衫，"
    "胸前挂着工牌，体型偏瘦，神态沉稳。 "
    "【本镜】本镜景别：全景，镜头运动：固定，画面构图以此描述为准，"
    "不要把镜头拉远，参考图只用于锁定人物长相，不要照搬参考图的构图与景别，"
    "一镜到底，中途不切镜，景别与构图全程保持不变："
    "深夜的数据中心工位区，一个穿深蓝工装的中年男人从走廊尽头走进画面，"
    "脚步沉稳，手里拎着保温杯，在显示器墙前停下，冷蓝的屏幕光打在他侧脸上；"
    "固定机位广角展现空旷工位区与远处机房金属门；4K写实真人，电影级画质，浅景深 "
    "画面中只有示例人物B一个人，不要出现其他人物，不要有路人经过。"
    "【场景一致性（只用于保持空间与光线相同，不得改变下面本镜的景别与构图）】"
    "空间：数据中心运维工位区，深灰色防静电地板，一排显示器墙泛着冷蓝光，"
    "左侧是通往机房的厚重金属门，桌面散落着咖啡杯与线缆；"
    "光源：顶部条形灯管冷白光，显示器蓝光从正面打在脸上，门缝透出机房的红蓝指示灯。 "
    "背景声：深夜机房底噪"
)

REF = "/input/uploads/portrait_chr_f7c6a7515da1.png"
OUT = Path("/tmp/framing_test")


def run(tag: str, with_ref: bool) -> str | None:
    if with_ref:
        up = comfy.upload_image(REF)
        wf = workflow.build_h3_ref2va(
            prompt=PROMPT, resolution=RES, duration=DUR, steps=8, seed=SEED,
            ref_images=[up], ref_image_size="match",
            filename_prefix=f"framing_{tag}")
    else:
        wf = workflow.build_h3(
            prompt=PROMPT, negative="模糊，变形，水印",
            resolution=RES, duration=DUR, steps=8, seed=SEED,
            filename_prefix=f"framing_{tag}")
    pid = comfy.submit(wf)
    print(f"  [{tag}] 已提交 {pid}", flush=True)
    t0 = time.time()
    h = comfy.wait(pid, timeout=1800, poll=3)
    print(f"  [{tag}] 完成 {round(time.time()-t0,1)}s", flush=True)
    for kind in ("videos", "gifs", "images"):
        for it in comfy.outputs_of(h, kind):
            if it.get("filename"):
                sub = it.get("subfolder") or ""
                return (sub + "/" if sub else "") + it["filename"]
    return None


def frames(fn: str, tag: str) -> None:
    src = Path(cfg.get("settings", "paths.output_dir", "/output") or "/output") / fn
    OUT.mkdir(parents=True, exist_ok=True)
    for t in ("0.5", "1.5", "2.5"):
        dst = OUT / f"{tag}_{t}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", t, "-i", str(src),
             "-frames:v", "1", "-q:v", "2", str(dst)], check=False)
        print(f"    {dst} {dst.stat().st_size if dst.exists() else 0}B")


def main() -> int:
    if not comfy.alive():
        print("❌ ComfyUI 未运行")
        return 1
    for tag, with_ref in (("ref", True), ("noref", False)):
        print(f"[{tag}] with_ref={with_ref}")
        fn = run(tag, with_ref)
        if fn:
            print(f"  输出: {fn}")
            frames(fn, tag)
        comfy.free_vram()
        time.sleep(2)
    print(f"\n帧文件在 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
