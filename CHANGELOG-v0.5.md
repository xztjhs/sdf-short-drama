# v0.5 变更说明

发布日期：2026-10-22
基线：v0.4（分镜模式 + 中文提示词 + 成片链路 + 跨集队列）

---

## 一句话

**只留 H3（Wan2.2 下线并归档）· 成片回归 720p（不再隐式拉 1080p）· 平台预设支持横竖屏 · 全链路中文 + 旧集批量转中文 · 成片/超分可预览。**

对应老板第二轮 7 条反馈的第 1/2/3/4/5 条（第 6/7 条是分析类，见 `docs/09` 与 LLM 对比报告）。

---

## 1. 全链路中文提示词（问题①）

### 之前哪里还是英文

| 环节 | v0.4 状态 |
|---|---|
| 分镜 prompt | ✅ 已中文（新生成的） |
| **历史分镜（西部荒漠 E1/E3/E4/E5）** | ❌ 全是英文（v0.4 之前生成） |
| **视觉风格（①输入故事）** | ❌ 界面写死"英文风格提示词（进渲染）"，存的是英文 |
| **角色外貌锚点（③锁定）** | ❌ 存的是 `English anchor: a 28-year-old woman...` |
| 分镜时注入的风格 | ❌ 走 `prj.style`（英文原串），不是 `style_zh` |

所以"进渲染的提示词还是英文"是真的 —— 不是模板没切，是**存量数据 + 风格字段**还是英文。

### 现在怎么改

1. **风格预设改中文渲染提示词**：`presets.styles[].render`（例：`废土荒漠风格，强烈日光，高对比暖赭色调，锈蚀金属质感，飞扬沙尘，电影感，胶片颗粒`）。界面展示中文、存储中文、渲染用中文。
2. **风格选择逻辑** `llm.pick_style()`：中文模式优先 `style_zh` → 不是英文的 `style` → 请求值，避免把英文串喂进中文模板。
3. **旧集批量转中文**（新增能力，不用重拆分镜）：
   ```
   POST /api/episodes/{eid}/localize?scope=shots   # 只转分镜 prompt
   POST /api/episodes/{eid}/localize?scope=all     # 同时转 negative + 角色锚点
   ```
   界面：第④步「🀄 本集转中文」按钮。走 `localize_v2` 模板，一次 20 条，
   保留镜头语言/光线/色调/景别，不改台词、不动已渲染的镜头、不重排 seq。

### 实测

- E3（空枪）40 镜：**40/40 全部转中文**，约 2 分钟
- 转换样例：`Extreme wide shot, vast yellow sand wasteland highway under harsh noon sunlight...`
  → `极远景，正午刺眼阳光下广袤黄沙荒原公路，一个孤独纤细的女人剪影，留着鲜艳深红色卷发，沿着开裂的柏油路朝镜头走来，热浪蒸腾闪烁...`

---

## 2. 只保留 MiniMax H3（问题②）

### 配置层

| 文件 | 变更 |
|---|---|
| `models.yaml` | 删除 `wan22-ti2v-5b` / `wan22-clip` / `wan22-vae`；**启用 `h3-ref2va-int8`**（之前 disabled） |
| `routing.yaml` | `default_engine: h3`、`allowed_engines: [h3]`、删掉 2 条指向 wan22 的规则、删 `resolutions.wan22` / `cost.wan22` |
| `prompts.yaml` | 分镜规则只留 h3 |

### 代码层（三层兜底，防止旧数据把流程带回已归档权重）

```
① router.route()          → allowed_engines 白名单收敛
② worker.render_shot()    → 再兜一次，非 h3 直接改 h3
③ queue.run_one()         → force_engine 固定 "h3"
```

旧镜头里残留 `engine: wan22` 不会报错，会自动按 H3 渲，界面路由原因显示
"原定 wan22 已下线 → 改用 H3"。

### H3 模型完整性核查（实际查盘）

