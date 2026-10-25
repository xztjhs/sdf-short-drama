"""跨集渲染队列 —— 单卡串行消费，分镜做完即可入队。

设计动机（2026-10-22 老板反馈）：
  以前是「一集分镜 → 立刻渲染 → 等 1-2 小时 → 再去做下一集分镜」，
  人被迫等 GPU。现在是「分镜做完就入队，人接着做下一集」，
  后台按队列顺序单卡串行跑，队列可单独监控/暂停/取消。

关键约束（不可违反）：
  1. A5000 24GB 上 H3 峰值 22GB —— 全局只有一个消费者线程，
     所有渲染都经过 worker.LOCK，绝不并发。
  2. 队列状态落 SQLite（表 render_queue），API 重启后未完成项仍是 queued。
  3. 消费者用「启动即恢复」语义：重启后把之前 running 的项重新排回 queued，
     避免出现「界面显示在跑、其实没人跑」的幽灵任务。

用法：
    q = QueueRunner(progress_factory=...)   # progress_factory(qid) -> 回调
    q.start()                                # 幂等
    q.pause() / q.resume() / q.wake()
"""
from __future__ import annotations

import json
import threading
import time
import traceback

from . import db, worker
from .config import cfg

_STATE = {"running": False, "paused": False, "thread": None}


def _log(msg: str) -> None:
    print(f"[queue] {msg}", flush=True)


def _app_module():
    """拿到正在运行的 API 模块。

    注意：容器里 app.py 是顶层模块（uvicorn app:app），仓库里是 web/app.py。
    两种名字都试，不写死 import，否则容器 /app 下直接 ImportError。
    """
    import sys
    for name in ("app", "web.app"):
        m = sys.modules.get(name)
        if m is not None and hasattr(m, "_task_update"):
            return m
    return None


def resume_stale() -> int:
    """启动时把上次中断的 running 项排回 queued（服务重启后自动续跑）。"""
    n = 0
    try:
        for item in db.queue_list(limit=500):
            if item["status"] == "running":
                db.queue_update(item["id"], status="queued",
                                error="服务重启，重新排队")
                n += 1
    except Exception as e:
        _log(f"恢复队列失败: {e}")
    if n:
        _log(f"恢复 {n} 个中断的队列任务")
    return n


def _emit_factory(qid: str, tid: str | None):
    """把 worker 的进度同时写进 queue.progress 与 task 表。"""
    def emit(ev: dict) -> None:
        try:
            db.queue_update(qid, progress=json.dumps(ev, ensure_ascii=False))
        except Exception:
            pass
        if tid:
            app = _app_module()
            if app is not None:
                try:
                    app._task_update(tid, ev)
                except Exception:
                    pass

    return emit


def run_one(item: dict) -> dict:
    """跑一个队列项：渲染剩余镜头（串行）→ 拼接 → 响度/字幕 → (超分)。"""
    qid = item["id"]
    eid = item["episode_id"]
    opts = item.get("options") or {}
    upscale = (opts.get("upscale") or "").strip()
    # v0.5：只有 H3。旧队列项里残留的 wan22 一律忽略。
    force_engine = "h3"
    max_shots = opts.get("max_shots") or None

    db.queue_update(qid, status="running", started_at=time.time())
    emit = _emit_factory(qid, item.get("task_id"))
    # v0.5：只有 H3 一个引擎。这里原先误用了未定义的 mode → NameError，
    # 导致队列消费者每次拿到项就崩，队列永远不推进（2026-10-24 实测）。
    emit({"phase": "queue", "status": "start", "episode": item.get("seq"),
          "engine_mode": force_engine})

    try:
        ep = db.get_episode(eid)
        if not ep:
            raise ValueError("剧集不存在（可能已被删除）")

        res = worker.run_episode(
            eid,
            quality_first=bool(opts.get("quality_first")),
            force_engine=force_engine,
            max_shots=int(max_shots) if max_shots else None,
            upscale=upscale,
            assemble_after=True,
            tier=(opts.get("tier") or ""),
            progress=emit)

        final = db.latest_artifact(eid, "final")
        out = (final or {}).get("path") or ""
        if not res.get("ok"):
            # 镜头都渲完但成片失败 → 算失败，带清楚原因
            # （否则界面显示“已完成”却没有成片，最难排）
            raise RuntimeError(res.get("assemble_error")
                               or "成片失败（未生成 final 产物）")
        db.queue_update(qid, status="done", finished_at=time.time(),
                        output=out, error=None,
                        progress=json.dumps(
                            {"phase": "queue", "status": "done",
                             "rendered": res.get("rendered"),
                             "failed": res.get("failed")},
                            ensure_ascii=False))
        emit({"phase": "queue", "status": "done", "output": out})
        _log(f"完成 {item.get('title') or eid} → {out}")
        return {"ok": True, "output": out}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        # 整集失败 → ComfyUI 里剩下的排队任务都是这集的残留，
        # 不清的话会继续空转烧 GPU（实测 8 小时、72 个任务）。
        try:
            from . import comfy
            left = comfy.clear_queue()
            if left:
                _log(f"清理 ComfyUI 残留队列 {left} 个")
        except Exception:
            pass
        db.queue_update(qid, status="failed", finished_at=time.time(),
                        error=err[:1000])
        emit({"phase": "queue", "status": "failed", "error": err[:300]})
        _log(f"失败 {item.get('title') or eid}: {err}")
        return {"ok": False, "error": err}


