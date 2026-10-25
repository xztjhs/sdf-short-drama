# v0.3 变更说明

发布日期：2026-10-22
基线：v0.2（配置外置 + 五段流程 + 后台管理）

---

## 一句话

**改名 SDF + 项目管理全套 + 镜头级控制 + 模型在线下载 + 角色库复用 + 镜头衔接。**

v0.2 → v0.3：6 个提交，+4835 行。

---

## 1. 改名 Video → SDF

| 项 | v0.2 | v0.3 |
|---|---|---|
| 根目录 | `compose/video` | **`compose/sdf`** |
| 容器 | `video-api` / `video-comfyui` | **`sdf-api`** / **`sdf-comfyui`** |
| 镜像 | `video-api:local` | `sdf-api:local` |
| 数据库 | `video.db` | **`sdf.db`** |
| 环境变量 | `VIDEO_*` | `SDF_*` |
| 界面标题 | `Video 短剧工厂` | **`SDF 短剧工厂`** |

代码 / 文档 / 部署脚本全部同步。迁移用同盘 `mv`，59GB 秒级完成。

### 旧项目目录迁移

`prj_xxx` → `{YYYYMMDD-HHMM}_{英文名}`，3 个已迁，1 个本就新格式跳过。
工具：`scripts/migrate_dirs.py`（带 `--dry-run`）。

---

## 2. 项目管理

原来只能「新建 / 点开」。现在：

- **搜索**：按标题/简介实时过滤
- **归档**：默认列表隐藏，可切换查看
- **单项菜单**：重命名 / 复制 / 导出 ZIP / 归档 / 删除
- **批量**：多选后归档 / 取消归档 / 删除
- **进度条**：每项显示已完成镜头/总镜头

删除需手打 `DELETE` 确认，会连产物视频一起删。

**导出 ZIP** 含 `project.json`（角色/剧集/分镜全量）+ 成片 mp4 + 字幕 srt。

**自动英文名**：新建/改名时用 LLM 翻译标题生成路径安全的 slug；目录同步重命名（保留时间戳前缀）。

---

## 3. 镜头级控制

| 能力 | 说明 |
|---|---|
| 状态列 | 待渲染/渲染中/已完成/失败/已跳过（中文） |
| 耗时列 | 每镜实际耗时 |
| **可读报错** | `CUDA OOM` → "显存不足"、`value_not_in_list` → "模型文件名不对"等 |
| 单镜操作 | 重渲 / 预览 / 上移 / 下移 / 删除 |
| 手工加镜头 | 自动排到末尾 |
| 批量重置 | 仅失败项 / 全部 |

---

## 4. 渲染队列 + 任务持久化

**队列可视化**：每镜一个色块（灰=待渲染 黄=渲染中 绿=完成 红=失败），悬停看详情，点击看错误。顶部有计数 + 进度条 + 剩余时间估算。

**任务持久化（重要修复）**：`task` 表落库，API 重启不再丢任务记录。

> 实测踩到的坑：重建 API 容器后，内存里的任务记录没了，但 ComfyUI 还在老实算——形成「跑了 1 小时 20 分的幽灵任务占着 22GB 显存、界面上却什么都没有」。
> 现在启动时会把上次中断的任务标为 failed，并提供 `/api/admin/comfy/queue` 查真实队列、`/api/admin/comfy/interrupt` 中断孤儿任务。

---

## 5. ComfyUI 对接配置化

原先硬编码 `BASE = os.environ.get(...)`。现在 `config/settings.yaml`：

```yaml
comfy:
  base_url: http://sdf-comfyui:8188   # 换机器改这里
  request_timeout_sec: 30
  submit_timeout_sec: 60
  wait_timeout_sec: 7200
  poll_interval_sec: 3
  free_vram_before_shot: true
```

`comfy.py` 每次调用实时读配置，改完点「重载」即生效。后台有独立面板：地址编辑、测连通、实时状态（版本/PyTorch/内存/GPU）。

---

## 6. 模型管理

- **编辑**：改名/路径/类型/引擎/备注/大小（id 不可改）
- **删除**：从配置移除（必需模型禁止）
- **在线下载**：ModelScope / Hugging Face / HF 镜像站 / 自定义直链
  - 后台任务跑，可绑定模型 id 下完自动启用
  - 缺失的模型行直接有 ⬇ 按钮

> 修复：`models` 挂载从 `:ro` 改为可写，否则下载必然失败（`curl: (23) Read-only file system`）。

---

## 7. LLM 配置

- API KEY 输入（掩码显示，留空不改）
- **获取模型列表**（拉 `/v1/models`，实测取回 10 个）
- 主模型 / 降级模型下拉选择
- `max_tokens` 默认 **64K**
- `context_length` 默认 **256K**（并在调用前校验，超长直接报清楚）
- 连通测试按钮

---

## 8. 全局角色库

跨项目复用同一人设：`character_lib` 表 + 全套 CRUD。锁定页两个按钮：「📚 从角色库导入」「💾 存到角色库」，按名字自动去重。

**角色参考图终于接进渲染**（v0.2 只存不用）：镜头没有首帧时，取该角色 `ref_image` 作为首帧锚定长相。

---

## 9. 界面改进

