"""v0.3 新功能验证：模型编辑、ComfyUI 配置、项目管理。

在 .253 上：
    docker cp scripts/verify_v03.py sdf-api:/app/
    docker exec sdf-api python /app/verify_v03.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
ok = True


def call(path, data=None, method=None, expect_error=False):
    from urllib.parse import quote
    # 路径里可能含中文（搜索关键词），URL 必须是 ascii
    safe_path = quote(path, safe="/?=&%")
    req = urllib.request.Request(
        BASE + safe_path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data else {},
        method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if expect_error:
            return {"__error__": e.code, "body": e.read().decode()[:200]}
        raise


def check(cond, label, detail=""):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {detail}" if detail else ""))
    if not cond:
        ok = False
    return cond


print("=" * 66)
print(" v0.3 新功能验证")
print("=" * 66)

# ---- 1. ComfyUI 对接 ----
print("\n[1] ComfyUI 对接配置")
c = call("/api/admin/comfy")
check(c["config"].get("base_url"), "配置已加载", c["config"]["base_url"])
check(c["live"]["alive"], "实时连通",
      f"v{c['live'].get('comfyui_version')} / {len(c['live'].get('devices') or [])} GPU")
t = call("/api/admin/comfy/test", {"base_url": c["effective_url"]})
check(t.get("ok"), "连通性测试接口", f"{t.get('latency_ms')}ms")
t2 = call("/api/admin/comfy/test", {"base_url": "http://127.0.0.1:59999"})
check(not t2.get("ok"), "错误地址能被识别", (t2.get("error") or "")[:40])

# ---- 2. 模型编辑 ----
print("\n[2] 模型编辑")
ms = call("/api/admin/models")["models"]
target = next(m for m in ms if m["id"] == "wan22-ti2v-5b")
old_note = target.get("note", "")

r = call("/api/admin/models/update",
         {"model_id": "wan22-ti2v-5b", "patch": {"note": "编辑测试"}})
check(r["model"].get("note") == "编辑测试", "改备注生效", r["model"].get("note"))

r = call("/api/admin/models/update",
         {"model_id": "wan22-ti2v-5b", "patch": {"note": old_note}})
check(r["model"].get("note") == old_note, "已恢复备注")

r = call("/api/admin/models/update",
         {"model_id": "wan22-ti2v-5b", "patch": {"id": "hack"}}, expect_error=True)
check(r.get("__error__") == 400, "禁止改 id", str(r.get("__error__")))

r = call("/api/admin/models/update",
         {"model_id": "wan22-ti2v-5b", "patch": {"bogus": 1}}, expect_error=True)
check(r.get("__error__") == 400, "未知字段被拒", str(r.get("__error__")))

r = call("/api/admin/models/h3-fl2va-int8", method="DELETE", expect_error=True)
check(r.get("__error__") == 400, "必需模型不可删", str(r.get("__error__")))

# ---- 3. 项目管理 ----
print("\n[3] 项目管理")
prj = call("/api/projects", {"title": f"v03验证_{__import__('time').strftime('%H%M%S')}"})
pid = prj["id"]
check(pid, "创建项目")

r = call(f"/api/projects/{pid}", {"title": "改名测试"}, method="PATCH")
check(r["title"] == "改名测试", "重命名")

r = call(f"/api/projects/{pid}/archive", {"archived": True})
check(r.get("archived") is True, "归档")
lst = call("/api/projects")["projects"]
check(all(p["id"] != pid for p in lst), "归档后默认列表不显示")
lst2 = call("/api/projects?include_archived=true")["projects"]
check(any(p["id"] == pid for p in lst2), "含归档列表能查到")
call(f"/api/projects/{pid}/archive", {"archived": False})

dup = call(f"/api/projects/{pid}/duplicate", {"title": "副本测试"})
check(dup.get("id") and dup["id"] != pid, "复制项目", dup.get("title"))

# 搜索（URL 含中文，走 quote 编码）
found = call("/api/projects?q=副本测试")["projects"]
check(any(p["id"] == dup["id"] for p in found), "搜索命中")

# 导出
import urllib.request as ur
from urllib.parse import quote as _q
with ur.urlopen(f"{BASE}/api/projects/{pid}/export", timeout=60) as resp:
    data = resp.read()
    ctype = resp.headers.get("Content-Type", "")
check(data[:2] == b"PK", "导出 ZIP", f"{len(data)} bytes, {ctype}")

# 批量删除
r = call("/api/projects/batch", {"action": "delete", "ids": [pid, dup["id"]]})
check(r.get("affected") == 2, "批量删除", str(r.get("affected")))

print("\n" + "=" * 66)
print(" ✅ 全部通过" if ok else " ❌ 有失败项")
print("=" * 66)
sys.exit(0 if ok else 1)
