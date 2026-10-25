"""列出失败镜头的错误详情。"""
import sys

from pipeline import db

db.init_db()

prefix = sys.argv[1] if len(sys.argv) > 1 else "v02流程"
prjs = db.list_projects()
tgt = next((p for p in prjs if p["title"].startswith(prefix)), None)
if not tgt:
    print(f"未找到项目: {prefix}")
    for p in prjs[:5]:
        print("  ", p["title"])
    sys.exit(1)

print(f"项目: {tgt['title']}  stage={tgt.get('stage')}\n")
found = False
for ep in db.list_episodes(tgt["id"]):
    for s in db.list_shots(ep["id"]):
        if s["status"] == "failed":
            found = True
            print(f"[第{ep['seq']}集] 镜{s['seq']}  engine={s.get('engine')}  "
                  f"res={s.get('resolution')}  retries={s.get('retries')}")
            print(f"  error: {(s.get('error') or '')[:700]}")
            print("-" * 70)

if not found:
    print("没有失败镜头")

print("\n最近 job 记录：")
for j in db.list_jobs(8):
    err = (j.get("error") or "")[:200]
    print(f"  {j.get('status'):8} {j.get('engine'):6} {err}")
