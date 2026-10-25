"""端到端整集：建 4 镜（2 H3 + 2 Wan2.2）→ 全渲染 → 自动成片。

在 .253 上：
    docker cp scripts/test_episode.py sdf-api:/app/test_episode.py
    docker exec sdf-api python /app/test_episode.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from pipeline import db

BASE = "http://127.0.0.1:8000"


def call(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=600) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


# 4 镜：2 个有对白/音效(→H3) + 2 个空镜(→Wan2.2)
SHOTS = [
    dict(seq=1, duration=3.0, shot_size="远景",
         prompt="Wide shot of an empty city rooftop at dusk, city skyline in background, "
                "warm golden hour light, cinematic, film grain",
         dialogue=None, sfx=None),
    dict(seq=2, duration=3.0, shot_size="近景",
         prompt="Close-up of a young Chinese woman's face, long black hair, "
                "tears in her eyes, dim warm light from below, cinematic, shallow depth of field",
         dialogue="原来一直都是你。", sfx=None),
    dict(seq=3, duration=3.0, shot_size="中景",
         prompt="The woman turns away slowly, wind blowing her hair, "
                "city lights bokeh background, cinematic, film grain",
         dialogue=None, sfx="风声"),
    dict(seq=4, duration=3.0, shot_size="远景",
         prompt="Empty street at night, rain reflecting neon signs, no people, "
                "moody atmosphere, cinematic, film grain",
         dialogue=None, sfx=None),
]

print("=" * 66)
print(" 端到端整集测试（渲染 → 成片）")
print("=" * 66)

prj = call("/api/projects", {
    "title": f"整集E2E_{int(time.time())}",
    "synopsis": "端到端验证",
    "style": "modern urban realism, cinematic, 35mm film grain",
})
eid = call(f"/api/projects/{prj['id']}/episodes", {"seq": 1, "title": "第1集"})["id"]

for s in SHOTS:
    db.add_shot(eid, s["seq"], s["prompt"], duration=s["duration"],
                shot_size=s["shot_size"], dialogue=s["dialogue"], sfx=s["sfx"])

plan = call(f"/api/episodes/{eid}/plan")
print(f"\n项目 {prj['id']}  剧集 {eid}")
print(f"规划: {plan['summary']}")
for p in plan["plan"]:
    r = p["_route"]
    print(f"  镜{p['seq']} {r['engine']:6} {r['resolution']:10} {r['steps']}步  {r['reason']}")

print(f"\n预计 {plan['summary']['estimated_minutes']:.0f} 分钟，开始渲染（串行）...")
t0 = time.time()
tid = call("/api/render", {"episode_id": eid})["task_id"]

last = None
for i in range(600):
    time.sleep(10)
    t = call(f"/api/tasks/{tid}")
    prog = t.get("progress") or []
    cur = prog[-1] if prog else {}
    sig = (t.get("status"), cur.get("phase"), cur.get("index"), cur.get("status"))
    if sig != last:
        el = (time.time() - t0) / 60
        print(f"  [{el:5.1f}min] {t.get('status'):8} "
              f"{cur.get('phase','')} 镜{cur.get('shot','?')} "
              f"({cur.get('index','?')}/{cur.get('total','?')}) {cur.get('status','')}"
              + (f"  err={cur.get('error','')[:60]}" if cur.get("error") else ""))
        last = sig
    if t.get("status") != "running":
        break

el = (time.time() - t0) / 60
print(f"\n任务{('完成' if t.get('status')=='done' else t.get('status'))}  耗时 {el:.1f} 分钟")
print(f"结果: {json.dumps(t.get('result') or t.get('error'), ensure_ascii=False)}")

full = call(f"/api/episodes/{eid}")
done = [s for s in full["shots"] if s["status"] == "done"]
failed = [s for s in full["shots"] if s["status"] == "failed"]

print(f"\n镜头: 完成 {len(done)}/{len(full['shots'])}")
for s in done:
    print(f"  镜{s['seq']} {s.get('engine'):6} {s.get('resolution'):10} "
          f"{s.get('duration_ms',0)/1000:6.0f}s  {s.get('output_path')}")
for s in failed:
    print(f"  ❌ 镜{s['seq']} {s.get('error','')[:100]}")

# 成片
print(f"\n剧集状态: {full['status']}")
import glob, os
finals = sorted(glob.glob(f"/output/{prj['id']}/E01/E01_final.mp4"))
if finals:
    from pipeline import assemble
    fi = assemble.info(finals[0])
    print(f"✅ 成片: {finals[0]}")
    print(f"   {fi['width']}x{fi['height']} {fi['fps']}fps {fi['duration']:.2f}s "
          f"audio={fi['has_audio']}({fi['audio_channels']}ch) "
          f"{os.path.getsize(finals[0])/1e6:.2f}MB")
    ok = fi["width"] == 1080 and fi["height"] == 1920 and fi["has_audio"]
    print("\n" + ("=" * 66))
    print(" ✅ 端到端通过" if ok else " ⚠️ 尺寸/音轨不符")
    print("=" * 66)
    sys.exit(0 if ok else 1)
else:
    print(f"❌ 未生成成片（目录: /output/{prj['id']}/E01/）")
    for f in sorted(glob.glob(f"/output/{prj['id']}/E01/*"))[:10]:
        print("   ", os.path.basename(f))
    sys.exit(1)