| 模型 | 文件 | 大小 | 状态 |
|---|---|---|---|
| H3 FL2VA（主力） | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 20.97 GB | ✅ |
| **H3 Ref2VA**（角色一致性） | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 20.97 GB | ✅ **已下载，v0.5 启用** |
| Qwen3-VL 32B 文本编码器 | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 15.69 GB | ✅ |
| H3 视频 VAE | `minimax_h3_video_vae_fp16.safetensors` | 5.21 GB | ✅ |
| H3 音频 VAE | `minimax_h3_audio_vae_fp32.safetensors` | 0.61 GB | ✅ |
| Turbo 8 步 LoRA | `minimax_h3_fl2v_turbo_8step_v1.0...` | 1.96 GB | ✅ |
| Turbo 4 步 LoRA | `minimax_h3_fl2v_turbo_4step...` | 1.96 GB | ✅（disabled，草稿备用） |

**结论：H3 没有缺失模型**，7 个文件全在盘上，共约 67 GB。之前以为缺 ref2va，实际是配置里 `enabled: false`。

### Wan2.2 归档（Gitea + 本机）

- 权重移到 **`/mnt/disks/nvme/sdf-model-archive/wan2.2-20261022/`**（部署树之外，避免 rsync 误删）
- 三个文件 sha256 全部校验通过：`wan2.2_ti2v_5B` / `umt5_xxl_fp8` / `wan2.2_vae`
- Gitea 仓库 **`admin/sdf-model-archive`**（private）：
  `README.md`（清单+sha256+恢复步骤）、`restore.sh`（一键重下）、
  `models.wan22.yaml.bak`（v0.4 末态 models.yaml）、`config-snapshot/`（settings/routing/presets 快照）

> ⚠️ **踩坑记录（重要）**：第一次把权重移到 `compose/sdf/models_backup/`，
> 紧接着跑 `sync_deploy.sh`，它的 `rsync -az --delete` 把这个目录**整个删了**
> （不在排除列表）。幸好 sha256 已先算出、且 ModelScope 可重下，已恢复并校验。
> 现已把 `models_backup` 与 `workflows` 都加入排除列表，并在脚本里写明
> "新增运行时目录必须同步加排除"。

---

## 3. 为什么默认出来是 1080p（问题③）

### 原因（一句话）

**单镜确实是 720p，但成片画布被平台预设设成了 1080×1920，拼接前的"统一规格"阶段把每个 720p 镜头 Lanczos 放大了一次。**

具体链路：

```
① routing.yaml  resolutions.h3.normal = 720x1280
   → 单镜渲染 720×1280 ✅（这个没变）

② presets.yaml  platforms.douyin = {width: 1080, height: 1920}
   → 点「应用平台规格」写进 settings.assemble.width/height = 1080×1920

③ assemble.normalize_clip()
   → 把每个 720×1280 的镜头 scale 到 1080×1920
   → 像素量 ×2.25，画质没有真实提升（源就是 720p）
```

所以「720p 渲染 + 1080p 成片」= **一次无意义的中间放大**，还多花编码时间。

### 修复

| 项 | v0.4 | v0.5 |
|---|---|---|
| `assemble.width/height` | 1080×1920 | **720×1280**（= 渲染基准） |
| 平台预设含义 | 直接当画布 | 拆成 `base`（画布）+ `width/height`（**交付规格**） |
| 要 1080p | 默认就是 | **点超分**，显式产出 `_1080p.mp4` |

### 实测对比（西部荒漠 E01，40 镜 159 秒）

| 版本 | 成片分辨率 | 文件大小 | 说明 |
|---|---|---|---|
| v0.4 | 1080×1920 | 200.7 MB | 720p 源被放大，像素虚增 |
| **v0.5** | **720×1280** | **101.0 MB** | 与源一致，无放大 |

两版都保留，可在产物列表里对比。

---

## 4. 平台预设支持横竖屏（问题④）

### 数据结构

```yaml
platforms:
  - id: douyin
    orientation: portrait          # portrait | landscape
    base: {width: 720, height: 1280}   # 渲染与成片画布（默认档）
    width: 1080, height: 1920          # 交付规格（超分默认档）
    fps: 30
  - id: bilibili
    orientation: landscape
    base: {width: 1280, height: 720}
    width: 1920, height: 1080
    fps: 30
```

