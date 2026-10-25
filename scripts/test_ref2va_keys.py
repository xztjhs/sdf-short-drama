"""Ref2VA 参考图接线回归测试（不需要 GPU，毫秒级）。

历史 bug（2026-10-23 定位，这是"人脸不像"的真正根因）：
  workflow.build_h3_ref2va 把参考图写成**嵌套 dict**：
      ref_images = {"ref_image_0": (node, 0)}
  schema 接受这个写法，但 ComfyUI 的 Autogrow 展开逻辑只认
  **带前缀的点号键**，于是 ref_images 被静默丢弃 → 参考图完全不参与计算。

  实证（同一 seed / 同一提示词，只改参考图）：
      0 张参考图        → 抽帧 md5 6be2e144c07f...
      1 张整图          → 抽帧 md5 6be2e144c07f...  完全相同
      1 张脸部特写      → 抽帧 md5 6be2e144c07f...  完全相同
      ref_image_size=max→ 仍是欧美西装男，无改善
      改成点号键后      → 抽帧 md5 88694a8e79c9...  生成人物与照片一致

  正确格式依据 ComfyUI 自身单测：
      tests-unit/execution_test/dynamic_inputs_test.py
      Autogrow 用例即 {"rows.item0": 0.5}

用法（api 容器内或本地仓库根目录）：
    python scripts/test_ref2va_keys.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import workflow  # noqa: E402

REF_NODE = "MiniMaxH3ReferenceToVideo"


def build(refs: list[str]):
    wf = workflow.build_h3_ref2va(
        prompt="人物长相严格参考 <Picture 1>。测试。",
        resolution="544x960", duration=3, steps=8, seed=1,
        ref_images=refs)
    node = next(n for n in wf.values() if n["class_type"] == REF_NODE)
    return wf, node


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        print(f"  {'✅' if cond else '❌'} {msg}")
        if not cond:
            failures.append(msg)

    print("[1] 单张参考图")
    wf, node = build(["a.jpg"])
    keys = set(node["inputs"])
    check("ref_images.ref_image_0" in keys,
          "使用点号键 ref_images.ref_image_0")
    check("ref_images" not in keys,
          "不存在嵌套 dict（会被静默丢弃）")
    check("ref_image_0" not in keys,
          "不存在无前缀扁平键（会报 unexpected kwarg）")
    check(bool(workflow.validate(wf)) is False,
          f"静态自检无问题 {workflow.validate(wf)}")

    print("[2] 多张参考图（顺序必须稳定）")
    wf, node = build(["a.jpg", "b.jpg", "c.jpg"])
    keys = set(node["inputs"])
    for i in range(3):
        check(f"ref_images.ref_image_{i}" in keys, f"存在 ref_image_{i}")
    check("ref_images.ref_image_3" not in keys, "不超过实际张数")

    print("[3] 每张参考图都接到独立 LoadImage 节点")
    wf, _ = build(["a.jpg", "b.jpg"])
    loaders = [n for n in wf.values() if n["class_type"] == "LoadImage"]
    check(len(loaders) == 2, f"LoadImage 节点数 = {len(loaders)} (期望 2)")
    check(sorted(l["inputs"]["image"] for l in loaders) == ["a.jpg", "b.jpg"],
          "图片路径正确")

    print("[4] 零参考图时不产生任何 ref 键")
    wf, node = build([])
    check(not any(k.startswith("ref_images") for k in node["inputs"]),
          "无 ref_images.* 键")
    check(bool(workflow.validate(wf)) is False,
          f"静态自检无问题 {workflow.validate(wf)}")

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("✅ Ref2VA 参考图接线正确（点号键，参考图会真正参与采样）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