def _loop() -> None:
    _STATE["running"] = True
    _log("消费者启动")
    while _STATE["running"]:
        try:
            if _STATE["paused"]:
                time.sleep(2)
                continue
            item = db.queue_next()
            if not item:
                time.sleep(3)
                continue
            # GPU 预检：只有真的还要渲镜头时才需要（全都渲完只成片不占显存）。
            # 另：ComfyUI 自己缓存模型会恒占 60-90%，先尝试释放再判。
            try:
                shots = db.list_shots(item.get("episode_id") or "")
                todo = [s for s in shots if s["status"] in ("pending", "failed")]
                opts = item.get("options") or {}
                if opts.get("max_shots"):
                    todo = todo[:int(opts["max_shots"])]
            except Exception:
                todo = []
            if todo:
                try:
                    app = _app_module()
                    pre = app.gpu_preflight() if app is not None else {"ok": True}
                    if pre.get("ok") is False:
                        _log(f"显存 {pre.get('pct')}%，先释放缓存再判")
                        try:
                            from . import comfy
                            comfy.free_vram()
                            time.sleep(3)
                            pre = app.gpu_preflight() if app is not None else {"ok": True}
                        except Exception:
                            pass
                    if pre.get("ok") is False:
                        _log(f"显存仍占 {pre.get('pct')}%，等待中…")
                        time.sleep(30)
                        continue
                except Exception:
                    pass
            run_one(item)
            time.sleep(1)
        except Exception:
            _log("消费者异常:\n" + traceback.format_exc()[-1200:])
            time.sleep(5)
    _log("消费者退出")


def start() -> bool:
    """启动消费者线程（幂等）。返回是否新起了一个线程。"""
    if _STATE["thread"] and _STATE["thread"].is_alive():
        return False
    resume_stale()
    t = threading.Thread(target=_loop, name="sdf-render-queue", daemon=True)
    t.start()
    _STATE["thread"] = t
    return True


def stop() -> None:
    _STATE["running"] = False


def pause() -> None:
    _STATE["paused"] = True
    _log("队列已暂停")


def resume() -> None:
    _STATE["paused"] = False
    _log("队列已恢复")


def status() -> dict:
    max_concurrent = int(cfg.get("settings", "render.max_concurrent", 1) or 1)
    alive = bool(_STATE["thread"] and _STATE["thread"].is_alive())
    return {"consumer_alive": alive, "paused": _STATE["paused"],
            "max_concurrent": max_concurrent,
            "model_serial": True}


def enqueue(episode_id: str, *, quality_first: bool = False,
            force_engine: str | None = None, max_shots: int | None = None,
            upscale: str = "", task_id: str | None = None,
            tier: str = "", priority: int = 100) -> str:
    """把一集加入渲染队列（重复入队会先去重已有的 queued 项）。"""
    ep = db.get_episode(episode_id) or {}
    # 去重：同一集已有排队项 → 直接返回旧的
    for it in db.queue_list("queued", limit=200):
        if it.get("episode_id") == episode_id:
            db.queue_update(it["id"], options=json.dumps({
                "quality_first": quality_first,
                "force_engine": "h3",
                "max_shots": max_shots,
                "upscale": upscale,
                "tier": tier,
            }, ensure_ascii=False), task_id=task_id or it.get("task_id"))
            return it["id"]
    qid = db.queue_add(
        episode_id,
        project_id=ep.get("project_id") or "",
        seq=int(ep.get("seq") or 0),
        title=ep.get("title") or "",
        engine_mode=(ep.get("engine_mode") or "auto"),
        options={"quality_first": quality_first,
                 "force_engine": "h3",
                 "max_shots": max_shots,
                 "upscale": upscale,
                 "tier": tier},
        priority=priority)
    if task_id:
        db.queue_update(qid, task_id=task_id)
    start()
    return qid
