"""验证真实项目的导出 ZIP 内容（有剧集/镜头/成片）。"""
import io
import json
import sys
import urllib.request
import zipfile

BASE = "http://127.0.0.1:8000"

with urllib.request.urlopen(f"{BASE}/api/projects", timeout=30) as r:
    projects = json.loads(r.read())["projects"]

if not projects:
    print("没有项目")
    sys.exit(1)

# 挑镜头最多的那个
target = max(projects, key=lambda p: p.get("shot_total", 0))
print(f"目标项目: {target['title']}  镜头 {target.get('shot_total')}  "
      f"阶段 {target.get('stage')}\n")

with urllib.request.urlopen(f"{BASE}/api/projects/{target['id']}/export", timeout=120) as r:
    data = r.read()
    print(f"ZIP 大小: {len(data):,} bytes")

zf = zipfile.ZipFile(io.BytesIO(data))
names = zf.namelist()
print(f"条目数: {len(names)}")
for n in names:
    info = zf.getinfo(n)
    print(f"  {info.file_size:>10,}  {n}")

# 校验 project.json 可解析
pj = json.loads(zf.read("project.json"))
print(f"\nproject.json:")
print(f"  标题: {pj['title']}")
print(f"  角色: {len(pj.get('characters') or [])}")
print(f"  剧集: {len(pj.get('episodes') or [])}")
total_shots = sum(len(e.get("shots") or []) for e in (pj.get("episodes") or []))
print(f"  镜头: {total_shots}")

has_final = any(n.endswith(".mp4") for n in names)
print(f"\n含成片 mp4: {has_final}")

ok = total_shots > 0 and bool(pj.get("episodes"))
print("\n✅ 导出内容完整" if ok else "\n⚠️ 导出内容不完整")
sys.exit(0 if ok else 1)
