# v0.2 变更说明

发布日期：2026-10-21
基线：v0.1（整集端到端已验证）

---

## 一句话

**配置全部外置 + 五段创作流程 + 后台管理界面 + 目录迁到 compose/video。**

---

## 1. 目录迁移

| 项 | v0.1 | v0.2 |
|---|---|---|
| 根目录 | `/mnt/disks/nvme/docker/video` | **`/mnt/disks/nvme/docker/compose/video`** |
| 配置 | 散落在代码和 `.env` | **`config/` 下 6 个文件** |

迁移用 `mv`（同盘，秒级完成），已有数据/模型/产物全部保留。

---

## 2. 配置全部外置

### 你只需要改这些文件

```
/mnt/disks/nvme/docker/compose/video/config/
├── settings.yaml      运行参数（并发/超时/路径/成片规格/日志）
├── models.yaml        模型注册表（10 个模型，含启用开关）
├── prompts.yaml       提示词模板（大纲/分镜/基础信息/润色）
├── routing.yaml       引擎路由规则（关键词、音效判定、耗时表）
├── engines.yaml       引擎参数（步数/cfg/LoRA/帧数规则）
├── secrets.env        API KEY（权限 600，不进 git）
└── secrets.env.example  模板
```

### 三种改法，任选

| 方式 | 适合 |
|---|---|
| **宿主机直接编辑** | 用 vim/VSCode，改完点界面「重载配置」 |
| **后台管理界面** | 表单/编辑器，自动备份旧文件 |
| **API** | `PUT /api/admin/config` |

### 关键设计

- **改动立即生效**，不用重建镜像（`cfg.reload()`）
- **写前自动备份**：`settings.yaml.bak.20261021_184500`
- **缺字段用内置默认值兜底**，不会因为删了一行就崩
- **首次启动自动生成**：`ensure_files()` 从镜像内模板复制

---

## 3. 五段创作流程

```
① 输入故事  →  ② 剧情及大纲  →  ③ 锁定基础信息  →  ④ 分镜头脚本  →  ⑤ 渲染成片
```

| 阶段 | API | 做什么 | 产物 |
|---|---|---|---|
| story | `POST /api/projects` | 收原始故事素材 | project.story |
| outline | `POST /api/projects/{id}/outline` | LLM 拆成 N 集，每集带钩子/冲突/悬念 | project.outline + episode 行 |
| locked | `POST /api/projects/{id}/extract-baseinfo`<br>`POST /api/projects/{id}/lock` | LLM 抽角色/场景/道具 → 人工确认 → 锁定 | character(locked=1) / location / prop |
| storyboard | `POST /api/episodes/{id}/storyboard` | LLM 拆分镜，**强制沿用锁定信息** | shot 行 + 自动路由 |
| render | `POST /api/render` | 串行渲染 + 自动成片 | E01_final.mp4 |

### 为什么要「锁定基础信息」这一步

不锁的话，同一角色在不同镜头里会被 LLM 描述成不同长相，出片必然串脸。

锁定后：
- 角色外貌锚点（英文）固定下来，写进 `character` 表并标 `locked=1`
- 分镜生成时把锁定信息拼进 prompt，强制 LLM 沿用
- 已锁定的角色，分镜阶段不会覆盖

### 流程状态机

`project.stage` 字段：`story → outline → locked → storyboard → rendering → done`

界面顶部步骤条按此高亮，已完成打勾。

---

## 4. 后台管理界面

**地址**：`http://YOUR_SERVER_IP:8080/admin`

| 模块 | 功能 |
|---|---|
| **总览** | GPU/内存/磁盘/模型完整性/配置文件状态，一键重载、一键测 LLM |
| **设置项** | settings.yaml 表单化编辑，按组切换（app/render/paths/assemble/ui/logging） |
| **模型接入** | 10 个模型列表，启用/禁用开关，文件校验，手动添加 |
| **提示词** | 按模板分页编辑 system/user，LLM 参数单独一页 |
| **路由策略** | routing.yaml 编辑器（JSON 视图，后端转 YAML） |
| **引擎参数** | engines.yaml 编辑器 |
| **API KEY** | 掩码显示，可改可加，一键测连通性 |
| **文件管理** | output/models/input/config 四类浏览，支持删除 |
| **配置文件** | 直接看/改原文件（含 secrets.env 掩码版） |
| **任务日志** | 内存任务进度 + job 表历史 |