新增 3 个通用预设：`portrait-generic`（9:16）、`landscape-generic`（16:9）、`youtube`（横屏长视频）。

### 切换逻辑

- 界面：①输入故事页「画面方向」下拉 + 「平台规格」下拉 + 应用按钮
- 切朝向时自动交换宽高（避免出现 1280 宽的竖屏）
- **渲染分辨率自动跟随**：`routing.resolutions` 仍只写竖屏一套，
  `router._swap_for_orientation()` 在横屏时交换 W/H。
  实测横屏：`normal 1280×720`、`quality 1344×768`、`draft 960×544`

### API

```
POST /api/presets/platform   {platform_id} 或 {orientation}
GET  /api/presets/runtime    当前画幅/渲染档/交付档（前端不再自己算）
```

---

## 5. 成片与超分产物预览（问题⑤）

之前产物 `url` 只在 `/api/episodes/{id}/artifacts` 里补，而第⑤页用的是
`/api/episodes/{id}`（artifact 没带 url）→ 前端拿不到地址，预览按钮不出现。

修复：抽出 `_decorate_artifacts()`，两个接口共用（顺带补 `exists` 字段，
文件丢了会标「文件缺失」）。

界面现在有：

- **顶部主预览**：优先生成片，其次最新超分，内嵌播放器
- 每个产物一行：类型徽标、分辨率、时长、大小、**▶ 预览**、⬇ 下载、🗑 移除登记
- 预览弹窗：大画面播放 + 高度 240-900 拖动 + 按宽度铺满 + 下载 + 新窗口

---

## 6. 其他

| 项 | 说明 |
|---|---|
| 质量档透传 | 界面选 标准/质量/草稿 → `RenderIn.tier` → worker → router，真正决定分辨率与步数 |
| 按 seq 串行 | `plan_episode` 不重排（v0.4 已修），ETA 用 `cost.h3` 实测值 |
| 迁移脚本 | `scripts/migrate_v05.py`：settings/models/routing/presets/prompts 五块，只补不覆盖、保留注释、写前 YAML 自检 |
| 部署脚本加固 | `sync_deploy.sh` 排除 `models_backup` / `workflows`，并注释说明原因 |

---

## 7. 升级步骤

```bash
cd /home/claw/work/projects/Video
./deploy/sync_deploy.sh
ssh root@YOUR_SERVER_IP
cd /mnt/disks/nvme/docker/compose/sdf
docker compose build api && docker compose up -d api

docker exec sdf-api python /app/migrate_v05.py --config /config --dry-run
docker exec sdf-api python /app/migrate_v05.py --config /config
curl -X POST localhost:8080/api/admin/config/reload
```

回滚：`config/*.bak.YYYYmmdd_HHMMSS` + 权重从 `/mnt/disks/nvme/sdf-model-archive/` 移回。

---

## 8. 验证记录

| 套件 | 结果 |
|---|---|
| 浏览器回归（前台 22 + 后台 5） | **27/27** |
| 720p 成片（E01 40 镜） | 720×1280 / 101.0 MB ✅（原 1080×1920 / 200.7 MB） |
| 横屏切换 | 1280×720 + 渲染档自动交换 ✅ |
| 旧集转中文 E3 | 40/40 ✅ |
| Wan2.2 归档校验 | 3 文件 sha256 全部 OK ✅ |
| H3 模型完整性 | 7 个文件齐全，无缺失 ✅ |
| 残留 wan2.2 镜头 | 自动改 h3，界面标注原因 ✅ |

---

## 9. 已知问题

| 项 | 状态 |
|---|---|
| AI 超分 | 仍为 Lanczos 重采样 |
| Ref2VA 工作流 | 模型已启用，但 `workflow.py` 仍只有 fl2va 图；r2v 节点待接（见 `docs/09` 方案 B） |
| 跨镜人物一致性 | 提示词层可改善，锁脸需 Ref2VA + 参考图（见 `docs/09`） |
| 语音跨镜一致性 | H3 每镜独立发声，需合并镜头或后期 TTS（见 `docs/09` 方案 C） |
