"""冒烟渲染 —— 在容器内跑一次真实 H3 出图。

用法（在 .253 上）：
    docker exec -e SDF_COMFYUI_URL=http://sdf-comfyui:8188 sdf-api \
        python /app/smoke_render.py [分辨率] [时长] [步数]

例：
    docker cp smoke_render.py sdf-api:/app/smoke_render.py
    docker exec sdf-api python /app/smoke_render.py 544x960 3 8
"""
from __future__ import annotations

import json
import sys
import time

from pipeline import comfy, workflow

res = sys.argv[1] if len(sys.argv) > 1 else "544x960"
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
steps = int(sys.argv[3]) if len(sys.argv) > 3 else 8
engine = sys.argv[4] if len(sys.argv) > 4 else "h3"

PROMPT = (
    "A 26-year-old Chinese woman with long black hair stands on a rooftop at dusk, "
    "city skyline behind her, wind blowing her hair, warm golden hour light, "
    "medium shot, slow push in, cinematic, film grain, highly detailed"
)

print(f"[1/4] ComfyUI alive: {comfy.alive()}", flush=True)
comfy.free_vram()
time.sleep(2)

print(f"[2/4] building {engine} {res} {dur}s {steps}steps", flush=True)
wf = workflow.build(engine, prompt=PROMPT, resolution=res, duration=dur,
                    steps=steps, seed=20261021, filename_prefix="smoke")
print(f"       nodes={len(wf)}", flush=True)
problems = workflow.validate(wf)
print(f"       validate: {problems or 'OK'}", flush=True)

print("[3/4] submitting", flush=True)
t0 = time.time()
pid = comfy.submit(wf)
print(f"       prompt_id={pid}", flush=True)


def prog(e):
    print(f"       -> {e}", flush=True)


h = comfy.wait(pid, timeout=3600, on_progress=prog)
elapsed = time.time() - t0

print(f"[4/4] DONE in {elapsed/60:.1f} min ({elapsed:.0f}s)", flush=True)
outs = comfy.outputs_of(h)
print(json.dumps(outs, ensure_ascii=False, indent=2)[:1500], flush=True)

if not outs:
    print("❌ 无产物", flush=True)
    sys.exit(1)
print("✅ OK", flush=True)
