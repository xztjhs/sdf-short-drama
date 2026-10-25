# SDF MCP Server 对接指南

> 短剧工厂 v0.6.4 起内置 MCP（Model Context Protocol）server，
> 任何支持 MCP Streamable HTTP 的客户端（OpenClaw、CherryStudio、
> Claude Desktop 等）都可以直接查询和触发短剧工厂的操作。

## MCP server 信息

| 项 | 值 |
|---|---|
| 协议 | MCP Streamable HTTP（JSON-RPC over HTTP POST） |
| 端点 | `http://YOUR_SERVER_IP:8080/mcp` |
| 协议版本 | `2025-06-18` |
| 无鉴权 | 仅内网使用 |

## OpenClaw 接入（已实测）

一条命令搞定，会自动探测连接再保存：

```bash
openclaw mcp add sdf \
  --transport streamable-http \
  --url http://YOUR_SERVER_IP:8080/mcp \
  --timeout 120
```

验证（应输出 `9 tools`，doctor 应输出 `ok`）：

```bash
openclaw mcp probe sdf
openclaw mcp doctor
```

让当前会话立刻拿到新工具（不重启 Gateway）：

```bash
openclaw mcp reload
```

手写配置等价于在 `~/.openclaw/openclaw.json` 里加（**顶层 `mcp.servers`，不是 `mcpServers`**）：

```json
{
  "mcp": {
    "servers": {
      "sdf": {
        "url": "http://YOUR_SERVER_IP:8080/mcp",
        "transport": "streamable-http",
        "requestTimeoutMs": 120000
      }
    }
  }
}
```

> ⚠️ 两个易错点：
> 1. 键名是 `mcp.servers`。`mcpServers` 是 Claude Desktop / CherryStudio 的写法，
>    OpenClaw 读不到。
> 2. 传输字段是 `transport: "streamable-http"`。
>    写 `type: "http"` 也能用，但会被 `openclaw doctor --fix` 归一化掉。
>
> 模型侧看到的工具名带服务前缀：`sdf__sdf_list_projects`。
> 想少给几个工具，用 `openclaw mcp tools sdf --include 'sdf_list_*,sdf_get_*'`。

## CherryStudio 接入

**设置 → MCP 服务器 → 添加 → 类型选「可流式传输的 HTTP」(streamableHttp)**，填：

- 名称：`sdf`
- URL：`http://YOUR_SERVER_IP:8080/mcp`
- 不需要填密钥/Header（服务端无鉴权）

添加后点「启用」，新建对话时勾选该 MCP 服务器即可。

手写 `mcpServers` 配置等价于：

```json
{
  "mcpServers": {
    "sdf": {
      "type": "streamableHttp",
      "url": "http://YOUR_SERVER_IP:8080/mcp"
    }
  }
}
```

> 如果客户端只支持 SSE，本服务没开 SSE 端点，会连不上——
> 请确认选的是 streamable HTTP（有些版本叫「HTTP」或「HTTP 流」）。
> 客户端必须在同一内网（能访问 YOUR_SERVER_IP:8080）。

## 工具清单

| 工具 | 作用 |
|---|---|
| `sdf_list_projects` | 列出所有短剧项目 |
| `sdf_get_project` | 查项目详情（含大纲/锁定信息） |
| `sdf_list_episodes` | 列出某项目的所有剧集 |
| `sdf_get_episode` | 查剧集详情（含分镜镜头列表） |
| `sdf_list_shots` | 查剧集分镜镜头列表 |
| `sdf_render_episode` | 触发渲染（可只渲部分镜头） |
| `sdf_get_task` | 查任务状态（渲染进度） |
| `sdf_list_artifacts` | 查成片与产物 |
| `sdf_get_prompt_preview` | 查镜头将要发给模型的完整提示词（预览） |

## 使用示例

### 列出项目
```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "sdf_list_projects", "arguments": {}}}
```

### 触发渲染
```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
 "params": {"name": "sdf_render_episode",
            "arguments": {"eid": "ep_5e86f9c4a9b0", "tier": "normal"}}}
```

### 查任务进度
```json
{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
 "params": {"name": "sdf_get_task",
            "arguments": {"tid": "task_xxx"}}}
```

## 验证 MCP server 是否正常

```bash
# initialize
curl -s -X POST http://YOUR_SERVER_IP:8080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18",
                 "capabilities":{},
                 "clientInfo":{"name":"curl","version":"1.0"}}}'

# tools/list
curl -s -X POST http://YOUR_SERVER_IP:8080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

## 注意事项

1. **渲染是长任务**：`sdf_render_episode` 单镜约 11-19 分钟，
   一集（17 镜）约 3-5 小时。建议用 `sdf_get_task` 轮询进度。
2. **单卡串行**：A5000 24GB 上 H3 峰值 22GB，并发渲染必 OOM，
   同一时间只能跑一个渲染任务。
3. **不做删除**：MCP 工具面刻意不含删除操作（防误操作），
   删除请走 Web UI。
4. **无鉴权**：MCP server 不做鉴权，仅限内网使用。
   如需公网暴露，请在前面加反向代理 + 鉴权。
