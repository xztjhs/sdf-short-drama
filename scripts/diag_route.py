"""诊断路由：列出每个镜头的引擎决策依据。"""
import json
import sys
import urllib.request

from pipeline import router

BASE = "http://127.0.0.1:8000"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read())


prjs = get("/api/projects")["projects"]
prefix = sys.argv[1] if len(sys.argv) > 1 else "v02流程"
target = next((p for p in prjs if p["title"].startswith(prefix)), None)
if not target:
    print(f"未找到标题以 {prefix} 开头的项目")
    sys.exit(1)

full_prj = get("/api/projects/" + target["id"])
ep = full_prj["episodes"][0]
full = get("/api/episodes/" + ep["id"])

print(f"项目: {target['title']}  剧集: {ep['seq']}  镜头: {len(full['shots'])}\n")
print(f"{'镜':>2} {'引擎':<6} {'依据':<28} {'sfx':<14} prompt")
print("-" * 110)

h3 = wan = 0
est = 0.0
for s in full["shots"]:
    d = router.route(s)
    if d.engine == "h3":
        h3 += 1
    else:
        wan += 1
    est += router.cost_of(d.engine, d.resolution) * (float(s.get("duration") or 5) / 5.0)
    sfx = (s.get("sfx") or "-").replace("\n", " ")[:12]
    pr = (s.get("prompt") or "").replace("\n", " ")[:48]
    print(f"{s['seq']:>2} {d.engine:<6} {d.reason:<28} {sfx:<14} {pr}")

print("-" * 110)
print(f"H3={h3}  Wan2.2={wan}  预计 {est:.0f} 分钟 = {est/60:.1f} 小时")
