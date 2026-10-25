"""验证目录命名 + 重命名同步 + 翻译 + 角色库（接口层）。"""
import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

BASE = "http://127.0.0.1:8000"
ok = True


def call(path, data=None, method=None, expect_error=False):
    req = urllib.request.Request(
        BASE + quote(path, safe="/?=&%"),
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data else {},
        method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if expect_error:
            return {"__code__": e.code, "body": e.read().decode()[:200]}
        raise


def check(cond, label, detail=""):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {detail}" if detail else ""))
    if not cond:
        ok = False
    return cond


print("=" * 66)
print(" 目录命名 / 重命名 / 翻译 / 角色库 验证")
print("=" * 66)

# ---- 1. 翻译 ----
print("\n[1] LLM 翻译")
t = call("/api/translate", {"text": "星矿赘婿", "kind": "slug"})
check(t.get("ok") and t["result"], "中文→英文路径名", t.get("result"))
slug1 = t.get("result", "")

t2 = call("/api/translate", {"text": "26岁中国女性，黑色长发", "kind": "text"})
check(t2.get("ok") and len(t2["result"]) > 5, "中文→英文描述",
      (t2.get("result") or "")[:50])

t3 = call("/api/translate", {"text": "cinematic lighting", "kind": "text"})
check(t3.get("ok") and any("\u4e00" <= ch <= "\u9fff" for ch in t3["result"]),
      "英文→中文", (t3.get("result") or "")[:30])

# ---- 2. 项目创建带 slug / 目录 ----
print("\n[2] 项目目录命名")
prj = call("/api/projects", {"title": "星矿赘婿", "slug": slug1 or "xingkuang-zhuixu"})
pid = prj["id"]
check(prj.get("slug"), "slug 已存", prj.get("slug"))
od = prj.get("out_dir") or ""
check(len(od) == 13 + 1 + len(prj.get("slug", "")) or "_" in od,
      "目录名格式 时间_slug", od)
import re
check(bool(re.match(r"^\d{8}-\d{4}_", od)), "时间前缀正确", od[:14])

# ---- 3. 重命名同步目录 ----
print("\n[3] 重命名同步英文名与目录")
old_dir = od
r = call(f"/api/projects/{pid}", {"title": "赘婿逆袭"}, method="PATCH")
new_dir = r.get("out_dir")
new_slug = r.get("slug")
check(new_slug and new_slug != prj.get("slug"), "英文名已重算", f"{prj.get('slug')} → {new_slug}")
check(new_dir != old_dir, "目录名已变", f"{old_dir} → {new_dir}")
check(new_dir.startswith(old_dir.split("_")[0]), "时间戳保留",
      new_dir.split("_")[0])

# ---- 4. 显式 slug 优先级 ----
print("\n[4] 手工指定英文名")
r2 = call(f"/api/projects/{pid}", {"title": "赘婿逆袭", "slug": "my-custom-name"},
          method="PATCH")
check(r2.get("slug") == "my-custom-name", "自定义 slug 生效", r2.get("slug"))
check(r2.get("out_dir", "").endswith("_my-custom-name"), "目录跟着变", r2.get("out_dir"))

# ---- 5. 角色库 ----
print("\n[5] 角色库")
lib = call("/api/character-lib", {"name": "测试角色A", "appearance": "25yo woman",
                                  "appearance_zh": "25岁女性", "tags": "测试"})
check(lib.get("id"), "库中新增角色")
found = call("/api/character-lib?q=测试角色")["characters"]
check(len(found) >= 1, "搜索命中", f"{len(found)} 个")

imp = call(f"/api/projects/{pid}/import-characters", {"lib_ids": [lib["id"]]})
check(imp.get("imported") == 1, "导入到项目", str(imp.get("imported")))
again = call(f"/api/projects/{pid}/import-characters", {"lib_ids": [lib["id"]]})
check(again.get("imported") == 0, "重复导入被去重")

saved = call(f"/api/projects/{pid}/save-to-lib", {})
check(saved.get("saved") == 0, "反向存入去重", str(saved.get("saved")))

# 清理
call(f"/api/character-lib/{lib['id']}", method="DELETE")
call(f"/api/projects/{pid}?purge_files=false", method="DELETE")

print("\n" + "=" * 66)
print(" ✅ 全部通过" if ok else " ❌ 有失败项")
print("=" * 66)
sys.exit(0 if ok else 1)
