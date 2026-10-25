"""LLM 分镜链路测试 —— 验证 icompify 调用 + JSON 解析 + 入库。

在 .253 上：
    docker cp scripts/test_storyboard.py sdf-api:/app/test_storyboard.py
    docker exec sdf-api python /app/test_storyboard.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from pipeline import db, script as llm

BASE = "http://localhost:8000"


def call(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


SCRIPT = """
林晚在天台等江辞。
江辞来了，她质问："你早就知道，对不对？"
江辞沉默三秒，转身看向城市灯火。
"我以为我能护住你。"他说。
林晚笑了，眼泪掉下来。
"""

print("=" * 60)
print(" LLM 分镜链路测试")
print("=" * 60)

print(f"\n[0] 配置: base={llm.BASE_URL} model={llm.MODEL} key={'有' if llm.API_KEY else '无'}")
if not llm.API_KEY:
    print("❌ 无 API key")
    sys.exit(1)

# ---- 1. 直接测 LLM ----
print("\n[1] 直连 LLM 测试...")
t0 = time.time()
try:
    raw = llm.chat([{"role": "user", "content": "只回复两个字：收到"}], max_tokens=50)
    print(f"    ✅ 响应 {time.time()-t0:.1f}s: {raw.strip()[:50]}")
except Exception as e:
    print(f"    ❌ 失败: {e}")
    sys.exit(1)

# ---- 2. 分镜生成 ----
print("\n[2] 分镜生成...")
t0 = time.time()
try:
    data = llm.storyboard(SCRIPT, style="modern urban realism, cinematic, 35mm film grain",
                          max_shots=8, target_duration=45)
    el = time.time() - t0
except Exception as e:
    print(f"    ❌ 失败: {type(e).__name__}: {e}")
    sys.exit(1)

print(f"    ✅ {el:.1f}s · 标题={data.get('title')}")
print(f"    角色: {[c.get('name') for c in data.get('characters', [])]}")
print(f"    镜头数: {len(data['shots'])}")
for s in data["shots"][:6]:
    dlg = (s.get("dialogue") or "")[:14]
    print(f"      镜{s['seq']:2d} {s['duration']:>4.1f}s {s.get('shot_size',''):4} "
          f"dlg={dlg!r:18} prompt={s['prompt'][:48]}")

# ---- 3. 走 API 落库 ----
print("\n[3] 通过 API 落库...")
prj = call("/api/projects", {"title": f"LLM测试_{int(time.time())}", "style": "cinematic"})
ep = call(f"/api/projects/{prj['id']}/episodes", {"seq": 1, "title": "第1集", "script": SCRIPT})
r = call(f"/api/episodes/{ep['id']}/storyboard",
         {"script": SCRIPT, "max_shots": 8, "target_duration": 45})
print(f"    ✅ 入库 {len(r['shots'])} 镜")
print(f"    路由: {r['plan_summary']}")

ep_full = call(f"/api/episodes/{ep['id']}")
print(f"    角色数: {len(ep_full['shots'])}")  # 占位
for s in ep_full["shots"][:5]:
    print(f"      镜{s['seq']:2d} {s['duration']}s status={s['status']} "
          f"chars={s['characters']}")

print("\n" + "=" * 60)
print(" ✅ 链路通" if r["shots"] else " ❌ 无镜头")
print("=" * 60)
sys.exit(0 if r["shots"] else 1)
