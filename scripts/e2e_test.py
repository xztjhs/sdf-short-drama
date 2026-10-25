"""端到端测试：通过 Web API 走完整流程（不依赖 LLM）。

用途：验证 project → episode → shot → render → assemble 全链路。
LLM 分镜那一步需要 API key，这里直接手工插入镜头绕过。

在 .253 上：
    docker cp scripts/e2e_test.py sdf-api:/app/e2e_test.py
    docker exec sdf-api python /app/e2e_test.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://localhost:8000"


def call(path: str, data: dict | None = None, method: str | None = None) -> dict:
    url = f"{BASE}{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


print("=" * 60)
print(" 端到端测试")
print("=" * 60)

# ---- 1. 建项目 ----
prj = call("/api/projects", {
    "title": f"E2E测试_{int(time.time())}",
    "synopsis": "验证链路",
    "style": "cinematic, modern urban, film grain",
})
pid = prj["id"]
print(f"\n[1] 项目: {pid}  {prj['title']}")

# ---- 2. 建剧集 ----
ep = call(f"/api/projects/{pid}/episodes", {"seq": 1, "title": "第1集", "script": "测试"})
eid = ep["id"]
print(f"[2] 剧集: {eid}")

# ---- 3. 手工插镜头（绕过 LLM）
# 通过 db 直接写，因为 API 没有「手工加镜头」端点
from pipeline import db

shots_spec = [
    dict(seq=1, prompt="Wide shot of an empty city street at night, rain reflecting neon signs, "
                       "cinematic, film grain",
         duration=3, shot_size="远景", dialogue=None, sfx=None),
    dict(seq=2, prompt="Close-up of a young woman's face, tears in eyes, dim warm light, "
                       "slow push in, cinematic",
         duration=3, shot_size="特写", dialogue="原来是你。", sfx=None),
]
for s in shots_spec:
    db.add_shot(eid, s["seq"], s["prompt"], duration=s["duration"],
                shot_size=s["shot_size"], dialogue=s["dialogue"], sfx=s["sfx"])
print(f"[3] 插入 {len(shots_spec)} 个镜头")

# ---- 4. 查规划 ----
plan = call(f"/api/episodes/{eid}/plan")
print(f"[4] 路由规划: {plan['summary']}")
for p in plan["plan"]:
    r = p["_route"]
    print(f"      镜{p['seq']} {r['engine']:6} {r['resolution']:10} {r['steps']}步")

# ---- 5. 触发渲染（只跑 1 镜，省时间）
print("\n[5] 触发渲染（max_shots=1）...")
r = call("/api/render", {"episode_id": eid, "max_shots": 1})
tid = r["task_id"]
print(f"    task: {tid}")

for i in range(200):
    time.sleep(5)
    t = call(f"/api/tasks/{tid}")
    prog = t.get("progress") or []
    last = prog[-1] if prog else {}
    st = t.get("status")
    print(f"    [{i*5:3d}s] {st:8} {last.get('phase','')} "
          f"{('镜'+str(last.get('shot'))) if last.get('shot') else ''} "
          f"{last.get('status','')}")
    if st != "running":
        break

print(f"\n[6] 任务结果: {json.dumps(t.get('result') or t.get('error'), ensure_ascii=False)}")

# ---- 6. 查产物 ----
ep_full = call(f"/api/episodes/{eid}")
done = [s for s in ep_full["shots"] if s["status"] == "done"]
print(f"\n[7] 完成镜头: {len(done)}/{len(ep_full['shots'])}")
for s in done:
    print(f"      镜{s['seq']} {s.get('engine')} {s.get('resolution')} "
          f"{s.get('duration_ms',0)/1000:.0f}s → {s.get('output_path')}")

failed = [s for s in ep_full["shots"] if s["status"] == "failed"]
if failed:
    print("\n❌ 失败镜头:")
    for s in failed:
        print(f"      镜{s['seq']}: {s.get('error')}")

print("\n" + "=" * 60)
print(" 完成" if done else " ❌ 无产物")
print("=" * 60)
sys.exit(0 if done else 1)
