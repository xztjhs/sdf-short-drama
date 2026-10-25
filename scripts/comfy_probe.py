#!/usr/bin/env python3
"""查 ComfyUI 队列与历史，定位对照实验进度。"""
import json
import urllib.request

import os
BASE = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read())


q = get("/queue")
running = q.get("queue_running") or []
pending = q.get("queue_pending") or []
print(f"running={len(running)} pending={len(pending)}")
for item in running:
    pid = item[1]
    print(f"  running prompt_id={pid}")

h = get("/history?max_items=20")
print(f"\n历史完成数: {len(h)}")
for pid, entry in h.items():
    st = entry.get("status") or {}
    ok = st.get("status_str") or "?"
    outs = entry.get("outputs") or {}
    files = []
    for node_out in outs.values():
        for kind in ("videos", "gifs", "images"):
            for it in (node_out.get(kind) or []):
                fn = it.get("filename")
                if fn:
                    files.append(fn)
    print(f"  {pid[:8]} status={ok} files={files}")
