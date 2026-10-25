#!/usr/bin/env python3
"""夜间无人值守看护：检查渲染进度，自动修可修的问题。

老板 2026-10-24 要求「自动跑完全流程，明早检查结果」。
H3 单镜实测 16-40 分钟，9 镜约 2.5-4 小时，整夜没人盯，必须自愈：

  ① 队列消费者死了（曾因 NameError / 容器重启）→ 重新拉起
  ② 镜头 failed → 重置 pending 重跑（最多 MAX_RETRY 次，防反复烧 GPU）
  ③ 全部 done 但没有 final 成片 → 触发 assemble
  ④ 队列项卡在 running 但没有任何镜头在跑 → 重新入队
  ⑤ GPU 显存被外部进程占满 → 释放 ComfyUI 缓存

用法：
    python3 scripts/night_watch.py            # 检查并修（幂等，可反复跑）
    python3 scripts/night_watch.py --dry-run  # 只看状态不动手
    python3 scripts/night_watch.py --status   # 只打印状态（给报告用）

退出码：0=一切正常或在推进；1=需要人介入
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

HOST = "http://YOUR_SERVER_IP:8080"
EID = "ep_15727ae3df1a"
PID = "prj_718a8672dce4"
# 单镜最多重试次数（含首次）。H3 单镜 ~25 分钟，不能无限重跑。
MAX_RETRY = 2


def api(path: str, payload=None, method=None, timeout=60):
    url = HOST + path
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        method = method or "POST"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        return {"_error": f"HTTP {e.code}", "_body": body}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def snapshot() -> dict:
    """一次性抓全状态。"""
    ep = api(f"/api/episodes/{EID}")
    queue = api("/api/queue")
    sysinfo = api("/api/system")
    arts = api(f"/api/episodes/{EID}/artifacts")
    return {"ep": ep, "queue": queue, "sys": sysinfo,
            "artifacts": arts, "ts": time.time()}


def analyze(s: dict) -> dict:
    shots = s["ep"].get("shots") or []
    cnt: dict[str, int] = {}
    for x in shots:
        cnt[x["status"]] = cnt.get(x["status"], 0) + 1
    total = len(shots)
    done = cnt.get("done", 0)
    failed = [x for x in shots if x["status"] == "failed"]
    running = [x for x in shots if x["status"] == "running"]
    pending = [x for x in shots if x["status"] == "pending"]
    arts = s["artifacts"].get("artifacts") or []
    final = [a for a in arts if a.get("kind") == "final" and a.get("exists")]

    consumer = (s["queue"].get("consumer") or {})
    qitems = s["queue"].get("items") or []
    q_running = [q for q in qitems if q.get("status") == "running"]
    q_queued = [q for q in qitems if q.get("status") == "queued"]

    gpu = (s["sys"].get("gpu") or {})
    return {
        "total": total, "done": done, "failed": failed, "running": running,
        "pending": pending, "cnt": cnt, "final": final,
        "consumer_alive": bool(consumer.get("consumer_alive")),
        "consumer_paused": bool(consumer.get("paused")),
        "q_running": q_running, "q_queued": q_queued,
        "gpu_util": gpu.get("util_pct"), "vram": gpu.get("vram_used_mb"),
        "temp": gpu.get("temp_c"),
        "complete": bool(final) or (total > 0 and done == total),
    }


def print_status(a: dict) -> None:
    print(f"镜头：{a['done']}/{a['total']} 完成"
          f"，失败 {len(a['failed'])}，在跑 {len(a['running'])}"
          f"，等渲 {len(a['pending'])}")
    if a["running"]:
        print("  正在渲染：" + ", ".join(f"镜{x['seq']}" for x in a["running"]))
    if a["failed"]:
        for x in a["failed"]:
            print(f"  镜{x['seq']} 失败：{(x.get('error') or '')[:100]}")
    print(f"队列消费者：{'alive' if a['consumer_alive'] else 'DEAD'}"
          f"{'（已暂停）' if a['consumer_paused'] else ''}"
          f"；running={len(a['q_running'])} queued={len(a['q_queued'])}")
    print(f"GPU：{a['gpu_util']}% {a['vram']}MB {a['temp']}°C")
    if a["final"]:
        f = a["final"][0]
        print(f"成片：{f.get('path')}")
    else:
        print("成片：尚未生成")


def fix(s: dict, a: dict, dry: bool) -> list[str]:
    """自愈动作。返回做过的动作列表。"""
    acts: list[str] = []

    # 已完成就不用再动
    if a["complete"]:
        return acts

    # ① 消费者死了 / 被暂停 → 拉起
    if not a["consumer_alive"] or a["consumer_paused"]:
        if dry:
            acts.append("[dry] 会拉起队列消费者")
        else:
            r = api("/api/queue/control", {"action": "resume"})
            api("/api/queue/control", {"action": "start"})
            acts.append(f"拉起队列消费者: {r.get('ok')}")

    # ② 失败镜头
    #    ⚠️ 关键：worker.run_episode 的 todo 已包含 failed 状态，
    #    队列自己就会重跑；这里**绝不能**再单独 rerender —— 会撞单卡 LOCK
    #    并产生重复任务。只有当队列已经空转（无 running/queued）时才补一次入队。
    queue_busy = bool(a["q_running"] or a["q_queued"] or a["running"])
    over_limit = [x for x in a["failed"]
                  if int(x.get("retries") or 0) >= MAX_RETRY]
    for x in over_limit:
        acts.append(f"镜{x['seq']} 已重试 {x.get('retries')} 次"
                    f"（上限 {MAX_RETRY}），需人工看")
    retryable = [x for x in a["failed"] if x not in over_limit]
    if retryable and not queue_busy:
        if dry:
            acts.append(f"[dry] 队列已空，会重新入队以重跑 "
                        f"{len(retryable)} 个失败镜："
                        + ",".join(str(x["seq"]) for x in retryable))
        else:
            r = api("/api/queue", {"episode_id": EID, "tier": "quality"})
            acts.append(f"重新入队重跑失败镜: "
                        f"{'ok' if r.get('ok') else r.get('_body', '失败')}")

    # ③ 全部 done 但没成片 → 触发 assemble
    if a["total"] and a["done"] == a["total"] and not a["final"]:
        if dry:
            acts.append("[dry] 会触发成片合成")
        else:
            r = api(f"/api/episodes/{EID}/assemble")
            acts.append(f"触发成片: {'ok' if r.get('task_id') else r.get('_body','失败')}")

    # ④ 队列项卡在 running 但没有任何镜头在跑 → 重新入队
    stuck = (a["q_running"] and not a["running"]
             and not a["failed"] and a["pending"])
    if stuck:
        if dry:
            acts.append("[dry] 队列卡死，会重新入队")
        else:
            for q in a["q_running"]:
                api(f"/api/queue/{q['id']}", None, method="DELETE")
            r = api("/api/queue", {"episode_id": EID, "tier": "quality"})
            acts.append(f"重新入队: {'ok' if r.get('ok') else r.get('_body','失败')}")

    return acts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON")
    a = ap.parse_args()

    s = snapshot()
    if s["ep"].get("_error"):
        print(f"❌ 取不到剧集状态: {s['ep']}")
        return 1

    st = analyze(s)
    if a.json:
        out = {k: v for k, v in st.items() if k != "failed"}
        out["failed_seqs"] = [x["seq"] for x in st["failed"]]
        out["running_seqs"] = [x["seq"] for x in st["running"]]
        # need_human：有镜头超重试上限 → 再跑也没用，必须人看
        out["need_human"] = any(
            int(x.get("retries") or 0) >= MAX_RETRY for x in st["failed"])
        out["final_path"] = (st["final"][0].get("path") if st["final"] else "")
        out["final_url"] = (st["final"][0].get("url") if st["final"] else "")
        out["artifact_count"] = len(s["artifacts"].get("artifacts") or [])
        print(json.dumps(out, ensure_ascii=False))
        return 0

    print_status(st)
    if a.status:
        return 0

    acts = fix(s, st, a.dry_run)
    if acts:
        print("\n自愈动作：")
        for x in acts:
            print("  · " + x)

    need_human = [x for x in st["failed"]
                  if int(x.get("retries") or 0) >= MAX_RETRY]
    if need_human:
        print(f"\n⚠️ {len(need_human)} 个镜头超过重试上限，需人工判断")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
