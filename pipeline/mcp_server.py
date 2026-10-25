"""SDF MCP Server —— 把短剧工厂暴露为 MCP 工具。

接入方：
  OpenClaw      → 模型配置/工具面板添加 MCP server
  CherryStudio  → 设置 → MCP 服务器 → 添加
  任何支持 MCP streamable HTTP 的客户端

传输：MCP Streamable HTTP（单一 /mcp 端点，JSON-RPC）
依赖：只用标准库 + web/app.py 已有依赖（fastapi/pydantic），
      mcp 包由容器 requirements.txt 提供

工具清单（tools/list 返回）：
  sdf_list_projects      列出项目
  sdf_get_project        查项目详情（含大纲/锁定信息）
  sdf_list_episodes      列出某项目的所有剧集
  sdf_get_episode        查剧集详情（含分镜镜头列表）
  sdf_list_shots         查剧集分镜镜头列表
  sdf_render_episode     触发渲染（可只渲部分镜头）
  sdf_get_task           查任务状态（渲染进度）
  sdf_list_artifacts     查成片与产物
  sdf_get_prompt_preview 查镜头将要发给模型的完整提示词（预览）

设计原则：
  1. 所有工具都做「读」或「幂等触发」，不做删除（防误操作）
  2. 渲染是长任务，返回 task_id，用 sdf_get_task 查进度
  3. 错误以 MCP 协议标准错误返回（jsonrpc error）
"""
from __future__ import annotations

import inspect
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pipeline import __version__, comfy, db, worker
from pipeline.config import cfg

router = APIRouter()

# ---------------------------------------------------------------------------
# MCP 协议层
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2025-06-18"
JSONRPC_VERSION = "2.0"

MCP_TOOLS: list[dict[str, Any]] = []


def mcp_tool(name: str, description: str):
    """装饰器：把函数注册为 MCP 工具，参数 schema 从类型注解推导。"""

    def deco(fn):
        sig = inspect.signature(fn)
        props: dict[str, Any] = {}
        required: list[str] = []
        for pname, param in sig.parameters.items():
            ann = param.annotation
            if ann is inspect.Parameter.empty:
                continue
            tname = getattr(ann, "__name__", str(ann))
            if tname == "str":
                schema_t = {"type": "string"}
            elif tname == "int":
                schema_t = {"type": "integer"}
            elif tname == "float":
                schema_t = {"type": "number"}
            elif tname == "bool":
                schema_t = {"type": "boolean"}
            else:
                schema_t = {"type": "string"}
            desc = ""
            props[pname] = {**schema_t, "description": desc}
            if param.default is inspect.Parameter.empty:
                required.append(pname)

        MCP_TOOLS.append({
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        })
        fn._mcp_name = name
        return fn

    return deco


def _jsonrpc_response(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "result": result,
    })


def _jsonrpc_error(req_id: Any, code: int, message: str,
                   data: Any = None) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "error": err,
    })


# ---------------------------------------------------------------------------
# 工具实现（全部走 db/worker，不重复造轮子）
# ---------------------------------------------------------------------------

@mcp_tool("sdf_list_projects", "列出所有短剧项目")
def tool_list_projects(include_archived: bool = False) -> dict:
    items = db.list_projects(include_archived=include_archived)
    return {"projects": items}


@mcp_tool("sdf_get_project", "查项目详情（含大纲/锁定信息）")
def tool_get_project(pid: str) -> dict:
    p = db.get_project(pid)
    if not p:
        raise FileNotFoundError(f"项目不存在: {pid}")
    return p


@mcp_tool("sdf_list_episodes", "列出某项目的所有剧集")
def tool_list_episodes(pid: str) -> dict:
    eps = db.list_episodes(pid)
    return {"episodes": eps}


@mcp_tool("sdf_get_episode", "查剧集详情（含分镜镜头列表）")
def tool_get_episode(eid: str) -> dict:
    ep = db.get_episode(eid)
    if not ep:
        raise FileNotFoundError(f"剧集不存在: {eid}")
    ep["shots"] = db.list_shots(eid)
    return ep


@mcp_tool("sdf_list_shots", "查剧集分镜镜头列表")
def tool_list_shots(eid: str) -> dict:
    shots = db.list_shots(eid)
    return {"shots": shots}


