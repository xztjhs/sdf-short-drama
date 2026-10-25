"""验证 v0.6.1 首帧上传修复（不需要 GPU）。

复现的 bug：
  镜1 无首帧 → Ref2VA 成功
  镜2 继承镜1尾帧（/output/.../lastframe.png）→ use_ref=False
       → 但没有上传到 ComfyUI input → LoadImage 报 Invalid image file → 失败
  镜2 失败后 prev_last_frame 清空 → 镜3 回到 Ref2VA 成功
  ⇒ 100% 奇数成功 / 偶数失败（示例人物A项目实测 37 镜失败）

本脚本用 monkeypatch 拦截 comfy.upload_image / comfy.submit，
断言「带 /output 绝对路径的首帧一定被上传」。

用法（api 容器内）：
    docker exec sdf-api python /app/test_firstframe_upload.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/app")

from pipeline import worker  # noqa: E402

calls: dict = {"upload": [], "submit": []}


def fake_upload(local_path: str, name: str | None = None) -> str:
    calls["upload"].append(str(local_path))
    return "uploaded_" + Path(local_path).name


def fake_submit(wf: dict) -> str:
    calls["submit"].append(wf)
    raise SystemExit("STOP_AFTER_SUBMIT")


def check(wf: dict, label: str) -> None:
    """检查 LoadImage 节点拿到的 image 是不是已上传的文件名。"""
    imgs = [n["inputs"].get("image") for n in wf.values()
            if n["class_type"] == "LoadImage"]
    ok = all(isinstance(i, str) and not i.startswith("/") for i in imgs)
    print(f"  {'✅' if ok else '❌'} {label}: LoadImage image={imgs}")
    return ok


def main() -> int:
    worker.comfy.upload_image = fake_upload
    worker.comfy.submit = fake_submit
    worker.comfy.free_vram = lambda *a, **k: None
    worker.db.update_shot = lambda *a, **k: None
    worker.db.create_job = lambda *a, **k: "job"
    worker.db.update_job = lambda *a, **k: None

    results = []

    # 场景1：有首帧（/output 绝对路径，模拟上镜尾帧），无角色参考图
    # ⚠️ 必须造一个真实存在的文件，否则 os.path.exists 会把它置空，
    #    测的就不是"带首帧"这条分支了（第一版测试就是栽在这）。
    fake_ff = "/tmp/fake_lastframe.png"
    src = Path("/app/static/../static/index.html")
    if not Path(fake_ff).exists():
        # 用任意真实 PNG：从 output 里找一个，找不到就生成 1x1 PNG
        cands = list(Path("/output").rglob("*_lastframe.png"))
        if cands:
            import shutil
            shutil.copy2(str(cands[0]), fake_ff)
        else:
            import base64
            Path(fake_ff).write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    print(f"测试用首帧: {fake_ff} 存在={Path(fake_ff).exists()}")
    shot1 = {
        "id": "sh_test1", "seq": 2, "prompt": "他在走廊后退",
        "dialogue": None, "characters": ["张三"],
        "first_frame": fake_ff,
        "duration": 5, "resolution": "544x960", "status": "pending",
        "engine": "h3", "seed": 1,
    }
    calls["upload"].clear(); calls["submit"].clear()
    try:
        worker.render_shot(shot1, project_id="prj_none", episode_seq=1)
    except SystemExit:
        pass
    uploads = list(calls["upload"])
    print(f"场景1 有首帧（应上传 1 次）: uploads={uploads}")
    ok1 = any("lastframe" in u for u in uploads)
    print(f"  {'✅' if ok1 else '❌'} 首帧被上传")
    if calls["submit"]:
        ok1 = ok1 and check(calls["submit"][0], "提交的图")
    results.append(ok1)

    # 场景2：无首帧、无参考图 → 无图，不该崩
    shot2 = dict(shot1)
    shot2.pop("first_frame")
    shot2["id"] = "sh_test2"
    calls["upload"].clear(); calls["submit"].clear()
    try:
        worker.render_shot(shot2, project_id="prj_none", episode_seq=1)
    except SystemExit:
        pass
    print(f"场景2 无首帧无参考图: uploads={len(calls['upload'])} submits={len(calls['submit'])}")
    ok2 = len(calls["submit"]) == 1
    print(f"  {'✅' if ok2 else '❌'} 仍能正常提交")
    results.append(ok2)

    print()
    if all(results):
        print("✅ 首帧上传修复验证通过（奇数成功/偶数失败的 bug 已修）")
        return 0
    print("❌ 修复未生效")
    return 1


if __name__ == "__main__":
    sys.exit(main())