| 项 | 内容 |
|---|---|
| 监控栏 | 11 项可配（GPU/温度/CPU/内存/SWAP/磁盘/磁盘IO/网络/ComfyUI/LLM/队列），刷新 3-300s |
| 浅色主题 | 顶栏 ☀️/🌙 切换，localStorage 记忆，首次跟随系统 |
| 中英对照 | 风格预设 10 套、后台字段/分组全部中文标签 |
| 预览限高 | 默认 600px 按比例缩放，可拖 240-900px 或切按宽铺满 |
| 集数不限 | 去掉 30 集 / 200 镜头上限 |
| 大纲滚动 | 超过 560px 出滚动条，集号徽标 |

---

## 10. 渲染质量修复（用户实测反馈）

### 镜头不连贯

镜头各自独立生成，人物/环境突变。

**修复**：新增镜头衔接 `chain_frames`（默认开）——抽上一镜尾帧作为下一镜首帧。`assemble.extract_last_frame()` 实现。

### H3 声音听不懂、和台词对不上

**根因**：`dialogue` 字段只用于路由判断，**从未进入 H3 提示词**，模型只能自己瞎编人声。

**修复**：`worker.build_h3_prompt()` 把对白注入：

```
...The character speaks this exact line in Mandarin Chinese: "你早就知道，对不对？".
Lip movement must match the speech precisely. Native dialogue audio,
clear pronunciation, no gibberish. Background sound: 风声.
```

无对白时明确 `Ambient room tone only, no speech, no voice`。

### 渲染模式选择

| 模式 | 行为 |
|---|---|
| 自动路由（默认） | 对白走 H3，空镜走 5B |
| **全部 H3** | 每镜原生音频，声音统一，慢 3-4 倍 |
| 质量优先 | 全 H3 + 768p + 12 步 |
| 草稿 | 全 5B + 544p，最快，**无音轨** |

UI 明确提示：混合模式下空镜无音轨，声音会突变。

### 分辨率与超分

默认降到 **720×1280**。成片区可手动超分 **1080P / 2K / 4K**（Lanczos 重采样，原片保留）。

> 这是高质量重采样不是 AI 超分。720p→1080p 够用；2K/4K 有涂抹感，界面已注明。

---

## 11. 无限重绘死循环（最隐蔽的 bug）

**症状**：打开项目页不停闪、输入框内容被清空、看不到「输入故事」内容、控制台反复报 `Cannot set properties of null (setting 'innerHTML')`。

**根因**：

```
渲染页加载 → 查到历史任务（已完成）
  → pollTask 立刻判定"结束"
  → render() 整页重绘
  → 又查到同一个任务 → 死循环
```

**修复**：只挂载 `status === 'running'` 且未处理过的任务，加 `handledTasks` 集合 + `watchingTask` 去重。

---

## 12. 配置同步策略（避免互相覆盖）

| 脚本 | 行为 |
|---|---|
| `deploy/sync_deploy.sh` | `config/` 用 `--ignore-existing`，只补缺失文件，**绝不覆盖** |
| `scripts/merge_config.py` | 深合并：只补缺失键，已有值保留 |
| `scripts/set_config.py` | 显式覆盖指定键（改默认值时用） |

原因：界面上改的值存在远端 `config/`，直接 `rsync --delete` 会冲掉；但只用 `--ignore-existing` 又带不进新默认值。

---

## 13. 实测数据

### 完整流程（6 镜）

| 镜 | 引擎 | 耗时 |
|---|---|---|
| 1 | Wan2.2 | 162s |
| 2 | Wan2.2 | 145s |
| 3 | **H3** | **926s（15.4 分）** |
| 4 | Wan2.2 | 186s |
| 5 | **H3** | **926s** |
| 6 | **H3** | 526s |

成片：1080×1920 / 29.9s / 9.0MB / H.264+AAC 2ch，字幕烧录正确。

### 单镜耗时（720p 默认档）

| 引擎 | 耗时 |
|---|---|
| H3 | ~15 分（768p 实测，720p 待测） |
| Wan2.2 | ~2.5-3 分 |

> ⚠️ H3 从早先记录的 10.6 分涨到 15.4 分，全程 84°C，怀疑降频。产能估算需按此重算。

---

## 14. 验证

| 套件 | 结果 |
|---|---|
| SDF 后台（浏览器） | 23/23 |
| 浏览器基础回归 | 28/28 |
| 镜头/队列回归 | 15/15 |
| 命名/翻译/角色库（接口） | 17/17 |
| 最终修复验证 | 13/13 |

---

## 15. 已知问题

| 项 | 状态 |
|---|---|
| RAGFlow 反复自启 | 抢 22GB 显存会导致渲染失败；渲染前有预检拦截 |
| swap 未配 | 内存紧张时 OOM 静默杀进程 |
| H3 耗时上升 | 768p 实测 15.4 分，需复测确认是否降频 |
| Ref2VA 角色一致性 | 权重未下载（+21GB），未接 |
| AI 超分 | 当前只有 Lanczos 重采样 |

---

## 升级步骤

```bash
# 1. 停栈
cd /mnt/disks/nvme/docker/compose/sdf && docker compose down

# 2. 同步（config 受保护，只补缺失文件）
./deploy/sync_deploy.sh

# 3. 合并新配置键
docker exec sdf-api python /app/merge_config.py \
  --defaults /data/_cfg_defaults --target /config

# 4. 重建起栈
docker compose build api && docker compose up -d

# 5. 验证
curl -s localhost:8080/api/health
```