@mcp_tool("sdf_render_episode", "触发渲染（可只渲部分镜头）")
def tool_render_episode(eid: str, tier: str = "", max_shots: int = 0,
                        quality_first: bool = False) -> dict:
    ep = db.get_episode(eid)
    if not ep:
        raise FileNotFoundError(f"剧集不存在: {eid}")
    if not comfy.alive():
        raise RuntimeError("ComfyUI 未就绪，无法渲染")
    # 走 worker 同步渲染（简化版：不做后台任务）
    res = worker.run_episode(eid, quality_first=quality_first,
                             max_shots=max_shots or None, tier=tier)
    return res


@mcp_tool("sdf_get_task", "查任务状态（渲染进度）")
def tool_get_task(tid: str) -> dict:
    t = db.get_task(tid)
    if not t:
        raise FileNotFoundError(f"任务不存在: {tid}")
    return t


@mcp_tool("sdf_list_artifacts", "查成片与产物")
def tool_list_artifacts(eid: str) -> dict:
    arts = db.list_artifacts(eid)
    return {"artifacts": arts}


@mcp_tool("sdf_get_prompt_preview", "查镜头将要发给模型的完整提示词（预览）")
def tool_get_prompt_preview(sid: str) -> dict:
    shot = db.get_shot(sid)
    if not shot:
        raise FileNotFoundError(f"镜头不存在: {sid}")
    prompt = worker.build_h3_prompt(shot, project_id="", episode_id="")
    return {"prompt": prompt}


# ---------------------------------------------------------------------------
# MCP 协议处理
# ---------------------------------------------------------------------------

def _handle_initialize(msg: dict) -> JSONResponse:
    params = msg.get("params") or {}
    client_info = params.get("clientInfo") or {}
    return _jsonrpc_response(msg.get("id"), {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name": "sdf-mcp",
            "version": __version__,
            "title": f"Short Drama Factory MCP (v{__version__})",
        },
        "instructions": "短剧工厂 MCP server。可列出项目/剧集/镜头，触发渲染并查询进度。",
        "clientInfo": client_info,
    })


def _handle_tools_list(msg: dict) -> JSONResponse:
    return _jsonrpc_response(msg.get("id"), {"tools": MCP_TOOLS})


def _handle_tools_call(msg: dict) -> JSONResponse:
    params = msg.get("params") or {}
    name = params.get("name") or ""
    args = params.get("arguments") or {}

    handler = None
    for t in MCP_TOOLS:
        if t["name"] == name:
            handler = _TOOL_REGISTRY.get(name)
            break
    if handler is None:
        return _jsonrpc_error(msg.get("id"), -32602,
                              f"未知工具: {name}")

    try:
        result = handler(**args)
        return _jsonrpc_response(msg.get("id"), {
            "content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False,
                                            default=str)}],
            "isError": False,
        })
    except FileNotFoundError as e:
        return _jsonrpc_response(msg.get("id"), {
            "content": [{"type": "text", "text": f"未找到: {e}"}],
            "isError": True,
        })
    except Exception as e:
        return _jsonrpc_response(msg.get("id"), {
            "content": [{"type": "text",
                         "text": f"{type(e).__name__}: {e}"}],
            "isError": True,
        })


_TOOL_REGISTRY: dict[str, Any] = {}
for _t in [tool_list_projects, tool_get_project, tool_list_episodes,
           tool_get_episode, tool_list_shots, tool_render_episode,
           tool_get_task, tool_list_artifacts, tool_get_prompt_preview]:
    _TOOL_REGISTRY[_t._mcp_name] = _t


# ---------------------------------------------------------------------------
# HTTP 端点（MCP Streamable HTTP）
# ---------------------------------------------------------------------------

@router.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")
    method = body.get("method") or ""
    if method == "initialize":
        return _handle_initialize(body)
    if method == "notifications/initialized":
        return JSONResponse(status_code=202, content={})
    if method == "ping":
        return _jsonrpc_response(body.get("id"), {})
    if method == "tools/list":
        return _handle_tools_list(body)
    if method == "tools/call":
        return _handle_tools_call(body)
    return _jsonrpc_error(body.get("id"), -32601,
                          f"Method not found: {method}")


@router.get("/mcp")
async def mcp_get():
    """SSE 流保留位：当前仅支持 POST（Streamable HTTP）。"""
    return JSONResponse({"error": "use POST /mcp"}, status_code=405)
