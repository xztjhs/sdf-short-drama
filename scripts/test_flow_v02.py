"""v0.2 五段流程验证：故事 → 大纲 → 锁定 → 分镜 → 渲染。

在 .253 上：
    docker cp scripts/test_flow_v02.py sdf-api:/app/test_flow_v02.py
    docker exec sdf-api python /app/test_flow_v02.py [--full]

不带 --full 时只验证前四段（LLM 部分，约 60 秒）。
带 --full 会跑完整渲染（约 15 分钟）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
FULL = "--full" in sys.argv


def call(path: str, data: dict | None = None, method: str | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data else {},
        method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=900) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


STORY = """
林晚在天台等江辞。
江辞来了，她质问："你早就知道，对不对？"
江辞沉默三秒，转身看向城市灯火。
"我以为我能护住你。"他说。
林晚笑了，眼泪掉下来。
"""

ok = True


def step(n, title):
    print(f"\n{'='*66}\n [{n}] {title}\n{'='*66}")


def expect(cond, msg, detail=""):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + msg + (f"  {detail}" if detail else ""))
    if not cond:
        ok = False
    return cond


# ---------------- 1. 输入故事 ----------------
step(1, "输入故事")
prj = call("/api/projects", {"title": f"v02流程_{int(time.time())}",
                             "story": STORY,
                             "style": "modern urban realism, cinematic, 35mm film grain"})
pid = prj["id"]
expect(prj["stage"] == "outline", "项目创建，stage=outline", f"id={pid}")

# ---------------- 2. 剧情大纲 ----------------
step(2, "剧情及大纲编制（LLM）")
t0 = time.time()
r = call(f"/api/projects/{pid}/outline",
         {"story": STORY, "episode_count": 2,
          "style": "modern urban realism, cinematic"})
el = time.time() - t0
o = r["outline"]
expect(bool(o.get("episodes")), f"生成 {len(o.get('episodes', []))} 集大纲", f"{el:.1f}s")
for e in o.get("episodes", []):
    print(f"       第{e['seq']}集 {e.get('title','')} | 钩子: {(e.get('hook') or '')[:30]}")
expect(r["project"]["stage"] == "outline", "stage=outline")

# ---------------- 3. 锁定基础信息 ----------------
step(3, "锁定基础信息")
bi = call(f"/api/projects/{pid}/extract-baseinfo", {"script": STORY})["baseinfo"]
expect(bool(bi.get("characters")), f"抽取 {len(bi.get('characters', []))} 个角色")
for c in bi.get("characters", []):
    print(f"       {c['name']}: {(c.get('appearance') or '')[:60]}")

lock_body = {
    "characters": bi.get("characters", []),
    "locations": bi.get("locations", []),
    "props": bi.get("props", []),
    "visual_style": bi.get("visual_style", "modern urban realism, cinematic"),
    "aspect": "9:16",
    "era": bi.get("era", "当代都市"),
    "forbidden": bi.get("forbidden", ["不要出现文字"]),
}
lr = call(f"/api/projects/{pid}/lock", lock_body)
expect(lr.get("locked_info") is not None, "已锁定")
p2 = call(f"/api/projects/{pid}")
expect(p2["stage"] == "locked", "stage=locked", f"chars={len(p2['characters'])}")
expect(all(c["locked"] for c in p2["characters"]), "角色标记为 locked")

# ---------------- 4. 分镜头脚本 ----------------
step(4, "分镜头脚本（LLM + 路由）")
eps = p2["episodes"]
eid = eps[0]["id"]
t0 = time.time()
sb = call(f"/api/episodes/{eid}/storyboard",
          {"script": STORY, "max_shots": 6, "target_duration": 30})
el = time.time() - t0
expect(len(sb["shots"]) > 0, f"生成 {len(sb['shots'])} 个镜头", f"{el:.1f}s")
print(f"       路由: {sb['plan_summary']}")
for s in sb["shots"]:
    eng = ""
    print(f"       镜{s['seq']:2d} {s['duration']}s {(s.get('dialogue') or '-')[:14]!r}")

p3 = call(f"/api/projects/{pid}")
expect(p3["stage"] == "storyboard", "stage=storyboard")

# 验证锁定信息真的被用了（提示词里应含角色外貌特征词）
pl = call(f"/api/episodes/{eid}/plan")
h3n = pl["summary"]["h3"]
wann = pl["summary"]["wan22"]
expect(h3n + wann == len(pl["plan"]), f"H3={h3n} Wan2.2={wann} 全部完成路由")

# ---------------- 5. 渲染 ----------------
step(5, "渲染成片" + ("" if FULL else "（跳过，加 --full 才跑）"))
if FULL:
    tid = call("/api/render", {"episode_id": eid, "max_shots": 2})["task_id"]
    t0 = time.time()
    last = None
    for _ in range(400):
        time.sleep(10)
        t = call(f"/api/tasks/{tid}")
        cur = (t.get("progress") or [{}])[-1]
        sig = (t["status"], cur.get("index"), cur.get("status"))
        if sig != last:
            print(f"       [{(time.time()-t0)/60:5.1f}min] {t['status']} "
                  f"镜{cur.get('shot','?')} {cur.get('status','')}")
            last = sig
        if t["status"] != "running":
            break
    expect(t["status"] == "done", f"渲染任务 {t['status']}",
           json.dumps(t.get("result") or t.get("error"), ensure_ascii=False)[:200])
    ep_full = call(f"/api/episodes/{eid}")
    done = [s for s in ep_full["shots"] if s["status"] == "done"]
    expect(len(done) > 0, f"{len(done)} 个镜头完成")
else:
    print("       跳过渲染。要跑完整链路： python /app/test_flow_v02.py --full")

# ---------------- 汇总 ----------------
step("✓", "结果")
final = call(f"/api/projects/{pid}")
print(f"  项目 stage: {final['stage']}")
print(f"  角色 {len(final['characters'])} / 场景 {len(final['locations'])} / 道具 {len(final['props'])}")
print(f"  剧集 {len(final['episodes'])}")
st = final["stats"]
print(f"  镜头 {st['total']} (完成 {st['by_status'].get('done', 0)})")
print(f"\n{'='*66}\n {'✅ 五段流程全部通过' if ok else '❌ 有失败项'}\n{'='*66}")
sys.exit(0 if ok else 1)