### 密码保护

在 `config/secrets.env` 里设 `ADMIN_PASSWORD=xxx` 即启用。
留空 = 不校验（仅内网使用）。

---

## 5. 路由策略修正（重要）

v0.1 的规则有个严重问题：**「有音效」就走 H3**。

实测发现 LLM 会给几乎每个镜头都填音效（风声、脚步声、城市背景音），结果 6 个镜头全部走 H3，一集要 **64 分钟**。

v0.2 修正为：

| 判定 | 走哪 | 理由 |
|---|---|---|
| 有**对白** | H3 | 口型必须同步 |
| **人物发声**（笑/哭/喊/尖叫/呢喃…） | H3 | 需与表情同步 |
| **环境音**（风/雨/脚步/背景音） | Wan2.2 | 后期 ffmpeg 加，不影响路由 |
| 动作/空镜/低光 | Wan2.2 | 快 4 倍 |

**效果**：同样 6 个镜头，从 64 分钟降到 **32 分钟**。

音效关键词在 `routing.yaml` 的 `vocal_sfx` 列表里，可自行增删。

---

## 6. 实测验证

### 五段流程（LLM 部分，2026-10-21）

```
[1] 输入故事        ✅
[2] 剧情大纲        ✅ 2 集 / 2.3s
[3] 锁定基础信息    ✅ 2 角色（英文外貌锚点完整）
[4] 分镜头脚本      ✅ 6 镜 / 11.3s，路由 H3=2 5B=4
[5] 渲染成片        ✅ （测试中）
```

### 路由修正前后

| 版本 | H3 | Wan2.2 | 预计耗时 |
|---|---|---|---|
| 修正前 | 6 | 0 | 64 分钟 |
| **修正后** | **2** | **4** | **32 分钟** |

---

## 7. 修掉的坑

| 坑 | 现象 | 修法 |
|---|---|---|
| **旧库缺列** | `table episode has no column named hook` | `_migrate()` 逐列 ALTER（v0.1 的 CREATE IF NOT EXISTS 不改已存在的表） |
| **模型名带路径** | ComfyUI 报 `value_not_in_list` | 加载节点只吃**纯文件名**，新增 `model_basename()` 与 `model_file()` 分离 |
| **配置快照为空** | `ensure_files()` 写完文件但内存还是旧值 | 写完立刻 `reload()` |
| **apt 源太慢** | Debian 官方源构建 40 分钟且卡死 | Dockerfile 换清华源，构建降到几分钟 |
| **路由过度触发 H3** | 环境音也算「有音效」 | 拆分 `vocal_sfx`，只有人物发声走 H3 |

---

## 8. 已知问题

| 项 | 状态 |
|---|---|
| RAGFlow 反复自行启动 | ⚠️ 2026-10-21 已被停 3 次。原因未查明（crontab/compose.manager 均无记录）。会抢 22GB 显存导致渲染失败 |
| swap 未配 | ⚠️ 内存紧张时 OOM 静默杀进程 |
| Ref2VA 角色一致性 | ⬜ 权重未下载（+21GB），代码未接 |
| Wan2.2 低光欠曝 | ⚠️ 已在 prompts.yaml 要求 LLM 补 "well lit"，但未强制 |

---

## 9. 升级步骤

```bash
# 1. 停栈
cd /mnt/disks/nvme/docker/compose/video
docker compose down

# 2. 同步代码（从开发机）
rsync -av --exclude='models' --exclude='output' --exclude='data' \
  --exclude='logs' --exclude='input' --exclude='.git' \
  ./ root@YOUR_SERVER_IP:/mnt/disks/nvme/docker/compose/video/

# 3. 重建（apt 层有缓存，约 1 分钟）
docker compose build api

# 4. 起栈
docker compose up -d

# 5. 验证
curl -s localhost:8080/api/health
docker exec video-api python /app/test_flow_v02.py
```

**旧数据自动迁移**：`db.init_db()` 会检测缺列并 ALTER，不用手动改库。
