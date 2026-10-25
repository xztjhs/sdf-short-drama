"""ComfyUI 客户端 —— 提交任务 + 轮询进度 + 取产物。

连接地址与超时全部来自 config/settings.yaml 的 comfy 段，
改配置即可指向另一台 ComfyUI（例如换机器/加卡后跑双实例）。

优先读配置；配置缺失时回退到环境变量 SDF_COMFYUI_URL。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


class ComfyError(RuntimeError):
    pass


def _conf(key: str, default: Any = None) -> Any:
    """延迟读取配置，避免导入期循环依赖（config 不依赖 comfy）。"""
    try:
        from .config import cfg
        v = cfg.get("settings", f"comfy.{key}", None)
        if v is not None:
            return v
    except Exception:
        pass
    return default


def base_url() -> str:
    """当前 ComfyUI 地址（每次调用都重读，支持热改配置）。"""
    return str(_conf("base_url") or os.environ.get("SDF_COMFYUI_URL", "http://127.0.0.1:8188")).rstrip("/")


def default_timeout() -> int:
    return int(_conf("request_timeout_sec", 30) or 30)


# 向后兼容：老代码用 comfy.BASE
class _BaseProxy:
    def __str__(self) -> str:
        return base_url()

    def __fspath__(self) -> str:
        return base_url()


BASE = _BaseProxy()


def _req(path: str, data: dict | None = None, timeout: int | None = None) -> dict:
    url = f"{base_url()}{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or default_timeout()) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:2000]
        raise ComfyError(f"HTTP {e.code} {path}: {detail}") from e
    except urllib.error.URLError as e:
        raise ComfyError(f"连接失败 {url}: {e.reason}") from e


def alive() -> bool:
    try:
        r = _req("/system_stats", timeout=5)
        return "system" in r or "devices" in r
    except Exception:
        return False


def system_stats() -> dict:
    return _req("/system_stats", timeout=10)


def queue_status() -> dict:
    return _req("/queue", timeout=10)


def upload_image(local_path: str, name: str | None = None) -> str:
    """上传到 ComfyUI input 目录，返回服务器端文件名（I2V 首帧用）。"""
    name = name or os.path.basename(local_path)
    boundary = "----video" + uuid.uuid4().hex
    with open(local_path, "rb") as f:
        content = f.read()
    parts = []
    parts.append(f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n')
    parts.append(f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n")
    body = b"".join(p.encode() for p in parts) + content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    return d.get("name", name)


def submit(workflow: dict, client_id: str | None = None) -> str:
    """提交工作流（API 格式），返回 prompt_id。

    ⚠️ 若抛 "Prompt outputs failed validation" 或显存错误，
       先检查节点参数是否与当前 ComfyUI 版本匹配。
    """
    payload = {"prompt": workflow, "client_id": client_id or uuid.uuid4().hex}
    d = _req("/prompt", payload, timeout=int(_conf("submit_timeout_sec", 60) or 60))
    if "prompt_id" not in d:
        raise ComfyError(f"提交失败，无 prompt_id: {d}")
    if d.get("node_errors"):
        raise ComfyError(f"节点校验失败: {json.dumps(d['node_errors'], ensure_ascii=False)[:1500]}")
    return d["prompt_id"]


def history(prompt_id: str) -> dict:
    d = _req(f"/history/{prompt_id}", timeout=20)
    return d.get(prompt_id, {})


def wait(prompt_id: str, timeout: float = 3600, poll: float = 3.0,
         on_progress=None) -> dict:
    """轮询等完成。返回 history 条目。

    不用 WebSocket 的原因：ComfyUI 的 ws 在长任务里偶尔丢消息，
    而 /history 是权威来源。轮询 3s 对几分钟级的任务无感。
    """
    t0 = time.time()
    last_node = None
    while time.time() - t0 < timeout:
        h = history(prompt_id)
        if h:
            st = h.get("status", {})
            if st.get("status_str") == "error" or st.get("completed") is False and st.get("messages"):
                msgs = st.get("messages", [])
                err = next((m for m in msgs if m[0] == "execution_error"), None)
                if err:
                    raise ComfyError(f"渲染失败: {json.dumps(err[1], ensure_ascii=False)[:1500]}")
            if h.get("outputs") or st.get("completed"):
                return h
        if on_progress:
            try:
                q = queue_status()
                running = q.get("queue_running", [])
                if running:
                    node = running[0][2] if len(running[0]) > 2 else None
                    if node != last_node:
                        last_node = node
                        on_progress({"node": node, "elapsed": round(time.time() - t0, 1)})
            except Exception:
                pass
        time.sleep(poll)
    raise ComfyError(f"超时 {timeout}s 未完成 prompt {prompt_id}")


def outputs_of(h: dict, kind: str = "images") -> list[dict]:
    """从 history 里取产物列表。视频也用 kind='images'（ComfyUI 惯例）。"""
    out = []
    for node_id, node_out in (h.get("outputs") or {}).items():
        for item in node_out.get(kind, []) or []:
            out.append({"node": node_id, **item})
    return out


def interrupt() -> None:
    try:
        _req("/interrupt", {}, timeout=10)
    except Exception:
        pass


def cancel_prompt(prompt_id: str, interrupt_running: bool = True) -> bool:
    """撤掉指定 prompt：排队中的从队列删除，正在跑的则中断。

    ⚠️ 失败/超时路径必须调用。否则 ComfyUI 会在后台把这个镜头跑完，
       产物无人接收，纯粹白烧 GPU（实测残留 72 个任务空转 8 小时）。
    """
    ok = False
    try:
        _req("/queue", {"delete": [prompt_id]}, timeout=10)
        ok = True
    except Exception:
        pass
    if interrupt_running:
        try:
            q = queue_status()
            running = [it[1] for it in (q.get("queue_running") or []) if len(it) > 1]
            if prompt_id in running:
                _req("/interrupt", {}, timeout=10)
                ok = True
        except Exception:
            pass
    return ok


def clear_queue(interrupt_running: bool = True) -> int:
    """清空 ComfyUI 全部排队任务，返回清掉的条数。

    渲染队列是单消费者串行跑的，所以整队清空不会误伤别的任务；
    只有在「本集任务已经失败、队列里剩的全是它的残留」时才安全。
    """
    n = 0
    try:
        q = queue_status()
        n = len(q.get("queue_pending") or [])
    except Exception:
        pass
    if interrupt_running:
        interrupt()
    try:
        _req("/queue", {"clear": True}, timeout=10)
    except Exception:
        pass
    return n


def free_vram(unload_models: bool = True, free_memory: bool = True) -> None:
    """释放显存。跑长片前、或 OOM 后调用。"""
    try:
        _req("/free", {"unload_models": unload_models, "free_memory": free_memory}, timeout=30)
    except Exception:
        pass
