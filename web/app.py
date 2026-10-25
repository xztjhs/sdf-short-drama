"""SDF 栈 API —— 五段流程 + 后台管理。

流程：
  1. /api/projects                    输入故事
  2. /api/projects/{id}/outline       剧情及大纲编制（LLM）
  3. /api/projects/{id}/lock          锁定基础信息（角色/场景/道具/风格）
  4. /api/episodes/{id}/storyboard    分镜头脚本（LLM）
  5. /api/render                      渲染成片

后台管理（/admin）：
  /api/admin/system     平台状态
  /api/admin/config     配置读写
  /api/admin/models     模型接入与管理
  /api/admin/prompts    提示词管理
  /api/admin/secrets    API KEY
  /api/admin/files      文件与产物
  /api/admin/tasks      任务与日志
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline import __version__, assemble, comfy, config as cfgmod, db, router
from pipeline import queue as rqueue
from pipeline import script as llm
from pipeline import worker
from pipeline.config import cfg

app = FastAPI(title="Short Drama Factory 短剧工厂", version=__version__)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# MCP server（Streamable HTTP，POST /mcp）
# OpenClaw / CherryStudio 等支持 MCP 的客户端都可通过它直接操作短剧工厂
from pipeline.mcp_server import router as mcp_router
app.include_router(mcp_router)

STATIC_DIR = Path(__file__).parent / "static"
DATA_DIR = os.environ.get("SDF_DATA_DIR", "/data")

db.init_db()
cfg.ensure_files()

TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers

def _out_dir() -> Path:
    return Path(cfg.get("settings", "paths.output_dir", "/output") or "/output")


def _proj_out_dir(pid: str) -> Path:
    """项目产物目录：/output/{YYYYMMDD-HHMM_slug}（旧数据回退到项目 id）。"""
    name = db.get_project_dir(pid) or pid
    return _out_dir() / name


def gpu_preflight() -> dict:
    """渲染前预检：显存被占满就不让开始。

    背景：本机 RAGFlow 会自行拉起并吃掉 22GB 显存，
    导致 H3 渲染必然 OOM（而且失败信息很难看懂）。

    ⚠️ 不要试图区分「进程名」：api 容器里看宿主进程是 [Not Found]，
       把 ComfyUI 自己的缓存放判成外人（实测误拦过）。
       改为用绝对阈值 + ComfyUI 自报数据辅助判断。
    """
    out = {"ok": True, "reason": None, "used_mb": None, "total_mb": None,
           "pct": None, "comfy_vram_mb": None, "hint": None}
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return out
        used, total = (int(float(x)) for x in r.stdout.strip().split(",")[:2])
        out["used_mb"], out["total_mb"] = used, total
        out["pct"] = round(used * 100 / total) if total else None
    except Exception:
        return out

    # ComfyUI 自己的视角（它报告的 vram_free ≈ 整卡剩余）
    try:
        st = comfy.system_stats()
        devs = st.get("devices") or []
        if devs:
            d = devs[0]
            vt = d.get("vram_total")
            vf = d.get("vram_free")
            if vt and vf:
                out["comfy_vram_mb"] = round((vt - vf) / 1048576)
    except Exception:
        pass

    # 阈值：85%。
    #   实测参考：ComfyUI 跑完缓存约 16GB / 24.5GB = 66%，放行；
    #             RAGFlow 占用 22GB / 24.5GB = 90%，拦住。
    if out["pct"] is not None and out["pct"] >= 85:
        out["ok"] = False
        out["reason"] = (f"显存已占 {used}/{total} MiB（{out['pct']}%），渲染会 OOM。")
        out["hint"] = "free_comfyui"
        # 先看 ComfyUI 是不是还挂着任务（孤儿任务很常见：API 重启过但 ComfyUI 还在算）
        try:
            q = comfy.queue_status()
            run = len(q.get("queue_running") or [])
            pend = len(q.get("queue_pending") or [])
            if run or pend:
                out["comfy_queue"] = {"running": run, "pending": pend}
                out["reason"] += (f" ComfyUI 还有 {run} 个任务在跑、{pend} 个排队，"
                                  f"若是幽灵任务可用「中断当前任务」清掉。")
            else:
                out["reason"] += " ComfyUI 空闲，可能被其它进程（如 RAGFlow）占用。"
        except Exception:
            pass
    return out


@app.post("/api/admin/gpu/free")
def admin_gpu_free():
    """释放 ComfyUI 显存缓存（卸载模型 + 清碎片）。"""
    comfy.free_vram()
    time.sleep(2)
    return {"ok": True, "after": gpu_preflight()}


def _new_task(kind: str, **meta) -> str:
    tid = uuid.uuid4().hex[:12]
    with TASK_LOCK:
        TASKS[tid] = {"id": tid, "kind": kind, "status": "running",
                      "started": time.time(), "progress": [], **meta}
    # 同时落库，避免 API 重启后任务凭空消失
    try:
        db.create_task(tid, kind, episode_id=meta.get("episode_id"),
                       shot_id=meta.get("shot_id"))
    except Exception:
        pass
    return tid


def _task_update(tid: str, ev: dict) -> None:
    ev = {"t": round(time.time(), 1), **ev}
    with TASK_LOCK:
        t = TASKS.get(tid)
        if t:
            t["progress"].append(ev)
            if len(t["progress"]) > 300:
                t["progress"] = t["progress"][-300:]
    try:
        db.append_task_progress(tid, ev)
    except Exception:
        pass


def _task_finish(tid: str, status: str, **kw) -> None:
    with TASK_LOCK:
        t = TASKS.get(tid)
        if t:
            t["status"] = status
            t["finished"] = time.time()
            t.update(kw)
    try:
        db.finish_task(tid, status, result=kw.get("result"), error=kw.get("error"))
    except Exception:
        pass


def _admin_password() -> str:
    """管理密码：优先 settings.admin.password，兼容旧 secrets.env 的 ADMIN_PASSWORD。"""
    pw = cfg.get("settings", "admin.password", "") or ""
    if pw:
        return str(pw)
    return cfgmod.load_secrets().get("ADMIN_PASSWORD", "")


def _auth(request: Request) -> None:
    """管理接口密码校验。没设密码则放行（内网模式）。"""
    expected = _admin_password()
    if not expected:
        return
    given = request.headers.get("x-admin-password") or \
            request.query_params.get("_p") or ""
    if given != expected:
        raise HTTPException(401, "需要管理密码")


# ---------------------------------------------------------------- models

class ProjectIn(BaseModel):
    title: str
    synopsis: str = ""
    style: str = ""          # 英文风格（进提示词）
    style_zh: str = ""       # 中文风格（给人看）
    aspect: str = "9:16"
    story: str = ""
    slug: str = ""           # 英文名（留空则自动生成）


class StoryIn(BaseModel):
    story: str
    style: str = ""
    episode_count: int = 3


class OutlineIn(BaseModel):
    outline: dict


class LockIn(BaseModel):
    characters: list[dict] = []
    locations: list[dict] = []
    props: list[dict] = []
    visual_style: str = ""        # 英文（进提示词）
    visual_style_zh: str = ""     # 中文（给人看）
    aspect: str = "9:16"
    era: str = ""
    forbidden: list[str] = []


class EpisodeIn(BaseModel):
    seq: int
    title: str = ""
    script: str = ""


class StoryboardIn(BaseModel):
    script: str = ""
    style: str = ""
    max_shots: int = 40
    target_duration: float = 0
    replace: bool = True
    # v0.4：渲染模式 auto|h3|wan22 —— 决定分镜时按哪个模型的格式与要求拆
    engine_mode: str = "auto"


class RenderIn(BaseModel):
    episode_id: str
    quality_first: bool = False
    force_engine: str | None = None
    max_shots: int | None = None
    # v0.4：只渲染排队，不立刻跑（配合「全部剧集批量渲染」）
    enqueue: bool = False
    upscale: str = ""
    # v0.5：分辨率档 normal|quality|draft（决定单镜渲染分辨率与步数）
    tier: str = ""


class ShotPatch(BaseModel):
    prompt: str | None = None
    negative: str | None = None
    dialogue: str | None = None
    speaker: str | None = None
    scene_id: int | None = None
    sfx: str | None = None
    duration: float | None = None
    shot_size: str | None = None
    camera: str | None = None
    resolution: str | None = None
    steps: int | None = None
    seed: int | None = None
    first_frame: str | None = None
    engine: str | None = None


class ConfigWrite(BaseModel):
    section: str
    data: dict


class SecretIn(BaseModel):
    key: str
    value: str


class ModelToggle(BaseModel):
    model_id: str
    enabled: bool


class ModelAdd(BaseModel):
    id: str
    name: str
    engine: str
    kind: str
    file: str
    size_gb: float = 0
    required: bool = False
    enabled: bool = True
    note: str = ""


# ------------------------------------------------------------ health/system

@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__, "comfy": comfy.alive(),
            "time": time.time()}


@app.get("/api/system")
def system():
    info: dict[str, Any] = {"version": __version__}
    info["preflight"] = gpu_preflight()

    gpu = None
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,"
             "utilization.gpu,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            f = [x.strip() for x in r.stdout.strip().split(",")]
            gpu = {"name": f[0], "vram_used_mb": int(float(f[1])),
                   "vram_total_mb": int(float(f[2])),
                   "util_pct": int(float(f[3])) if f[3] not in ("", "[N/A]") else None,
                   "temp_c": int(float(f[4])) if f[4] not in ("", "[N/A]") else None,
                   "power_w": round(float(f[5])) if len(f) > 5 and f[5] not in ("", "[N/A]") else None,
                   "source": "nvidia-smi"}
    except Exception:
        pass

    if gpu is None:
        try:
            st = comfy.system_stats()
            devs = st.get("devices") or []
            if devs:
                d = devs[0]
                gpu = {"name": d.get("name", "GPU"),
                       "vram_used_mb": round((d.get("vram_total", 0) - d.get("vram_free", 0)) / 1048576),
                       "vram_total_mb": round(d.get("vram_total", 0) / 1048576),
                       "util_pct": None, "temp_c": None, "power_w": None,
                       "source": "comfyui"}
        except Exception:
            pass
    info["gpu"] = gpu

    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            mem[k] = int(v.strip().split()[0])
        info["memory"] = {"total_gb": round(mem["MemTotal"] / 1048576, 1),
                          "available_gb": round(mem["MemAvailable"] / 1048576, 1),
                          "swap_gb": round(mem.get("SwapTotal", 0) / 1048576, 1)}
    except Exception:
        info["memory"] = None

    try:
        du = shutil.disk_usage(str(_out_dir()))
        info["disk"] = {"total_gb": round(du.total / 1e9, 1),
                        "free_gb": round(du.free / 1e9, 1)}
    except Exception:
        info["disk"] = None

    # ---- 主机指标（CPU / swap / 磁盘IO / 网络 / CPU温度）----
    info.update(_host_metrics(info.get("memory")))

    info["comfy"] = {"alive": comfy.alive(), "url": comfy.base_url()}
    info["models"] = _model_inventory()
    info["stats"] = db.stats()
    info["llm"] = {"model": llm.model(), "base_url": llm.base_url(),
                   "configured": bool(llm.api_key())}
    info["config_files"] = cfg.file_info()
    info["monitor"] = cfg.get("presets", "monitor", {}) or {}
    return info


# ---------------------------------------------------------- 主机指标采样
_prev_sample: dict[str, Any] = {}


def _read_cpu() -> dict | None:
    """读 /proc/stat 算 CPU 使用率（与上次采样对比）。"""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:11]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        prev = _prev_sample.get("cpu")
        _prev_sample["cpu"] = (idle, total)
        if prev:
            di, dt = idle - prev[0], total - prev[1]
            pct = round((1 - di / dt) * 100) if dt > 0 else None
        else:
            pct = None
        load = [round(x, 2) for x in os.getloadavg()]
        return {"pct": pct, "cores": os.cpu_count(), "load": load}
    except Exception:
        return None


def _read_cpu_temp() -> float | None:
    """CPU 温度：优先 hwmon 的 k10temp/coretemp，退回到 thermal_zone。"""
    try:
        from pathlib import Path as _P
        for hw in _P("/sys/class/hwmon").glob("hwmon*"):
            name = (hw / "name").read_text().strip().lower()
            if name in ("k10temp", "coretemp", "zenpower", "cpu"):
                for t in sorted(hw.glob("temp*_input")):
                    try:
                        v = int(t.read_text().strip()) / 1000
                        if 0 < v < 130:
                            return round(v, 1)
                    except Exception:
                        continue
        for tz in _P("/sys/class/thermal").glob("thermal_zone*"):
            try:
                if "x86" in (tz / "type").read_text().lower():
                    return round(int((tz / "temp").read_text().strip()) / 1000, 1)
            except Exception:
                continue
    except Exception:
        pass
    return None


def _read_swap(memory: dict | None) -> dict | None:
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            mem[k] = int(v.strip().split()[0])
        total = mem.get("SwapTotal", 0)
        free = mem.get("SwapFree", 0)
        used = total - free
        return {"total_gb": round(total / 1048576, 1),
                "used_gb": round(used / 1048576, 1),
                "pct": round(used * 100 / total) if total else 0,
                "enabled": total > 0}
    except Exception:
        return None


def _read_disk_io() -> dict | None:
    """磁盘读写速率（MB/s），与上次采样对比。"""
    try:
        cur: dict[str, tuple[int, int]] = {}
        for line in open("/proc/diskstats"):
            f = line.split()
            if len(f) < 10:
                continue
            name = f[2]
            if not any(name.startswith(x) for x in ("sd", "nvme", "vd")):
                continue
            if any(name.endswith(f"p{i}") for i in range(1, 20)) or name[-1].isdigit():
                continue
            cur[name] = (int(f[5]) * 512, int(f[9]) * 512)   # sectors read/written
        prev = _prev_sample.get("disk_io")
        now_t = time.time()
        prev_t = _prev_sample.get("disk_io_t", now_t)
        _prev_sample["disk_io"] = cur
        _prev_sample["disk_io_t"] = now_t
        if not prev:
            return {"read_mbps": None, "write_mbps": None}
        dt = max(now_t - prev_t, 0.1)
        rd = sum(v[0] for v in cur.values()) - sum(v[0] for v in prev.values())
        wr = sum(v[1] for v in cur.values()) - sum(v[1] for v in prev.values())
        return {"read_mbps": round(max(rd, 0) / dt / 1048576, 2),
                "write_mbps": round(max(wr, 0) / dt / 1048576, 2)}
    except Exception:
        return None


def _read_net() -> dict | None:
    """网络收发速率（Mbps，与上次采样对比）。"""
    try:
        cur: dict[str, tuple[int, int]] = {}
        for line in open("/proc/net/dev"):
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo":
                continue
            f = rest.split()
            cur[iface] = (int(f[0]), int(f[8]))
        prev = _prev_sample.get("net")
        now_t = time.time()
        prev_t = _prev_sample.get("net_t", now_t)
        _prev_sample["net"] = cur
        _prev_sample["net_t"] = now_t
        if not prev:
            return {"rx_mbps": None, "tx_mbps": None, "ifaces": list(cur)}
        dt = max(now_t - prev_t, 0.1)
        rx = sum(v[0] for v in cur.values()) - sum(v[0] for v in prev.values())
        tx = sum(v[1] for v in cur.values()) - sum(v[1] for v in prev.values())
        return {"rx_mbps": round(max(rx, 0) * 8 / dt / 1e6, 2),
                "tx_mbps": round(max(tx, 0) * 8 / dt / 1e6, 2),
                "ifaces": list(cur)}
    except Exception:
        return None


def _host_metrics(memory: dict | None) -> dict:
    return {
        "cpu": _read_cpu(),
        "cpu_temp_c": _read_cpu_temp(),
        "swap": _read_swap(memory),
        "disk_io": _read_disk_io(),
        "net": _read_net(),
    }


def _model_inventory() -> list[dict]:
    models_dir = Path(cfg.get("models", "models_dir", "/models") or "/models")
    out = []
    for m in cfg.get("models", "models", []) or []:
        p = models_dir / m.get("file", "")
        ok = p.exists()
        size = round(p.stat().st_size / 1e9, 2) if ok else 0
        out.append({**m, "present": ok, "actual_gb": size,
                    "complete": ok and abs(size - float(m.get("size_gb") or 0)) < 0.3})
    return out


# ================================================================ 五段流程

# ---- 第1步：输入故事 ----
@app.get("/api/projects")
def api_projects(include_archived: bool = False, q: str = ""):
    items = db.list_projects(include_archived=include_archived)
    if q:
        ql = q.lower()
        items = [p for p in items if ql in (p.get("title") or "").lower()
                 or ql in (p.get("synopsis") or "").lower()]
    return {"projects": items}


@app.post("/api/projects")
def api_create_project(p: ProjectIn):
    pid = db.create_project(p.title, p.synopsis, p.style, p.aspect, p.story,
                            slug=(p.slug or None), style_zh=p.style_zh)
    return db.get_project(pid)


@app.post("/api/translate")
def api_translate(req: dict):
    """中英互译（项目英文名 / 风格 / 角色描述 / 提示词都用它）。

    kind: slug(生成路径安全的英文短名) | style(风格) | text(通用)
    """
    text = (req.get("text") or "").strip()
    kind = req.get("kind") or "text"
    if not text:
        raise HTTPException(400, "text 不能为空")
    try:
        return {"ok": True, "result": llm.translate(text, kind=kind)}
    except llm.LLMError as e:
        raise HTTPException(502, f"翻译失败: {e}")


@app.get("/api/projects/{pid}")
def api_get_project(pid: str):
    prj = db.get_project(pid)
    if not prj:
        raise HTTPException(404, "项目不存在")
    prj["episodes"] = db.list_episodes(pid)
    prj["characters"] = db.list_characters(pid)
    prj["locations"] = db.list_locations(pid)
    prj["props"] = db.list_props(pid)
    prj["stats"] = db.stats(pid)
    prj["stage"] = prj.get("stage") or "story"
    return prj


@app.patch("/api/projects/{pid}")
def api_update_project(pid: str, p: dict):
    """更新项目。

    改 title 时：如果前端没显式给 slug，自动重新生成英文名，
    并同步重命名产物目录（/output/{时间}_{slug} → 新 slug，时间部分不变）。
    """
    prj = db.get_project(pid)
    if not prj:
        raise HTTPException(404, "项目不存在")

    fields = dict(p)
    new_title = fields.get("title")

    # 英文名：显式给了就用，否则标题变了就重算
    slug = (fields.pop("slug", None) or "").strip()
    need_slug = bool(new_title and new_title != prj.get("title"))
    if need_slug and not slug:
        try:
            slug = llm.translate(new_title, kind="slug")
        except Exception:
            slug = db._fallback_slug(new_title)
    if slug or need_slug:
        fields["slug"] = slug or db._fallback_slug(new_title or prj["title"])

    # 目录重命名：保留原时间戳前缀，只换 slug 部分
    final_slug = fields.get("slug")
    if final_slug and final_slug != prj.get("slug"):
        old_dir = prj.get("out_dir") or prj["id"]
        stamp = old_dir.split("_", 1)[0] if "_" in old_dir else None
        if stamp and len(stamp) == 13 and "-" in stamp:
            new_dir = f"{stamp}_{final_slug}"
        else:
            new_dir = db.make_out_dir_name(final_slug, prj.get("created_at"))
        src = _out_dir() / old_dir
        dst = _out_dir() / new_dir
        if src.exists() and src != dst:
            try:
                if dst.exists():
                    raise HTTPException(
                        409, f"目标目录已存在：{new_dir}，请换个英文名")
                src.rename(dst)
                fields["out_dir"] = new_dir
            except OSError as e:
                raise HTTPException(500, f"目录重命名失败: {e}")
        elif not src.exists():
            fields["out_dir"] = new_dir

    db.update_project(pid, **fields)
    return db.get_project(pid)


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str, purge_files: bool = True):
    """删项目。purge_files=true 时连产物目录一起删。"""
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    removed = 0
    if purge_files:
        p = _proj_out_dir(pid)
        if p.exists():
            removed = sum(1 for _ in p.rglob("*") if _.is_file())
            shutil.rmtree(p, ignore_errors=True)
    db.delete_project(pid)
    return {"ok": True, "purged_files": removed}


@app.post("/api/projects/{pid}/archive")
def api_archive_project(pid: str, body: dict | None = None):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    archived = bool((body or {}).get("archived", True))
    db.set_archived(pid, archived)
    return {"ok": True, "archived": archived}


@app.post("/api/projects/{pid}/duplicate")
def api_duplicate_project(pid: str, body: dict | None = None):
    """复制项目（带角色/剧集/镜头，不带产物，镜头重置为 pending）。"""
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    nid = db.duplicate_project(pid, (body or {}).get("title"))
    if not nid:
        raise HTTPException(500, "复制失败")
    return db.get_project(nid)


@app.post("/api/projects/batch")
def api_projects_batch(req: dict):
    """批量操作: action=delete|archive|unarchive, ids=[...]"""
    action = req.get("action")
    ids = req.get("ids") or []
    if not ids:
        raise HTTPException(400, "ids 不能为空")
    done = 0
    for pid in ids:
        if not db.get_project(pid):
            continue
        if action == "delete":
            p = _proj_out_dir(pid)
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
            db.delete_project(pid)
        elif action == "archive":
            db.set_archived(pid, True)
        elif action == "unarchive":
            db.set_archived(pid, False)
        else:
            raise HTTPException(400, f"未知 action: {action}")
        done += 1
    return {"ok": True, "affected": done}


# ---- 第2步：剧情及大纲编制 ----
@app.post("/api/projects/{pid}/outline")
def api_outline(pid: str, req: StoryIn):
    prj = db.get_project(pid)
    if not prj:
        raise HTTPException(404, "项目不存在")
    db.update_project(pid, story=req.story, stage="outline")
    try:
        style_in = llm.pick_style(prj, req.style)
        data = llm.outline(req.story,
                           episode_count=max(1, req.episode_count),
                           style=style_in)
    except llm.LLMError as e:
        raise HTTPException(502, f"大纲生成失败: {e}")

    # v0.5：中文模式下把中文风格写进 style（渲染直接用），
    # 同时保留 style_zh 便于旧界面显示
    if llm.prompt_language() == "zh":
        db.update_project(pid, outline=data, style=style_in,
                          style_zh=style_in, synopsis=data.get("logline") or "")
    else:
        db.update_project(pid, outline=data,
                          style=req.style or prj.get("style") or "",
                          synopsis=data.get("logline") or "")

    # 大纲里的剧集直接落库（已存在则更新）
    existing = {e["seq"]: e for e in db.list_episodes(pid)}
    for ep in data.get("episodes", []):
        seq = int(ep.get("seq") or 0)
        if not seq:
            continue
        if seq in existing:
            db.update_episode(existing[seq]["id"],
                              title=ep.get("title", ""),
                              hook=ep.get("hook", ""),
                              conflict=ep.get("conflict", ""),
                              summary=ep.get("summary", ""),
                              cliffhanger=ep.get("cliffhanger", ""))
        else:
            db.create_episode(pid, seq, ep.get("title", ""),
                              hook=ep.get("hook", ""),
                              conflict=ep.get("conflict", ""),
                              summary=ep.get("summary", ""),
                              cliffhanger=ep.get("cliffhanger", ""))
    return {"ok": True, "outline": data, "project": db.get_project(pid)}


@app.put("/api/projects/{pid}/outline")
def api_save_outline(pid: str, req: OutlineIn):
    """手工修正大纲后保存。"""
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    db.update_project(pid, outline=req.outline)
    existing = {e["seq"]: e for e in db.list_episodes(pid)}
    for ep in req.outline.get("episodes", []):
        seq = int(ep.get("seq") or 0)
        if not seq:
            continue
        if seq in existing:
            db.update_episode(existing[seq]["id"], title=ep.get("title", ""),
                              hook=ep.get("hook", ""), conflict=ep.get("conflict", ""),
                              summary=ep.get("summary", ""),
                              cliffhanger=ep.get("cliffhanger", ""))
        else:
            db.create_episode(pid, seq, ep.get("title", ""), hook=ep.get("hook", ""),
                              conflict=ep.get("conflict", ""),
                              summary=ep.get("summary", ""),
                              cliffhanger=ep.get("cliffhanger", ""))
    return {"ok": True, "project": db.get_project(pid)}


# ---- 第3步：锁定基础信息 ----
@app.post("/api/projects/{pid}/extract-baseinfo")
def api_extract_baseinfo(pid: str, req: dict):
    """LLM 从剧本抽取角色/场景/道具/风格，供人工确认后锁定。

    2026-10-24 修：项目已锁定过就直接返回 locked_info，不再调 LLM。
    原因：LLM 抽取实测 ~6.7s，而锁定页每次进入都会调用 → 页面长时间停在
    「加载中…」，已锁定的项目反复花 LLM 费用重算同样的东西。
    要强制重算传 {"force": true}（界面上「重新抽取」用）。
    """
    force = bool(req.get("force"))
    if not force:
        prj = db.get_project(pid)
        locked = (prj or {}).get("locked_info") or {}
        # 只有「锁定时真的存了完整基准」才直接复用，避免旧数据缺字段
        if locked and locked.get("visual_style") is not None and locked.get("characters"):
            return {"ok": True, "baseinfo": {
                "characters": locked.get("characters") or [],
                "locations": locked.get("locations") or [],
                "props": locked.get("props") or [],
                "visual_style": locked.get("visual_style") or "",
                "aspect": locked.get("aspect") or (prj or {}).get("aspect") or "9:16",
                "era": locked.get("era") or "",
                "forbidden": locked.get("forbidden") or [],
            }, "cached": True}
        # 旧项目（v0.2 时代）没有 locked_info，但角色/风格已经在库里：
        # 直接组装，别再花 7-11s 让 LLM 重算一遍同样的东西。
        if prj and not locked:
            chars = db.list_characters(pid)     # project 表没有 characters 列，关系表里取
            style = prj.get("style") or prj.get("style_zh") or ""
            if chars and style:
                return {"ok": True, "baseinfo": {
                    "characters": chars,
                    "locations": prj.get("locations") or [],
                    "props": prj.get("props") or [],
                    "visual_style": style,
                    "aspect": prj.get("aspect") or "9:16",
                    "era": "",
                    "forbidden": [],
                }, "cached": True, "source": "project"}

    script_text = (req.get("script") or "").strip()
    if not script_text:
        prj = db.get_project(pid)
        script_text = (prj or {}).get("story") or ""
    if not script_text:
        raise HTTPException(400, "没有可分析的剧本内容")
    try:
        data = llm.baseinfo(script_text)
    except llm.LLMError as e:
        raise HTTPException(502, f"基础信息抽取失败: {e}")
    return {"ok": True, "baseinfo": data, "cached": False}


@app.post("/api/projects/{pid}/lock")
def api_lock(pid: str, req: LockIn):
    """锁定：写入角色/场景/道具，标记 locked，进入分镜阶段。"""
    prj = db.get_project(pid)
    if not prj:
        raise HTTPException(404, "项目不存在")

    db.replace_characters(pid, req.characters, locked=1)
    db.replace_locations(pid, req.locations)
    db.replace_props(pid, req.props)

    locked = {
        "characters": req.characters,
        "locations": req.locations,
        "props": req.props,
        "visual_style": req.visual_style,
        "visual_style_zh": req.visual_style_zh,
        "aspect": req.aspect,
        "era": req.era,
        "forbidden": req.forbidden,
        "locked_at": time.time(),
    }
    db.update_project(pid, locked_info=locked, stage="locked",
                      style=req.visual_style or prj.get("style") or "",
                      style_zh=req.visual_style_zh or prj.get("style_zh") or "",
                      aspect=req.aspect or prj.get("aspect") or "9:16")
    return {"ok": True, "locked_info": locked,
            "characters": db.list_characters(pid)}


@app.get("/api/projects/{pid}/locked")
def api_get_locked(pid: str):
    prj = db.get_project(pid)
    if not prj:
        raise HTTPException(404, "项目不存在")
    return {"locked_info": prj.get("locked_info"),
            "characters": db.list_characters(pid),
            "locations": db.list_locations(pid),
            "props": db.list_props(pid),
            "stage": prj.get("stage")}


@app.post("/api/projects/{pid}/characters")
def api_add_character(pid: str, c: dict):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    cid = db.add_character(pid, c.get("name", ""), c.get("appearance", ""),
                           c.get("ref_image", ""), c.get("voice_note", ""),
                           locked=int(c.get("locked", 0)))
    return {"id": cid}


@app.patch("/api/characters/{cid}")
def api_patch_character(cid: str, c: dict):
    db.update_character(cid, **{k: v for k, v in c.items() if v is not None})
    return {"ok": True}


# ---- 全局角色库（跨项目复用） ----
@app.get("/api/character-lib")
def api_lib_list(q: str = ""):
    items = db.lib_list(q)
    # 附带「被哪些项目使用」，角色库页面要用
    usage: dict[str, list[str]] = {}
    try:
        for prj in db.list_projects(include_archived=True):
            for c in db.list_characters(prj["id"]):
                usage.setdefault(c["name"], []).append(prj["title"] or prj["id"])
    except Exception:
        pass
    for it in items:
        it["used_by"] = sorted(set(usage.get(it.get("name") or "", [])))
    return {"characters": items, "count": len(items)}


@app.post("/api/character-lib")
def api_lib_add(c: dict):
    cid = db.lib_add(c.get("name", ""), c.get("name_en", ""),
                     c.get("appearance", ""), c.get("appearance_zh", ""),
                     c.get("ref_image", ""), c.get("voice_note", ""),
                     c.get("tags", ""))
    return db.lib_get(cid)


@app.patch("/api/character-lib/{cid}")
def api_lib_update(cid: str, c: dict):
    if not db.lib_get(cid):
        raise HTTPException(404, "角色不存在")
    db.lib_update(cid, **{k: v for k, v in c.items() if v is not None})
    return db.lib_get(cid)


@app.delete("/api/character-lib/{cid}")
def api_lib_delete(cid: str):
    db.lib_delete(cid)
    return {"ok": True}


@app.post("/api/projects/{pid}/import-characters")
def api_import_chars(pid: str, req: dict):
    """从全局库导入角色到项目。"""
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    ids = req.get("lib_ids") or []
    n = db.lib_import_to_project(pid, ids)
    return {"ok": True, "imported": n, "characters": db.list_characters(pid)}


@app.post("/api/projects/{pid}/save-to-lib")
async def api_save_to_lib(pid: str, request: Request):
    """把项目角色存进全局库。

    body 可选：{"character_ids": [...]} 指定部分，不传则全部存。
    """
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    ids = None
    try:
        raw = await request.body()
        if raw:
            body = json.loads(raw)
            ids = body.get("character_ids") or None
    except Exception:
        ids = None
    n = db.lib_from_project(pid, ids)
    return {"ok": True, "saved": n}


@app.post("/api/upload")
async def api_upload(request: Request, kind: str = "ref"):
    """上传参考图/首帧。返回可访问的相对路径。

    存储位置：/input/uploads/，文件名用时间戳避重名。
    """
    import time as _t
    from fastapi import UploadFile  # noqa: F401  (仅类型提示用)

    form = await request.form()
    files = [v for k, v in form.items() if hasattr(v, "read")]
    if not files:
        raise HTTPException(400, "没有文件")

    updir = Path(cfg.get("settings", "paths.input_dir", "/input") or "/input") / "uploads"
    updir.mkdir(parents=True, exist_ok=True)

    out = []
    for f in files:
        raw = await f.read()
        if not raw:
            continue
        ext = Path(getattr(f, "filename", "x.png")).suffix.lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov"):
            raise HTTPException(400, f"不支持的文件类型: {ext}")
        name = f"{kind}_{int(_t.time()*1000)}{ext}"
        (updir / name).write_bytes(raw)
        out.append({"name": name, "path": f"/input/uploads/{name}",
                    "size": len(raw)})
    return {"ok": True, "files": out}


@app.get("/api/uploads/{name}")
def api_get_upload(name: str):
    """读上传的图/视频（参考图预览用）。"""
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    updir = Path(cfg.get("settings", "paths.input_dir", "/input") or "/input") / "uploads"
    p = updir / name
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, filename=p.name)


@app.delete("/api/characters/{cid}")
def api_delete_character(cid: str):
    db.delete_character(cid)
    return {"ok": True}


# ---- 第4步：分镜头脚本 ----
@app.post("/api/projects/{pid}/episodes")
def api_create_episode(pid: str, e: EpisodeIn):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    eid = db.create_episode(pid, e.seq, e.title, e.script)
    return db.get_episode(eid)


@app.get("/api/episodes/{eid}")
def api_get_episode(eid: str):
    ep = db.get_episode(eid)
    if not ep:
        raise HTTPException(404, "剧集不存在")
    ep["shots"] = db.list_shots(eid)
    mode = (ep.get("engine_mode") or "auto").strip().lower()
    forced = mode if mode in ("h3", "wan22") else None
    ep["plan"], ep["summary"] = router.plan_episode(ep["shots"],
                                                    forced_engine=forced)
    ep["engine_mode"] = mode
    try:
        ep["artifacts"] = _decorate_artifacts(db.list_artifacts(eid))
    except Exception:
        ep["artifacts"] = []
    try:
        q = [x for x in db.queue_list(limit=200) if x.get("episode_id") == eid]
        ep["queue"] = q[:3]
    except Exception:
        ep["queue"] = []
    return ep


@app.patch("/api/episodes/{eid}")
def api_patch_episode(eid: str, e: dict):
    if not db.get_episode(eid):
        raise HTTPException(404, "剧集不存在")
    db.update_episode(eid, **{k: v for k, v in e.items() if v is not None})
    return db.get_episode(eid)


@app.delete("/api/episodes/{eid}")
def api_delete_episode(eid: str):
    if not db.get_episode(eid):
        raise HTTPException(404, "剧集不存在")
    db.delete_episode(eid)
    return {"ok": True}


@app.post("/api/episodes/{eid}/duplicate")
def api_duplicate_episode(eid: str, body: dict | None = None):
    if not db.get_episode(eid):
        raise HTTPException(404, "剧集不存在")
    neid = db.duplicate_episode(eid, (body or {}).get("seq"))
    if not neid:
        raise HTTPException(500, "复制失败")
    return db.get_episode(neid)


@app.post("/api/episodes/{eid}/storyboard")
def api_storyboard(eid: str, req: StoryboardIn):
    ep = db.get_episode(eid)
    if not ep:
        raise HTTPException(404, "剧集不存在")
    prj = db.get_project(ep["project_id"]) or {}

    script_text = req.script or ep.get("script") or ""
    if not script_text:
        parts = [ep.get("summary") or "", ep.get("hook") or "",
                 ep.get("conflict") or ""]
        script_text = "\n".join(x for x in parts if x)
    if not script_text:
        raise HTTPException(400, "该集没有剧本内容，请先填写或从大纲生成")

    locked_info = _locked_info_text(prj)

    # 渲染模式：优先用本次请求，其次该集已存的，最后 auto
    # v0.5：只有 H3
    mode = "h3"

    try:
        data = llm.storyboard(script_text,
                              style=llm.pick_style(prj, req.style),
                              max_shots=max(1, req.max_shots),
                              target_duration=req.target_duration,
                              locked_info=locked_info,
                              engine_mode=mode)
    except llm.LLMError as e:
        raise HTTPException(502, f"分镜生成失败: {e}")

    # 角色：已锁定的不动，只补新的
    existing = {c["name"] for c in db.list_characters(ep["project_id"])}
    for ch in data.get("characters", []):
        name = (ch.get("name") or "").strip()
        if name and name not in existing:
            db.add_character(ep["project_id"], name, ch.get("appearance", ""),
                             "", ch.get("voice_note", ""), locked=0)
            existing.add(name)

    removed = 0
    if req.replace:
        # 只删「没渲成」的镜头。原来是把 pending 标成 skipped 留在库里，
        # 于是每重跑一次分镜就多一份，E02 因此变成 78 镜（应为 40）。
        removed = db.delete_unrendered_shots(eid)

    for s in data["shots"]:
        db.add_shot(eid, s["seq"], s["prompt"],
                    duration=s["duration"], shot_size=s["shot_size"],
                    camera=s["camera"], negative=s["negative"],
                    dialogue=s["dialogue"], speaker=s.get("speaker"),
                    scene_id=s.get("scene_id"), sfx=s["sfx"],
                    characters=s["characters"],
                    engine=s.get("engine"))

    db.set_episode_scenes(eid, data.get("scenes") or [])
    db.update_episode(eid, script=script_text, engine_mode=mode)
    db.set_episode_status(eid, "storyboard")
    db.set_stage(ep["project_id"], "storyboard")

    shots = db.list_shots(eid)
    plan, summary = router.plan_episode(shots)
    return {"ok": True, "title": data.get("title"), "summary": data.get("summary"),
            "engine_mode": mode, "shots": shots, "plan_summary": summary,
            "removed": removed}


@app.get("/api/episodes/{eid}/plan")
def api_plan(eid: str, quality_first: bool = False):
    shots = db.list_shots(eid)
    plan, summary = router.plan_episode(shots, quality_first=quality_first)
    return {"plan": plan, "summary": summary}


@app.post("/api/projects/{pid}/storyboard-all")
def api_storyboard_all(pid: str, req: dict | None = None, bg: BackgroundTasks = None):
    """批量分镜：给项目里所有「无镜头」的集按同一模式生成分镜。

    后台跑（每集一次 LLM 调用，几十秒），进度写入 task.progress。
    跑完可直接调 /api/queue/all 把全部集丢进渲染队列。
    """
    prj = db.get_project(pid)
    if not prj:
        raise HTTPException(404, "项目不存在")
    b = req or {}
    mode = "h3"   # v0.5 只有 H3
    max_shots = int(b.get("max_shots") or 30)
    only_empty = bool(b.get("only_empty", True))
    target_dur = float(b.get("target_duration") or 0)

    eps = db.list_episodes(pid)
    todo = [e for e in eps if (not only_empty) or (e.get("shot_total") or 0) == 0]
    if not todo:
        return {"ok": True, "count": 0, "note": "没有待生成的集"}

    tid = _new_task("storyboard-all", project_id=pid)

    def _run():
        ok = fail = 0
        for i, ep in enumerate(todo, 1):
            _task_update(tid, {"phase": "storyboard", "index": i,
                               "total": len(todo), "seq": ep["seq"],
                               "title": ep.get("title") or "", "status": "start"})
            try:
                script_text = ep.get("script") or "\n".join(
                    x for x in (ep.get("summary") or "", ep.get("hook") or "",
                                ep.get("conflict") or "") if x)
                if not script_text:
                    raise llm.LLMError("该集没有剧本/简介内容")
                data = llm.storyboard(
                    script_text,
                    style=llm.pick_style(prj),
                    max_shots=max_shots,
                    target_duration=target_dur,
                    locked_info=_locked_info_text(prj),
                    engine_mode=mode)
                # 同单个分镜接口：重跑前先清掉没渲成的旧镜头，否则叠加
                db.delete_unrendered_shots(ep["id"])
                for s in data["shots"]:
                    db.add_shot(ep["id"], s["seq"], s["prompt"],
                                duration=s["duration"],
                                shot_size=s["shot_size"], camera=s["camera"],
                                negative=s["negative"], dialogue=s["dialogue"],
                                speaker=s.get("speaker"),
                                scene_id=s.get("scene_id"),
                                sfx=s["sfx"], characters=s["characters"],
                                engine=s.get("engine"))
                db.set_episode_scenes(ep["id"], data.get("scenes") or [])
                db.update_episode(ep["id"], engine_mode=mode)
                db.set_episode_status(ep["id"], "storyboard")
                ok += 1
                _task_update(tid, {"phase": "storyboard", "index": i,
                                   "total": len(todo), "seq": ep["seq"],
                                   "status": "done",
                                   "shots": len(data["shots"])})
            except Exception as e:
                fail += 1
                _task_update(tid, {"phase": "storyboard", "index": i,
                                   "total": len(todo), "seq": ep["seq"],
                                   "status": "failed",
                                   "error": f"{type(e).__name__}: {e}"[:200]})
        db.set_stage(pid, "storyboard")
        _task_finish(tid, "done" if not fail else "done",
                     result={"ok": ok, "failed": fail, "total": len(todo)})

    bg.add_task(_run)
    return {"ok": True, "task_id": tid, "count": len(todo)}


def _locked_info_text(prj: dict) -> str:
    """把锁定信息压成给 LLM 的文本（分镜与批量分镜共用）。"""
    locked = prj.get("locked_info") or {}
    if not locked:
        return ""
    lines = []
    if locked.get("characters"):
        lines.append("角色（必须沿用下列外貌锚点，不要改写）：")
        for c in locked["characters"]:
            zh = c.get("appearance_zh") or ""
            app = c.get("appearance") or zh
            lines.append(f"  - {c.get('name')}: {app}")
    if locked.get("locations"):
        lines.append("场景：" + "、".join(x.get("name", "")
                                        for x in locked["locations"]))
    if locked.get("visual_style"):
        lines.append(f"视觉风格：{locked['visual_style']}")
    if locked.get("era"):
        lines.append(f"时代：{locked['era']}")
    if locked.get("forbidden"):
        lines.append("禁止出现：" + "、".join(locked["forbidden"]))
    lines.append("【对白规则】dialogue 必须逐字使用剧本中文原文，不得改写。")
    return "\n".join(lines)


# ---- 第5步：渲染 ----
@app.post("/api/render")
def api_render(req: RenderIn, bg: BackgroundTasks):
    ep = db.get_episode(req.episode_id)
    if not ep:
        raise HTTPException(404, "剧集不存在")
    if not comfy.alive():
        raise HTTPException(503, "ComfyUI 未就绪")
    # 显存预检：外部进程（如 RAGFlow 自启）占着显存时直接拒绝，
    # 不然 H3 会在跑到一半时 OOM，错误信息还很难懂。
    pre = gpu_preflight()
    if not pre["ok"]:
        raise HTTPException(409, pre["reason"])

    if req.enqueue:
        # 只入队：交给后台消费者串行跑，人可以去做其它集的分镜
        tid = _new_task("render-queue", episode_id=req.episode_id)
        qid = rqueue.enqueue(req.episode_id,
                             quality_first=req.quality_first,
                             force_engine=req.force_engine,
                             max_shots=req.max_shots,
                             upscale=req.upscale,
                             tier=req.tier,
                             task_id=tid)
        return {"ok": True, "queued": True, "queue_id": qid, "task_id": tid}

    tid = _new_task("render", episode_id=req.episode_id)
    bg.add_task(_run_render, tid, req)
    return {"task_id": tid}


def _run_render(tid: str, req: RenderIn) -> None:
    try:
        res = worker.run_episode(
            req.episode_id, quality_first=req.quality_first,
            force_engine=req.force_engine, max_shots=req.max_shots,
            upscale=req.upscale, tier=req.tier,
            progress=lambda ev: _task_update(tid, ev))
        _task_finish(tid, "done" if res.get("ok") else "failed", result=res)
    except Exception as e:
        _task_finish(tid, "failed", error=f"{type(e).__name__}: {e}")


@app.get("/api/tasks")
def api_tasks():
    """任务列表：内存 + 数据库合并（重启后仍能看到历史）。"""
    with TASK_LOCK:
        mem = sorted(TASKS.values(), key=lambda x: -x["started"])
    try:
        disk = db.list_tasks(30)
    except Exception:
        disk = []
    seen = {t["id"] for t in mem}
    merged = mem + [t for t in disk if t["id"] not in seen]
    merged.sort(key=lambda x: -(x.get("started") or 0))
    return {"tasks": merged[:30]}


@app.get("/api/tasks/{tid}")
def api_task(tid: str):
    with TASK_LOCK:
        t = TASKS.get(tid)
    if not t:
        t = db.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.patch("/api/shots/{sid}")
def api_patch_shot(sid: str, p: ShotPatch):
    if not db.get_shot(sid):
        raise HTTPException(404, "镜头不存在")
    db.update_shot(sid, **{k: v for k, v in p.model_dump().items() if v is not None})
    return db.get_shot(sid)


@app.delete("/api/shots/{sid}")
def api_delete_shot(sid: str):
    db.delete_shot_and_compact(sid)
    return {"ok": True}


@app.post("/api/episodes/{eid}/shots")
def api_add_shot(eid: str, body: dict | None = None):
    """手工新增一个镜头（排到末尾）。"""
    if not db.get_episode(eid):
        raise HTTPException(404, "剧集不存在")
    b = body or {}
    sid = db.add_shot_manual(
        eid,
        prompt=b.get("prompt") or "",
        duration=float(b.get("duration") or 5),
        shot_size=b.get("shot_size") or "",
        camera=b.get("camera") or "",
        dialogue=b.get("dialogue"),
        sfx=b.get("sfx"),
        negative=b.get("negative"),
        characters=b.get("characters") or [],
    )
    return db.get_shot(sid)


@app.post("/api/shots/{sid}/move")
def api_move_shot(sid: str, req: dict):
    """上移/下移一个镜头（与相邻镜头交换 seq）。"""
    shot = db.get_shot(sid)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    direction = req.get("direction")
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction 必须是 up 或 down")

    shots = db.list_shots(shot["episode_id"])
    idx = next((i for i, s in enumerate(shots) if s["id"] == sid), -1)
    if idx < 0:
        raise HTTPException(404, "镜头不在该集")
    swap = idx - 1 if direction == "up" else idx + 1
    if swap < 0 or swap >= len(shots):
        return {"ok": True, "moved": False, "reason": "已在边界"}

    a, b = shots[idx], shots[swap]
    db.update_shot(a["id"], seq=b["seq"])
    db.update_shot(b["id"], seq=a["seq"])
    return {"ok": True, "moved": True}


@app.post("/api/episodes/{eid}/shots/reset")
def api_reset_shots(eid: str, body: dict | None = None):
    """批量重置镜头状态为 pending（重渲全部/仅失败）。"""
    if not db.get_episode(eid):
        raise HTTPException(404, "剧集不存在")
    only = (body or {}).get("only", "failed")   # failed | all
    n = 0
    for s in db.list_shots(eid):
        if only == "all" or s["status"] in ("failed", "skipped"):
            db.reset_shot_for_rerender(s["id"])
            n += 1
    return {"ok": True, "reset": n}


# ---- v0.6 定妆照（Ref2VA 的前置素材）----
@app.post("/api/projects/{pid}/portraits")
def api_make_portraits(pid: str, bg: BackgroundTasks, req: dict | None = None):
    """给项目角色生成/抽取定妆照，写回 character.ref_image。

    B 方案（Ref2VA）必须有参考图才能锁住脸。E02 的根因之一就是四个角色
    一张图都没有。这里提供两种来源：

      source="shots"    从已渲染镜头里抽一帧（秒级，免费，与已出片一致）
      source="generate" 用 H3 文生视频生成一段定妆镜头，再抽首帧（约 13 分钟/人）

    入参：{"source": "shots", "character_ids": [...]}（不传=本项目全部角色）
    """
    prj = db.get_project(pid)
    if not prj:
        raise HTTPException(404, "项目不存在")
    b = req or {}
    source = (b.get("source") or "shots").strip().lower()
    if source not in ("shots", "generate"):
        raise HTTPException(400, "source 只能是 shots 或 generate")

    chars = db.list_characters(pid)
    wanted = b.get("character_ids") or []
    if wanted:
        chars = [c for c in chars if c["id"] in wanted]
    if not chars:
        raise HTTPException(400, "没有可处理的角色")

    tid = _new_task("portrait", project_id=pid)

    def _run():
        try:
            eps = db.list_episodes(pid)
            all_shots = []
            for ep in eps:
                all_shots.extend(db.list_shots(ep["id"]))
            done = [s for s in all_shots if s["status"] == "done" and s.get("output_path")]
            ok = fail = 0
            for idx, c in enumerate(chars, 1):
                name = c["name"]
                _task_update(tid, {"phase": "portrait", "index": idx,
                                   "total": len(chars), "character": name,
                                   "status": "start", "source": source})
                try:
                    dst = Path("/input/uploads") / f"portrait_{c['id']}.png"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if source == "shots":
                        cand = _pick_portrait_shot(done, name)
                        if not cand:
                            raise RuntimeError("该角色没有可用的已渲染镜头")
                        _frame_for_portrait(cand["output_path"], str(dst),
                                            prefer=cand.get("shot_size") or "")
                    else:
                        _generate_portrait(c, prj, str(dst), progress=lambda ev: _task_update(tid, ev))
                    ref = f"/input/uploads/{dst.name}"
                    db.update_character(c["id"], ref_image=ref)
                    ok += 1
                    _task_update(tid, {"phase": "portrait", "index": idx,
                                       "total": len(chars), "character": name,
                                       "status": "done", "ref_image": ref})
                except Exception as e:
                    fail += 1
                    _task_update(tid, {"phase": "portrait", "index": idx,
                                       "total": len(chars), "character": name,
                                       "status": "failed",
                                       "error": f"{type(e).__name__}: {e}"[:200]})
            _task_finish(tid, "done", result={"ok": ok, "failed": fail,
                                              "source": source})
        except Exception as e:
            _task_finish(tid, "failed", error=f"{type(e).__name__}: {e}")

    bg.add_task(_run)
    return {"task_id": tid, "characters": len(chars), "source": source}


def _pick_portrait_shot(shots: list[dict], name: str) -> dict | None:
    """挑一个最适合出定妆照的镜头：该角色在镜头里、景别越近越好。"""
    rank = {"特写": 0, "近景": 1, "中景": 2, "全景": 3, "远景": 4}
    cands = []
    for s in shots:
        names = s.get("characters") or []
        if name not in names:
            continue
        # 单人镜头优先（多人同框抽出来可能是别人）
        solo = 0 if len(names) == 1 else 1
        cands.append((solo, rank.get((s.get("shot_size") or "").strip(), 9),
                      -(s.get("seq") or 0), s))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1], x[2]))
    return cands[0][3]


def _frame_for_portrait(video: str, dst: str, prefer: str = "") -> None:
    """从视频里抽一帧当定妆照。

    取整段 40% 位置（避开首帧的黑场和尾帧的过渡），中近景更容易抓到正脸。
    """
    from pipeline import assemble as asm
    try:
        dur = asm.info(video)["duration"]
    except Exception:
        dur = 0
    seek = max(0.0, dur * 0.4) if dur else 0.0
    import subprocess
    cmd = [asm.ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{seek:.3f}",
           "-i", video, "-frames:v", "1", "-q:v", "2", dst]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError(f"抽帧失败: {r.stderr[-300:]}")


def _generate_portrait(char: dict, prj: dict, dst: str, progress=None) -> None:
    """用 H3 生成一段定妆镜头，抽首帧当定妆照（约 13 分钟）。"""
    from pipeline import comfy as _comfy
    from pipeline import workflow as _wf

    app_anchor = (char.get("appearance") or char.get("appearance_zh") or "").strip()
    style = ""
    try:
        style = llm.pick_style(prj)
    except Exception:
        style = prj.get("style") or ""
    # ⚠️ 角色锚点本身常以名字开头（如「示例人物B，45岁中国男性…」），
    #    模板再拼一次 name 就会出现「示例人物B，示例人物B，45岁…」（2026-10-23 实测）。
    #    这里剥掉锚点开头的名字，只保留特征描述。
    name = (char.get("name") or "").strip()
    anchor_body = app_anchor
    for _sep in ("，", "、", ",", "：", ":", " "):
        pref = name + _sep
        if anchor_body.startswith(pref):
            anchor_body = anchor_body[len(pref):].lstrip()
            break
    prompt = (f"人物定妆照镜头：{name}，{anchor_body}。"
              f"正面半身，面部清晰，光线均匀打亮五官，背景简洁虚化，"
              f"镜头缓慢推近，人物基本不动，眼神看向镜头。{style}").strip()
    if progress:
        progress({"phase": "portrait", "character": char.get("name"),
                  "status": "render", "prompt": prompt[:120]})
    wf = _wf.build("h3", prompt=prompt, resolution="720x1280", duration=5,
                   steps=int(cfg.get("engines", "engines.h3.steps", 8) or 8),
                   seed=0, filename_prefix=f"portrait_{char['id']}")
    pid_ = _comfy.submit(wf)
    timeout = int(cfg.get("settings", "comfy.wait_timeout_sec", 7200) or 7200)
    h = _comfy.wait(pid_, timeout=timeout,
                    poll=float(cfg.get("settings", "comfy.poll_interval_sec", 3) or 3))
    fn = None
    for kind in ("images", "gifs", "videos"):
        for item in _comfy.outputs_of(h, kind):
            if item.get("filename"):
                sub = item.get("subfolder") or ""
                fn = f"{sub}/{item['filename']}" if sub else item["filename"]
                break
        if fn:
            break
    if not fn:
        raise RuntimeError("定妆照渲染无输出")
    tmp = str(Path("/output") / "_portraits" / Path(fn).name)
    Path(tmp).parent.mkdir(parents=True, exist_ok=True)
    src = Path(cfg.get("settings", "paths.output_dir", "/output") or "/output") / fn
    if src.exists():
        import shutil as _sh
        _sh.copy2(src, tmp)
    else:
        import urllib.parse
        import urllib.request
        parts = fn.rsplit("/", 1)
        sub, f2 = (parts[0], parts[1]) if len(parts) == 2 else ("", fn)
        url = (f"{_comfy.base_url()}/view?filename={urllib.parse.quote(f2)}"
               f"&type=output&subfolder={urllib.parse.quote(sub)}")
        urllib.request.urlretrieve(url, tmp)
    _frame_for_portrait(tmp, dst)


@app.post("/api/episodes/{eid}/retrofit")
def api_retrofit_episode(eid: str, bg: BackgroundTasks):
    """给旧分镜补全场次 / 说话人 / 场次卡（v0.6 方案 A 的存量迁移）。

    v0.4 之前生成的分镜没有这些字段，A 方案（场次卡 + 说话人点名）不会生效。
    只补结构，不改画面提示词、不改台词、不重排镜头、不动已渲染的产物。
    """
    ep = db.get_episode(eid)
    if not ep:
        raise HTTPException(404, "剧集不存在")
    shots = db.list_shots(eid)
    if not shots:
        raise HTTPException(400, "该集没有镜头")

    tid = _new_task("retrofit", episode_id=eid)

    def _run():
        try:
            _task_update(tid, {"phase": "retrofit", "total": len(shots),
                               "status": "start"})
            data = llm.retrofit(shots)
            scenes = data.get("scenes") or []
            mapping = data.get("shots") or {}
            if scenes:
                db.set_episode_scenes(eid, scenes)
            # 二次调用 LLM 补说话人：把有台词但没 speaker 的镜头单独过一遍
            updated = 0
            for s in shots:
                m = mapping.get(int(s.get("seq") or 0)) or {}
                fields = {}
                if m.get("scene_id"):
                    fields["scene_id"] = int(m["scene_id"])
                if (s.get("dialogue") or "").strip():
                    spk = m.get("speaker")
                    if not spk:
                        spk = _guess_speaker(s)
                    if spk:
                        fields["speaker"] = spk
                if fields:
                    db.update_shot(s["id"], **fields)
                    updated += 1
            _task_update(tid, {"phase": "retrofit", "total": len(shots),
                               "status": "done", "scenes": len(scenes),
                               "updated": updated})
            _task_finish(tid, "done", result={
                "scenes": len(scenes), "shots_updated": updated,
                "shots_total": len(shots)})
        except Exception as e:
            _task_finish(tid, "failed", error=f"{type(e).__name__}: {e}")

    bg.add_task(_run)
    return {"task_id": tid, "shots": len(shots)}


def _guess_speaker(shot: dict) -> str | None:
    """LLM 没给说话人时的兜底判断。

    规则：单人镜头 → 就是他；多人镜头 → 看提示词里谁的名字/代称
    离"说/道/喊/问"最近；实在判断不了返回 None（界面会标黄提示）。
    """
    names = shot.get("characters") or []
    if isinstance(names, str):
        try:
            import json as _j
            names = _j.loads(names)
        except Exception:
            names = [names]
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    p = shot.get("prompt") or ""
    import re as _re
    best, best_pos = None, -1
    for n in names:
        # 名字后面紧跟说话动词的位置
        for m in _re.finditer(_re.escape(n) + r"[^。，；]{0,12}?(说|道|喊|问|答|开口|低声|吼)",
                              p):
            if m.start() > best_pos:
                best, best_pos = n, m.start()
    return best or names[0]


@app.post("/api/episodes/{eid}/localize")
def api_localize_episode(eid: str, bg: BackgroundTasks, scope: str = "shots"):
    """把一集里英文的提示词批量转成中文（v0.5）。

    用途：v0.4 之前生成的分镜是英文（西部荒漠 E3/E4/E5），
    不必重拆分镜，直接翻译更省 LLM 成本，也不改动台词。

    scope=shots     只转分镜 prompt
    scope=all       同时转 negative 与角色外貌锚点
    """
    ep = db.get_episode(eid)
    if not ep:
        raise HTTPException(404, "剧集不存在")
    shots = db.list_shots(eid)
    if not shots:
        raise HTTPException(400, "该集没有镜头")

    tid = _new_task("localize", episode_id=eid)

    def _run():
        try:
            todo = [{"id": s["id"], "text": s.get("prompt") or ""}
                    for s in shots if llm._looks_english(s.get("prompt") or "")]
            _task_update(tid, {"phase": "localize", "kind": "prompt",
                               "total": len(todo), "status": "start"})
            done = 0
            # 每次 20 条，避免单次输出过长被截断
            for i in range(0, len(todo), 20):
                chunk = todo[i:i + 20]
                zh = llm.localize(chunk)
                for sid, text in zh.items():
                    db.update_shot(sid, prompt=text)
                done += len(zh)
                _task_update(tid, {"phase": "localize", "kind": "prompt",
                                   "index": min(i + 20, len(todo)),
                                   "total": len(todo), "status": "run",
                                   "converted": done})
            if scope == "all":
                negs = [{"id": s["id"], "text": s.get("negative") or ""}
                        for s in shots
                        if llm._looks_english(s.get("negative") or "")]
                if negs:
                    zh = llm.localize(negs)
                    for sid, text in zh.items():
                        db.update_shot(sid, negative=text)
                    _task_update(tid, {"phase": "localize", "kind": "negative",
                                       "converted": len(zh)})
                chars = [c for c in db.list_characters(ep["project_id"])
                         if llm._looks_english(c.get("appearance") or "")]
                if chars:
                    payload = [{"id": c["id"], "text": c["appearance"]} for c in chars]
                    zh = llm.localize(payload)
                    for cid, text in zh.items():
                        db.update_character(cid, appearance=text)
                    _task_update(tid, {"phase": "localize", "kind": "character",
                                       "converted": len(zh)})
            _task_finish(tid, "done", result={"converted": done,
                                              "total": len(todo)})
        except Exception as e:
            _task_finish(tid, "failed", error=f"{type(e).__name__}: {e}")

    bg.add_task(_run)
    return {"task_id": tid, "shots": len(shots)}


@app.post("/api/shots/{sid}/rerender")
def api_rerender_shot(sid: str, bg: BackgroundTasks):
    shot = db.get_shot(sid)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    ep = db.get_episode(shot["episode_id"])
    tid = _new_task("shot", shot_id=sid)

    def _run():
        try:
            r = worker.render_shot(shot, project_id=ep["project_id"],
                                   episode_seq=ep["seq"])
            _task_finish(tid, "done", result={"status": r["status"],
                                              "output": r.get("output_path"),
                                              "error": r.get("error")})
        except Exception as e:
            _task_finish(tid, "failed", error=str(e))

    bg.add_task(_run)
    return {"task_id": tid}


@app.get("/api/episodes/{eid}/takes")
def api_takes(eid: str):
    """列出该集每镜的多轮渲染 take，供「组合成片」面板选择。"""
    if not db.get_episode(eid):
        raise HTTPException(404, "剧集不存在")
    return {"takes": db.list_takes(eid)}


@app.post("/api/episodes/{eid}/takes/select")
def api_select_take(eid: str, req: dict):
    """把某镜的 output_path 改指到指定的 take 产物。

    请求体：{"shot_id": "sh_xxx", "take": "v2"}
    （take 是 v1/v2/… 序号，后端按产物命名规则找到对应文件）
    """
    shot_id = (req.get("shot_id") or "").strip()
    take = (req.get("take") or "").strip()
    if not shot_id or not take:
        raise HTTPException(400, "shot_id 和 take 必填")
    shot = db.get_shot(shot_id)
    if not shot or shot["episode_id"] != eid:
        raise HTTPException(404, "镜头不存在")

    # 扫描该镜全部 take，找到用户选的那个
    all_takes = [t for t in db.list_takes(eid)
                 if t["shot_id"] == shot_id]
    if not all_takes:
        raise HTTPException(404, "该镜头没有可用 take")
    target = None
    for tk in (all_takes[0].get("takes") or []):
        if tk.get("take") == take:
            target = tk
            break
    if not target:
        raise HTTPException(400, f"take {take} 不存在")

    ok = db.select_take(shot_id, target["path"])
    if not ok:
        raise HTTPException(400, "选择 take 失败")
    return {"ok": True, "shot_id": shot_id, "selected_take": take}


@app.post("/api/episodes/{eid}/compose")
def api_compose(eid: str, req: dict):
    """手动组合成片：接收镜头顺序 + 跳过标记 + 每镜所选 take，
    写回 DB（seq 重排、skip 标记），然后调用 assemble。"""
    ep = db.get_episode(eid)
    if not ep:
        raise HTTPException(404, "剧集不存在")

    order = req.get("order") or []
    skips = set(req.get("skips") or [])
    takes = req.get("takes") or {}    # {shot_id: "v2"}

    if not order:
        raise HTTPException(400, "order 不能为空")

    # ① 重排 seq：按前端给的顺序重新编号 1..N
    seq_map = {}
    for i, sid in enumerate(order, 1):
        seq_map[sid] = i
    db.reorder_shots(eid, seq_map)

    # ② 标记跳过：跳过的镜头不进成片（status 改为 skipped）
    for sid in skips:
        db.update_shot(sid, status="skipped")

    # ③ 应用所选 take：改 output_path
    for sid, take in takes.items():
        # 找到该镜该 take 的路径
        all_takes = db.list_takes(eid)
        for t in all_takes:
            if t["shot_id"] == sid:
                for tk in (t.get("takes") or []):
                    if tk.get("take") == take:
                        db.select_take(sid, tk["path"])
                        break
                break

    return {"ok": True, "reordered": len(seq_map), "skipped": len(skips)}


@app.post("/api/episodes/{eid}/assemble")
def api_assemble(eid: str, bg: BackgroundTasks, upscale: str = ""):
    """只重新成片（不重渲染）：拼接 → 响度/字幕 → (超分)。

    v0.4：每个阶段（规范化/拼接/响度/字幕/超分）都上报进度，
    产物登记到 artifact 表，界面可列出/预览/下载。
    """
    ep = db.get_episode(eid)
    if not ep:
        raise HTTPException(404, "剧集不存在")
    done = [s for s in db.list_shots(eid) if s["status"] == "done"]
    if not done:
        raise HTTPException(400, "没有已完成的镜头")
    if upscale and upscale not in assemble.UPSCALE_PRESETS:
        raise HTTPException(400, f"未知超分档位: {upscale}")
    tid = _new_task("assemble", episode_id=eid)

    def _run():
        try:
            art = worker.assemble_episode_only(
                eid, upscale=upscale,
                progress=lambda ev: _task_update(tid, ev))
            _task_finish(tid, "done", result={
                "output": art.get("summary"),
                "final": (art.get("final") or {}).get("path"),
                "upscale": (art.get("upscale") or {}).get("path"),
            })
        except Exception as e:
            _task_finish(tid, "failed", error=f"{type(e).__name__}: {e}")

    bg.add_task(_run)
    return {"task_id": tid}


# ---- 成片产物（artifact）----
def _artifact_url(path: str) -> str:
    """产物文件 → 可访问 URL（/api/files/{out_dir}/{相对路径}）。"""
    if not path:
        return ""
    try:
        root = _out_dir().resolve()
        p = Path(path).resolve()
        rel = p.relative_to(root)
        parts = rel.parts
        if len(parts) < 2:
            return ""
        from urllib.parse import quote
        return "/api/files/" + quote(parts[0]) + "/" + \
            "/".join(quote(x) for x in parts[1:])
    except Exception:
        return ""


def _decorate_artifacts(items: list[dict]) -> list[dict]:
    """给产物补 url / exists（前端预览与下载都依赖）。"""
    out = []
    for a in items:
        d = dict(a)
        p = d.get("path") or ""
        d["exists"] = bool(p and os.path.exists(p))
        d["url"] = _artifact_url(p) if d["exists"] else ""
        out.append(d)
    return out


@app.get("/api/episodes/{eid}/artifacts")
def api_artifacts(eid: str):
    if not db.get_episode(eid):
        raise HTTPException(404, "剧集不存在")
    return {"artifacts": _decorate_artifacts(db.list_artifacts(eid))}


@app.delete("/api/artifacts/{aid}")
def api_artifact_delete(aid: str, purge: bool = False):
    """删产物登记（purge=true 同时删磁盘文件）。"""
    if purge:
        for a in db.list_artifacts_by_id(aid):
            p = a.get("path")
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    db.delete_artifact(aid)
    return {"ok": True}


# ---- 渲染队列（跨集）----
@app.get("/api/queue")
def api_queue(limit: int = 100):
    """队列总览：队列项 + 消费者状态 + 统计。"""
    items = db.queue_list(limit=limit)
    for it in items:
        ep = db.get_episode(it.get("episode_id") or "") or {}
        prj = db.get_project(ep.get("project_id") or "") or {}
        it["project_title"] = prj.get("title") or ""
        it["episode_title"] = ep.get("title") or ""
        try:
            shots = db.list_shots(it["episode_id"])
            cnt: dict[str, int] = {}
            for s in shots:
                cnt[s["status"]] = cnt.get(s["status"], 0) + 1
            it["shots_total"] = len(shots)
            it["shots_done"] = cnt.get("done", 0)
            it["shots_failed"] = cnt.get("failed", 0)
            it["shots_running"] = cnt.get("running", 0)
        except Exception:
            pass
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return {"items": items, "counts": counts, "consumer": rqueue.status()}


@app.post("/api/queue")
def api_queue_add(req: dict):
    """入队。body: {episode_id} 或 {episode_ids:[...]}"""
    eids = req.get("episode_ids") or ([req["episode_id"]]
                                      if req.get("episode_id") else [])
    if not eids:
        raise HTTPException(400, "必须提供 episode_id 或 episode_ids")
    out = []
    for eid in eids:
        if not db.get_episode(eid):
            continue
        tid = _new_task("render-queue", episode_id=eid)
        qid = rqueue.enqueue(
            eid,
            quality_first=bool(req.get("quality_first")),
            force_engine=req.get("force_engine"),
            max_shots=req.get("max_shots"),
            upscale=req.get("upscale") or "",
            tier=req.get("tier") or "",
            task_id=tid)
        out.append({"episode_id": eid, "queue_id": qid, "task_id": tid})
    return {"ok": True, "queued": out}


@app.post("/api/queue/all")
def api_queue_all(req: dict | None = None):
    """项目内所有有镜头的剧集批量入队。"""
    b = req or {}
    pid = b.get("project_id")
    if not pid:
        raise HTTPException(400, "缺少 project_id")
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    out = []
    for ep in db.list_episodes(pid):
        if (ep.get("shot_total") or 0) <= 0:
            continue
        tid = _new_task("render-queue", episode_id=ep["id"])
        qid = rqueue.enqueue(
            ep["id"],
            quality_first=bool(b.get("quality_first")),
            force_engine=b.get("force_engine"),
            upscale=b.get("upscale") or "",
            tier=b.get("tier") or "",
            task_id=tid)
        out.append({"episode_id": ep["id"], "seq": ep["seq"],
                    "queue_id": qid, "task_id": tid})
    return {"ok": True, "queued": out, "count": len(out)}


@app.patch("/api/queue/{qid}")
def api_queue_update(qid: str, req: dict):
    """改队列项：priority / action=cancel。"""
    if not db.queue_get(qid):
        raise HTTPException(404, "队列项不存在")
    if req.get("action") == "cancel":
        db.queue_update(qid, status="canceled", finished_at=time.time())
        return {"ok": True}
    fields = {}
    if "priority" in req:
        fields["priority"] = int(req["priority"])
    if fields:
        db.queue_update(qid, **fields)
    return {"ok": True}


@app.delete("/api/queue/{qid}")
def api_queue_delete(qid: str):
    if not db.queue_get(qid):
        raise HTTPException(404, "队列项不存在")
    db.queue_update(qid, status="canceled")
    return {"ok": True}


@app.post("/api/queue/control")
def api_queue_control(req: dict):
    """action: pause | resume | start | clear_finished"""
    a = (req.get("action") or "").strip()
    if a == "pause":
        rqueue.pause()
    elif a == "resume":
        rqueue.resume()
    elif a == "start":
        rqueue.start()
    elif a == "clear_finished":
        n = db.queue_clear_finished()
        return {"ok": True, "cleared": n, "consumer": rqueue.status()}
    else:
        raise HTTPException(400, f"未知 action: {a}")
    return {"ok": True, "consumer": rqueue.status()}


# ---------------------------------------------------------------- files

@app.get("/api/files/{project_id}/{rest:path}")
def api_file(project_id: str, rest: str, download: bool = False):
    """产物文件访问。

    project_id 可以是项目 id 或产物目录名，两种都支持
    （前端有时传 id，有时直接拿 output_path 里的目录名）。

    download=true：加 Content-Disposition 强制下载；
    默认 inline：浏览器可直接预览 mp4/mp3/png/srt。
    """
    root = _out_dir().resolve()
    # 先按目录名直接找
    cand = (root / project_id).resolve()
    if not (str(cand).startswith(str(root)) and cand.exists()):
        # 再按项目 id 映射到 out_dir
        cand = _proj_out_dir(project_id).resolve()
    p = (cand / rest).resolve()
    if not str(p).startswith(str(cand)) or not p.exists():
        raise HTTPException(404, "文件不存在")
    if download:
        return FileResponse(p, filename=p.name,
                            media_type="application/octet-stream")
    return FileResponse(p, filename=p.name, content_disposition_type="inline")


# ---------------------------------------------------------------- 后台文件管理
_ADMIN_SCOPES = {
    "output": lambda: _out_dir(),
    "models": lambda: Path(cfg.get("models", "models_dir", "/models") or "/models"),
    "input": lambda: Path(cfg.get("settings", "paths.input_dir", "/input") or "/input"),
    "config": lambda: cfgmod.CONFIG_DIR,
    "logs": lambda: Path("/logs"),
}


@app.get("/api/admin/file")
def admin_file(scope: str, path: str, request: Request, download: bool = False):
    """后台文件下载/预览（单文件）。

    与 /api/files/ 的区别：这个可访问 models / input / config / logs，
    且做越界校验。download=true 强制下载，否则 inline 预览。
    """
    _auth(request)
    getter = _ADMIN_SCOPES.get(scope)
    if getter is None:
        raise HTTPException(400, "未知 scope")
    root = getter().resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(400, "路径越界")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    if download:
        return FileResponse(target, filename=target.name,
                            media_type="application/octet-stream")
    # 文本类直接当文本返回，前端好显示
    text_ext = {".txt", ".md", ".log", ".srt", ".json", ".yaml", ".yml",
                ".csv", ".env", ".py", ".sh", ".ini", ".conf"}
    if target.suffix.lower() in text_ext:
        try:
            size = target.stat().st_size
            with open(target, errors="replace") as f:
                body = f.read(400_000)
            return {"ok": True, "kind": "text", "path": path,
                    "size": size, "truncated": size > len(body.encode()),
                    "content": body}
        except Exception as e:
            raise HTTPException(500, f"读取失败: {e}")
    return FileResponse(target, filename=target.name,
                        content_disposition_type="inline")


@app.get("/api/admin/file/meta")
def admin_file_meta(scope: str, path: str, request: Request):
    """文件元信息（大小/mtime/类型/是否可预览）。"""
    _auth(request)
    getter = _ADMIN_SCOPES.get(scope)
    if getter is None:
        raise HTTPException(400, "未知 scope")
    root = getter().resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(400, "路径越界")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    st = target.stat()
    ext = target.suffix.lower()
    kind = ("video" if ext in (".mp4", ".mov", ".webm", ".mkv") else
            "image" if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif") else
            "audio" if ext in (".mp3", ".wav", ".m4a", ".aac") else
            "text" if ext in (".txt", ".md", ".log", ".srt", ".json",
                              ".yaml", ".yml", ".csv", ".env", ".py",
                              ".sh", ".ini", ".conf") else "binary")
    return {"ok": True, "path": path, "name": target.name, "size": st.st_size,
            "mtime": st.st_mtime, "ext": ext, "kind": kind,
            "scope": scope}


@app.get("/api/projects/{pid}/export")
def api_export_project(pid: str):
    """打包导出项目：剧本/分镜/角色/成片（zip）。"""
    prj = db.get_project(pid)
    if not prj:
        raise HTTPException(404, "项目不存在")

    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # 1. 项目元数据
        meta = {
            "title": prj.get("title"),
            "synopsis": prj.get("synopsis"),
            "style": prj.get("style"),
            "aspect": prj.get("aspect"),
            "stage": prj.get("stage"),
            "story": prj.get("story"),
            "outline": prj.get("outline"),
            "locked_info": prj.get("locked_info"),
            "characters": db.list_characters(pid),
            "locations": db.list_locations(pid),
            "props": db.list_props(pid),
            "episodes": [],
        }
        # 2. 剧集 + 分镜
        for ep in db.list_episodes(pid):
            shots = db.list_shots(ep["id"])
            meta["episodes"].append({
                "seq": ep["seq"], "title": ep.get("title"),
                "hook": ep.get("hook"), "conflict": ep.get("conflict"),
                "summary": ep.get("summary"),
                "cliffhanger": ep.get("cliffhanger"),
                "script": ep.get("script"),
                "shots": [{k: v for k, v in s.items()
                           if k not in ("created_at", "updated_at")} for s in shots],
            })
            # 3. 成片
            outdir = _proj_out_dir(pid) / f"E{ep['seq']:02d}"
            final = outdir / f"E{ep['seq']:02d}_final.mp4"
            if final.exists():
                z.write(final, f"final/E{ep['seq']:02d}_final.mp4")

        z.writestr("project.json",
                   json.dumps(meta, ensure_ascii=False, indent=2, default=str))
        # 4. 字幕
        for ep in db.list_episodes(pid):
            srt = _proj_out_dir(pid) / f"E{ep['seq']:02d}" / "subtitle.srt"
            if srt.exists():
                z.write(srt, f"final/E{ep['seq']:02d}.srt")

    buf.seek(0)
    from fastapi.responses import Response
    from urllib.parse import quote
    fname = f"{(prj.get('title') or 'project').replace('/', '_')}.zip"
    return Response(
        content=buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(fname)}"})


# ---- 预设（风格 / 平台 / 监控）----
@app.get("/api/presets")
def api_presets():
    """前端用的预设库（不需要管理密码）。"""
    return {
        "styles": cfg.get("presets", "styles", []) or [],
        "platforms": cfg.get("presets", "platforms", []) or [],
        "monitor": cfg.get("presets", "monitor", {}) or {},
        "assemble": cfg.get("settings", "assemble", {}) or {},
    }


@app.post("/api/presets/platform")
def api_apply_platform(req: dict):
    """应用平台规格 / 横竖屏到 settings.assemble。

    入参二选一或组合：
      {"platform_id": "douyin"}           按平台预设（含朝向与基准分辨率）
      {"orientation": "landscape"}        只切横/竖屏
      {"base_only": true}                 只改基准画布（720p），不改交付规格

    v0.5 关键：成片画布默认用 base（720p），不再沿用交付规格 1080p，
    否则每个 720p 镜头会在拼接前被拉大一次（白白多 2.25 倍像素）。
    """
    section = cfg.section("settings")
    a = section.setdefault("assemble", {})
    applied: dict = {}

    pid_ = (req or {}).get("platform_id")
    if pid_:
        plats = cfg.get("presets", "platforms", []) or []
        hit = next((p for p in plats if p.get("id") == pid_), None)
        if not hit:
            raise HTTPException(404, "平台预设不存在")
        applied = hit
        base = hit.get("base") or {}
        orient = hit.get("orientation") or (
            "landscape" if (hit.get("width") or 1080) > (hit.get("height") or 1920)
            else "portrait")
        a["orientation"] = orient
        a["width"] = int(base.get("width") or hit.get("width") or 720)
        a["height"] = int(base.get("height") or hit.get("height") or 1280)
        a["fps"] = int(hit.get("fps") or a.get("fps") or 24)
        # 记录交付规格，超分时用它做默认档
        a["delivery_width"] = int(hit.get("width") or 1080)
        a["delivery_height"] = int(hit.get("height") or 1920)

    orient = (req or {}).get("orientation")
    if orient:
        orient = "landscape" if str(orient).lower().startswith("land") else "portrait"
        w, h = int(a.get("width") or 720), int(a.get("height") or 1280)
        # 切换朝向时同步互换宽高，避免出现 1280 宽的竖屏
        if orient == "landscape" and h > w:
            w, h = h, w
        elif orient == "portrait" and w > h:
            w, h = h, w
        a["orientation"] = orient
        a["width"], a["height"] = w, h
        applied = applied or {"orientation": orient}

    cfg.write_section("settings", section)
    return {"ok": True, "applied": applied,
            "assemble": section.get("assemble"),
            "orientation": a.get("orientation")}


@app.get("/api/presets/runtime")
def api_presets_runtime():
    """当前生效的画幅/分辨率（前端展示用，不用自己算）。"""
    from pipeline import assemble as asm
    t = asm.target_from_config()
    orient = str(cfg.get("settings", "assemble.orientation", "portrait") or "portrait")
    res = {}
    for eng in ("h3",):
        for tier in ("quality", "normal", "draft"):
            v = cfg.get("routing", f"resolutions.{eng}.{tier}")
            if v:
                res[tier] = router._swap_for_orientation(v)
    return {"orientation": orient,
            "assemble": {"width": t.width, "height": t.height, "fps": t.fps,
                         "vcodec": t.vcodec},
            "render_resolutions": res,
            "delivery": {
                "width": int(cfg.get("settings", "assemble.delivery_width", 1080) or 1080),
                "height": int(cfg.get("settings", "assemble.delivery_height", 1920) or 1920),
            },
            "upscale_targets": list(asm.UPSCALE_PRESETS.keys())}


@app.put("/api/presets/monitor")
def api_save_monitor(req: dict):
    """保存监控栏配置（显示项 + 刷新间隔）。存到浏览器也行，这里兼顾多端。"""
    section = cfg.section("presets")
    mon = section.setdefault("monitor", {})
    if "enabled" in req:
        mon["enabled"] = req["enabled"]
    if "refresh_sec" in req:
        try:
            mon["refresh_sec"] = max(3, min(int(req["refresh_sec"]), 300))
        except Exception:
            pass
    cfg.write_section("presets", section)
    return {"ok": True, "monitor": mon}


# ============================================================== 后台管理

@app.get("/api/admin/system")
def admin_system(request: Request):
    _auth(request)
    return system()


@app.get("/api/admin/config")
def admin_config(request: Request):
    _auth(request)
    return {"snapshot": cfg.snapshot(), "files": cfg.file_info()}


@app.get("/api/admin/config/{section}")
def admin_config_section(section: str, request: Request):
    _auth(request)
    from pipeline.config import SECTIONS
    if section not in SECTIONS:
        raise HTTPException(400, f"未知配置块（可用: {', '.join(SECTIONS)}）")
    return {"section": section, "data": cfg.section(section)}


@app.put("/api/admin/config")
def admin_config_write(req: ConfigWrite, request: Request):
    _auth(request)
    try:
        backup = cfg.write_section(req.section, req.data)
    except Exception as e:
        raise HTTPException(400, f"写入失败: {e}")
    return {"ok": True, "backup": backup, "files": cfg.file_info()}


@app.post("/api/admin/config/reload")
def admin_config_reload(request: Request):
    _auth(request)
    try:
        cfg.reload()
    except Exception as e:
        raise HTTPException(400, f"重载失败: {e}")
    return {"ok": True, "snapshot_keys": list(cfg.snapshot().keys())}


# ---- 模型管理 ----
@app.get("/api/admin/models")
def admin_models(request: Request):
    _auth(request)
    return {"models": _model_inventory(),
            "models_dir": cfg.get("models", "models_dir", "/models")}


@app.post("/api/admin/models/toggle")
def admin_model_toggle(req: ModelToggle, request: Request):
    _auth(request)
    section = cfg.section("models")
    models = section.get("models", [])
    hit = next((m for m in models if m.get("id") == req.model_id), None)
    if not hit:
        raise HTTPException(404, "模型不存在")
    if hit.get("required") and not req.enabled:
        raise HTTPException(400, "必需模型不能禁用")
    hit["enabled"] = req.enabled
    cfg.write_section("models", section)
    return {"ok": True, "model": hit}


@app.post("/api/admin/models/add")
def admin_model_add(m: ModelAdd, request: Request):
    _auth(request)
    section = cfg.section("models")
    models = section.setdefault("models", [])
    if any(x.get("id") == m.id for x in models):
        raise HTTPException(400, f"模型 id 已存在: {m.id}")
    models.append(m.model_dump())
    cfg.write_section("models", section)
    return {"ok": True}


@app.post("/api/admin/models/check")
def admin_model_check(request: Request):
    """对所有模型做文件存在性+大小校验。"""
    _auth(request)
    inv = _model_inventory()
    return {"ok": True,
            "missing": [m["id"] for m in inv if not m["present"]],
            "incomplete": [m["id"] for m in inv if m["present"] and not m["complete"]],
            "models": inv}


@app.post("/api/admin/models/update")
def admin_model_update(req: dict, request: Request):
    """编辑单个模型：改名/文件路径/类型/引擎/备注/大小等。

    入参：{"model_id": "h3-fl2va-int8", "patch": {"name": "...", "file": "..."}}
    id 本身不可改（其它配置靠它引用）。
    """
    _auth(request)
    mid = req.get("model_id")
    patch = req.get("patch") or {}
    if not mid:
        raise HTTPException(400, "缺少 model_id")
    section = cfg.section("models")
    models = section.get("models", [])
    hit = next((m for m in models if m.get("id") == mid), None)
    if not hit:
        raise HTTPException(404, f"模型不存在: {mid}")

    immutable = {"id"}
    allowed = {"name", "engine", "kind", "file", "size_gb", "required",
               "enabled", "modes", "note"}
    bad = [k for k in patch if k in immutable]
    if bad:
        raise HTTPException(400, f"这些字段不可修改: {bad}")
    unknown = [k for k in patch if k not in allowed]
    if unknown:
        raise HTTPException(400, f"未知字段: {unknown}")

    hit.update(patch)
    cfg.write_section("models", section)
    # 改完重新校验一次文件状态
    inv = next((m for m in _model_inventory() if m.get("id") == mid), None)
    return {"ok": True, "model": inv}


@app.delete("/api/admin/models/{model_id}")
def admin_model_delete(model_id: str, request: Request):
    """从配置中移除一个模型（不动磁盘文件）。必需模型不允许删。"""
    _auth(request)
    section = cfg.section("models")
    models = section.get("models", [])
    hit = next((m for m in models if m.get("id") == model_id), None)
    if not hit:
        raise HTTPException(404, "模型不存在")
    if hit.get("required"):
        raise HTTPException(400, "必需模型不能删除（引擎依赖它）")
    section["models"] = [m for m in models if m.get("id") != model_id]
    cfg.write_section("models", section)
    return {"ok": True, "removed": model_id}


# ---- ComfyUI 对接 ----
@app.get("/api/admin/comfy")
def admin_comfy(request: Request):
    """读取 ComfyUI 对接配置 + 实时连通性。"""
    _auth(request)
    conf = cfg.get("settings", "comfy", {}) or {}
    live: dict[str, Any] = {"alive": False, "comfyui_version": None,
                            "ram_free_gb": None, "devices": []}
    try:
        if comfy.alive():
            st = comfy.system_stats()
            sysinfo = st.get("system") or {}
            devs = st.get("devices") or []
            live = {
                "alive": True,
                "comfyui_version": sysinfo.get("comfyui_version"),
                "pytorch_version": sysinfo.get("pytorch_version"),
                "python_version": (sysinfo.get("python_version") or "").split()[0],
                "ram_free_gb": round((sysinfo.get("ram_free") or 0) / 1073741824, 1),
                "devices": [{"name": d.get("name"),
                             "vram_total_gb": round((d.get("vram_total") or 0) / 1073741824, 1),
                             "vram_free_gb": round((d.get("vram_free") or 0) / 1073741824, 1)}
                            for d in devs],
            }
    except Exception as e:
        live["error"] = str(e)[:300]
    return {"config": conf, "effective_url": comfy.base_url(), "live": live}


@app.post("/api/admin/comfy/test")
def admin_comfy_test(req: dict | None = None, request: Request = None):
    """测试一个 ComfyUI 地址是否可用（不写入配置）。"""
    _auth(request)
    url = ((req or {}).get("base_url") or comfy.base_url()).rstrip("/")
    import urllib.request
    t0 = time.time()
    try:
        with urllib.request.urlopen(f"{url}/system_stats", timeout=8) as r:
            d = json.loads(r.read())
        s = d.get("system") or {}
        devs = d.get("devices") or []
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "comfyui_version": s.get("comfyui_version"),
                "devices": [x.get("name") for x in devs]}
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"[:300]}


@app.get("/api/admin/comfy/queue")
def admin_comfy_queue(request: Request = None):
    """看 ComfyUI 当前真实队列（含孤儿任务）。"""
    if request is not None:
        _auth(request)
    try:
        q = comfy.queue_status()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "running": 0, "pending": 0}
    run = q.get("queue_running") or []
    pend = q.get("queue_pending") or []

    def brief(item):
        try:
            pid = item[1] if len(item) > 1 else None
            wf = item[2] if len(item) > 2 else {}
            types = [n.get("class_type") for n in wf.values()] if isinstance(wf, dict) else []
            engine = "h3" if "MiniMaxH3ImageToVideo" in types else (
                "wan22" if "Wan22ImageToVideoLatent" in types else "?")
            return {"prompt_id": pid, "engine": engine}
        except Exception:
            return {"prompt_id": None, "engine": "?"}

    return {"ok": True,
            "running": len(run), "pending": len(pend),
            "running_items": [brief(x) for x in run],
            "pending_items": [brief(x) for x in pend]}


@app.post("/api/admin/comfy/interrupt")
def admin_comfy_interrupt(request: Request = None):
    """中断 ComfyUI 当前任务（清孤儿）。"""
    if request is not None:
        _auth(request)
    try:
        comfy.interrupt()
        time.sleep(1)
        comfy.free_vram()
        return {"ok": True, "after": gpu_preflight()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ---- 提示词管理 ----
@app.get("/api/admin/prompts")
def admin_prompts(request: Request):
    _auth(request)
    return cfg.section("prompts")


@app.put("/api/admin/prompts")
def admin_prompts_write(req: dict, request: Request):
    _auth(request)
    try:
        backup = cfg.write_section("prompts", req)
    except Exception as e:
        raise HTTPException(400, f"写入失败: {e}")
    return {"ok": True, "backup": backup}


# ---- 密钥管理 ----
@app.get("/api/admin/secrets")
def admin_secrets(request: Request):
    _auth(request)
    s = cfgmod.load_secrets()
    return {"secrets": {k: cfgmod.mask_secret(v) for k, v in s.items()},
            "keys": list(s.keys())}


@app.put("/api/admin/secrets")
def admin_secret_write(req: SecretIn, request: Request):
    _auth(request)
    if not req.key.replace("_", "").isalnum():
        raise HTTPException(400, "key 只能含字母数字下划线")
    cfgmod.write_secret(req.key, req.value)
    return {"ok": True, "masked": cfgmod.mask_secret(req.value)}


@app.delete("/api/admin/secrets/{key}")
def admin_secret_delete(key: str, request: Request):
    _auth(request)
    cfgmod.write_secret(key, "")
    return {"ok": True}


@app.get("/api/admin/llm/test")
def admin_llm_test(request: Request):
    _auth(request)
    return llm.health()


@app.get("/api/admin/llm/models")
def admin_llm_models(request: Request, base_url: str = "", api_key: str = ""):
    """拉取 LLM 服务端可用模型列表（OpenAI 兼容 /v1/models）。

    未传参数时用当前配置。用于后台下拉选择 default/fallback 模型。
    """
    _auth(request)
    import urllib.request as _ur
    url = (base_url or llm.base_url()).rstrip("/")
    key = api_key or llm.api_key()
    if not key:
        raise HTTPException(400, "未配置 API KEY，请先在「LLM 配置」里填写")
    t0 = time.time()
    try:
        req = _ur.Request(f"{url}/models",
                          headers={"Authorization": f"Bearer {key}"})
        with _ur.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise HTTPException(502, f"连接失败: {e}")

    items = d.get("data") or d.get("models") or []
    out = []
    for m in items:
        if isinstance(m, str):
            out.append({"id": m, "name": m})
            continue
        mid = m.get("id") or m.get("name")
        if not mid:
            continue
        out.append({
            "id": mid,
            "name": m.get("name") or mid,
            "context_length": (m.get("context_length")
                               or m.get("context_window")
                               or (m.get("meta") or {}).get("context_length")),
            "owned_by": m.get("owned_by"),
        })
    out.sort(key=lambda x: x["id"])
    return {"ok": True, "count": len(out), "models": out,
            "latency_ms": int((time.time() - t0) * 1000), "base_url": url}


# ---- 模型下载 ----
class ModelDownloadIn(BaseModel):
    source: str = "modelscope"      # modelscope | huggingface | hf-mirror | url
    repo: str = ""                  # 如 Comfy-Org/MiniMax-H3
    file: str = ""                  # 仓库内路径
    target: str = ""                # 落到 models/ 下的相对路径（默认同 file）
    url: str = ""                   # source=url 时直链
    model_id: str = ""              # 可选：绑定到某个模型 id，下完自动启用


DOWNLOAD_SOURCES = {
    "modelscope": "https://modelscope.cn/models/{repo}/resolve/master/{file}",
    "huggingface": "https://huggingface.co/{repo}/resolve/main/{file}",
    "hf-mirror": "https://hf-mirror.com/{repo}/resolve/main/{file}",
}


@app.post("/api/admin/models/download")
def admin_model_download(req: ModelDownloadIn, bg: BackgroundTasks, request: Request):
    """后台下载模型（后台任务，进度写 logs/model_download.log）。"""
    _auth(request)
    models_dir = Path(cfg.get("models", "models_dir", "/models") or "/models")
    target_rel = (req.target or req.file or "").strip()
    if not target_rel and not req.url:
        raise HTTPException(400, "必须提供 file 或 url")

    # 组装下载地址
    if req.source == "url":
        if not req.url:
            raise HTTPException(400, "source=url 时必须提供 url")
        src_url = req.url
        if not target_rel:
            target_rel = req.url.rsplit("/", 1)[-1].split("?")[0]
    else:
        tpl = DOWNLOAD_SOURCES.get(req.source)
        if not tpl:
            raise HTTPException(400, f"未知 source: {req.source}")
        if not req.repo or not req.file:
            raise HTTPException(400, "必须提供 repo 和 file")
        src_url = tpl.format(repo=req.repo.strip("/"), file=req.file.lstrip("/"))

    target_rel = target_rel.lstrip("/")
    if ".." in target_rel:
        raise HTTPException(400, "非法目标路径")
    dest = models_dir / target_rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    logdir = Path("/logs")
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / "model_download.log"
    tid = _new_task("download", shot_id=None)

    def _run():
        import subprocess
        _task_update(tid, {"phase": "download", "status": "start",
                           "url": src_url, "target": str(dest)})
        cmd = ["curl", "-L", "--retry", "5", "--retry-delay", "3", "-C", "-",
               "-o", str(dest), src_url]
        try:
            with open(logfile, "a") as lf:
                lf.write(f"\n[{time.strftime('%H:%M:%S')}] START {target_rel}\n")
                lf.flush()
                p = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                   timeout=7200)
            if p.returncode == 0 and dest.exists():
                size_gb = round(dest.stat().st_size / 1e9, 2)
                with open(logfile, "a") as lf:
                    lf.write(f"[{time.strftime('%H:%M:%S')}] DONE {target_rel} {size_gb}GB\n")
                # 绑定到模型条目并启用
                if req.model_id:
                    try:
                        sec = cfg.section("models")
                        for m in sec.get("models", []):
                            if m.get("id") == req.model_id:
                                m["file"] = target_rel
                                m["size_gb"] = size_gb
                                m["enabled"] = True
                                break
                        cfg.write_section("models", sec)
                    except Exception as e:
                        print("绑定模型失败:", e)
                _task_finish(tid, "done",
                             result={"target": target_rel, "size_gb": size_gb})
            else:
                _task_finish(tid, "failed",
                             error=f"curl 退出码 {p.returncode}")
        except Exception as e:
            _task_finish(tid, "failed", error=f"{type(e).__name__}: {e}")

    bg.add_task(_run)
    return {"ok": True, "task_id": tid, "url": src_url, "target": str(dest)}


@app.get("/api/admin/models/sources")
def admin_model_sources(request: Request):
    """可用的模型下载源说明（给前端下拉用）。"""
    _auth(request)
    return {"sources": [
        {"id": "modelscope", "zh": "ModelScope（魔搭，国内快）",
         "tpl": DOWNLOAD_SOURCES["modelscope"]},
        {"id": "huggingface", "zh": "Hugging Face（需能连）",
         "tpl": DOWNLOAD_SOURCES["huggingface"]},
        {"id": "hf-mirror", "zh": "Hugging Face 镜像站",
         "tpl": DOWNLOAD_SOURCES["hf-mirror"]},
        {"id": "url", "zh": "自定义 http/https 直链", "tpl": ""},
    ]}


# ---- 文件管理 ----
@app.get("/api/admin/files")
def admin_files(request: Request, scope: str = "output"):
    _auth(request)
    getter = _ADMIN_SCOPES.get(scope)
    if getter is None:
        raise HTTPException(400, "未知 scope")
    root = getter()
    items = []
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                try:
                    st = p.stat()
                    ext = p.suffix.lower()
                    kind = ("video" if ext in (".mp4", ".mov", ".webm", ".mkv") else
                            "image" if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif") else
                            "audio" if ext in (".mp3", ".wav", ".m4a", ".aac") else
                            "text" if ext in (".txt", ".md", ".log", ".srt", ".json",
                                              ".yaml", ".yml", ".csv", ".env", ".py",
                                              ".sh", ".ini", ".conf") else "binary")
                    items.append({
                        "path": str(p.relative_to(root)),
                        "name": p.name,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "ext": ext,
                        "kind": kind,
                    })
                except OSError:
                    continue
    total = sum(i["size"] for i in items)
    # 目录聚合（前端可折叠显示）
    dirs: dict[str, dict] = {}
    for i in items:
        d = os.path.dirname(i["path"]) or "."
        e = dirs.setdefault(d, {"dir": d, "count": 0, "bytes": 0})
        e["count"] += 1
        e["bytes"] += i["size"]
    return {"scope": scope, "root": str(root), "count": len(items),
            "total_bytes": total, "items": items[:3000],
            "dirs": sorted(dirs.values(), key=lambda x: x["dir"])[:400]}


@app.delete("/api/admin/files")
def admin_file_delete(request: Request, scope: str, path: str):
    _auth(request)
    roots = {
        "output": _out_dir(),
        "models": Path(cfg.get("models", "models_dir", "/models") or "/models"),
        "input": Path(cfg.get("settings", "paths.input_dir", "/input") or "/input"),
        "logs": Path("/logs"),
    }
    root = roots.get(scope)
    if root is None:
        raise HTTPException(400, "该 scope 不允许删除")
    target = (root / path).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise HTTPException(400, "路径越界")
    if not target.exists():
        raise HTTPException(404, "文件不存在")
    if target.is_dir():
        raise HTTPException(400, "只能删文件")
    target.unlink()
    return {"ok": True}


# ---- 任务与日志 ----
@app.get("/api/admin/jobs")
def admin_jobs(request: Request, limit: int = 100):
    _auth(request)
    return {"jobs": db.list_jobs(limit)}


@app.get("/api/admin/logs")
def admin_logs(request: Request, name: str = "comfyui", lines: int = 200):
    _auth(request)
    log_dir = Path("/logs")
    p = log_dir / name
    if not p.exists():
        cands = [x for x in log_dir.glob(f"*{name}*")] if log_dir.exists() else []
        if not cands:
            return {"name": name, "lines": [], "note": "日志文件不存在"}
        p = cands[0]
    try:
        with open(p, errors="replace") as f:
            data = f.readlines()[-max(1, min(lines, 2000)):]
    except Exception as e:
        raise HTTPException(500, f"读取失败: {e}")
    return {"name": p.name, "lines": [x.rstrip() for x in data]}


@app.get("/api/admin/config-file/{name}")
def admin_config_file(name: str, request: Request):
    """直接读配置文件原文（管理界面编辑器用）。"""
    _auth(request)
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = cfgmod.CONFIG_DIR / name
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    if name.endswith(".env"):
        return {"name": name, "content": _mask_env(p.read_text(encoding="utf-8"))}
    return {"name": name, "content": p.read_text(encoding="utf-8")}


@app.put("/api/admin/config-file/{name}")
def admin_config_file_write(name: str, req: dict, request: Request):
    _auth(request)
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    content = req.get("content", "")
    p = cfgmod.CONFIG_DIR / name
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    import time as _t
    backup = f"{name}.bak.{_t.strftime('%Y%m%d_%H%M%S')}"
    (cfgmod.CONFIG_DIR / backup).write_bytes(p.read_bytes())
    p.write_text(content, encoding="utf-8")
    if name.endswith(".env"):
        try:
            p.chmod(0o600)
        except OSError:
            pass
    cfg.reload()
    return {"ok": True, "backup": backup}


def _mask_env(text: str) -> str:
    out = []
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            if any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                out.append(f"{k}={cfgmod.mask_secret(v.strip())}")
                continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- static

@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    p = STATIC_DIR / "admin.html"
    if not p.exists():
        raise HTTPException(404, "admin.html 不存在")
    return HTMLResponse(p.read_text(encoding="utf-8"))


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.on_event("startup")
def _startup():
    db.init_db()
    cfg.ensure_files()
    # 上次运行被中断的任务（容器重建/崩溃）标为 failed，
    # 否则它们永远卡在 running，前端看着像「在跑」其实没人管。
    try:
        n = db.mark_stale_tasks(timeout_sec=0)   # 启动时全部旧任务都算中断
        if n:
            print(f"[startup] 标记 {n} 个中断任务为 failed")
    except Exception as e:
        print(f"[startup] 清理旧任务失败: {e}")

    # 卡在 running 的镜头同样要回收。worker 被 kill 后它们既不会被重试，
    # 也不会再有人推进，前端却一直显示「在渲」。
    try:
        n = db.reset_running_shots()
        if n:
            print(f"[startup] {n} 个卡住的镜头重置为 pending")
    except Exception as e:
        print(f"[startup] 重置镜头失败: {e}")

    # ComfyUI 里可能还残留上次没人接收的排队任务（失败路径曾不清理），
    # 它们会占着 GPU 空转。启动时一律清掉，渲染队列自己会重新提交。
    try:
        if comfy.alive():
            q = comfy.queue_status()
            leftover = len(q.get("queue_pending") or [])
            if leftover or (q.get("queue_running") or []):
                comfy.clear_queue()
                print(f"[startup] 清理 ComfyUI 残留队列 {leftover} 个")
    except Exception as e:
        print(f"[startup] 清理 ComfyUI 队列失败: {e}")
    for d in (cfg.get("settings", "paths.output_dir", "/output"),
              cfg.get("settings", "paths.input_dir", "/input"),
              DATA_DIR):
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # 渲染队列消费者：启动即恢复（上次 running 项排回 queued）
    try:
        if bool(cfg.get("settings", "render.queue_autostart", True)):
            rqueue.start()
            print("[startup] 渲染队列消费者已启动")
    except Exception as e:
        print(f"[startup] 队列启动失败: {e}")
