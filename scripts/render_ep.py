"""继续渲染指定剧集的剩余镜头，并自动成片。

用法： docker exec sdf-api python /app/render_ep.py <episode_id>
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
eid = sys.argv[1]


def call(path, data=None, method=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data else {},
        method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=3600) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


tid = call("/api/render", {"episode_id": eid})["task_id"]
print("task:", tid, flush=True)

t0 = time.time()
last_sig = None
for i in range(900):
    time.sleep(10)
    t = call("/api/tasks/" + tid)
    cur = (t.get("progress") or [{}])[-1]
    sig = (t.get("status"), cur.get("shot"), cur.get("phase"), cur.get("status"))
    if sig != last_sig:
        el = (time.time() - t0) / 60
        print("[%5.1fmin] %s 镜%s %s %s" % (
            el, t.get("status"), cur.get("shot", "?"),
            cur.get("phase", ""), cur.get("status", "")), flush=True)
        last_sig = sig
    if t.get("status") != "running":
        print("FINAL:", json.dumps(t.get("result") or t.get("error"),
                                   ensure_ascii=False), flush=True)
        break
