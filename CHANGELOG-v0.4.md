# v0.4 变更说明

发布日期：2026-10-22
基线：v0.3（项目管理 + 镜头控制 + 模型下载 + 角色库 + 镜头衔接）

---

## 一句话

**分镜按模型模式生成 + 全流程中文提示词 + 渲染成片链路显式化 + 跨集渲染队列 + 角色库独立界面 + 后台文件预览下载。**

来源于老板第一集（西部荒漠 E01，40 镜全 H3）实测反馈的 6 条问题。

---

## 1. 分镜按模型模式生成（问题 ①，最关键）

### 原来错在哪

分镜阶段让 LLM 自由发挥，`engine` 靠渲染时**事后路由**（routing.yaml 按关键词猜）。
后果：西部荒漠 E01 的 40 个镜头**全部**被判成 H3（因为 LLM 给每镜都填了环境音 sfx，
而 `vocal_sfx` 关键词里含"风"以外的大量词），一集要跑 10 小时。

### 现在怎么做

第 4 步新增「渲染模式」下拉，分镜时就把模式写进 system prompt：

| 模式 | 分镜规则（写进提示词） | 单镜耗时 |
|---|---|---|
| **自动路由**（默认） | 有台词/人物发声 → `engine=h3`；空镜/环境/道具/动作/背影 → `engine=wan22`。H3 占比建议 30-50% | 13-15 分 / 2-3 分 |
| **全部 H3** | 每镜 `engine=h3`，台词必进 dialogue，环境声进 sfx；机位要能看到嘴部 | 13-15 分 |
| **全部 Wan2.2** | 每镜 `engine=wan22`，**禁止说话/口型特写**，台词只填 dialogue 供烧字幕，用"嘴唇抿紧/别过头/眼神冷下来"等无声表演 | 2-3 分 |

配置位置：`config/prompts.yaml` → `prompts.storyboard_rules.{auto,h3,wan22}`
（想调规则不用改代码，后台「LLM 与提示词」可直接编辑）

### 路由链条（三层，逐层收窄）

```
① 分镜脚本 engine 字段（LLM 按模式拆好）   ← 新增，优先级最高
② routing.yaml rules（对白/人声/动作/空镜） ← 原来就有
③ default_engine: wan22                      ← 兜底
```

`routing.yaml` 新增 `respect_shot_engine: true` 与 `storyboard_engine` 规则。
`router.plan_episode()` **不再按 priority 重排**（之前会把镜头顺序打乱，界面序号乱跳）。

### 验证（线上实跑）

| 模式 | 结果 |
|---|---|
| wan22 | 6 镜全 wan22，有台词的镜头 `prompt` 里是"嘴唇抿紧、眼神冷下来、喉结滚动"，无口型描述，dialogue 保留供字幕 |
| auto | 6 镜 → H3 2 / Wan2.2 4，对白镜头（"妈还会回来吗？"）自动进 H3 |
| h3 | 4 镜全 H3，对白与 sfx 全部落到引擎 |

---

## 2. 全流程中文提示词（问题 ②）

Wan2.2（UMT5-XXL）与 MiniMax H3（Qwen3-VL）都原生支持中文。中文写人物、服装、
情绪、道具比英文更准，还省掉翻译损耗。

| 项 | v0.3 | v0.4 |
|---|---|---|
| 分镜 prompt | 英文 | **中文**（主体+动作+环境+光线+镜头运动+画质） |
| negative | 英文词 | 中文（模糊，变形，水印，低质量，多手多脚，面部扭曲） |
| 角色外貌锚点 | 英文 | 中文（年龄/脸型/发型/体型/服装/标志特征） |
| 视觉风格 | 英文 | 中文 |
| H3 台词注入 | `The character speaks this exact line in Mandarin Chinese: "..."` | `人物用普通话逐字说出这句台词："..."。口型必须精确对应…` |
| Wan2.2 提示词 | 直接用原 prompt | 追加"画面中人物不说话，没有口型动作，无水印无字幕"（防止模型硬凑口型） |

**切换开关**：`config/prompts.yaml` 顶层 `language: zh`（改 `en` 即回退旧英文模板）。

新增模板键（升级不覆盖旧键）：`storyboard_v2` / `baseinfo_v2` / `enhance_v2`，
旧键 `storyboard` / `baseinfo` / `enhance` 保留。

> 顺手修了个坑：模板里有 JSON 花括号示例，`str.format()` 会报 KeyError。
> 已改为只替换 `{命名占位符}` 的正则填充。

---

## 3. 渲染成片流程显式化 + 产物可视化（问题 ③）

### 实际流程（原来只有一句"成片合成中"）

```
① 逐镜渲染（串行，单卡）
       ↓  每个 mp4 长宽/帧率/音轨都不一致
② 统一规格：1080×1920 / 24fps / yuv420p，无音轨补静音  → _work/n_000..n.mp4
       ↓
③ 无损拼接（-c copy）                              → E01_concat.mp4  ← 保留原件
       ↓
④ 两遍 loudnorm 归一到 -16 LUFS（抖音/红果标准）
       ↓
⑤ 烧录字幕（libass，Noto Sans CJK）                → E01_final.mp4
       ↓
⑥ 可选超分（Lanczos 重采样，非 AI 超分）            → E01_final_2k.mp4
```

### 界面上的变化（第 5 步）

- 顶部四段进度徽标：`① 逐镜渲染 → ② 拼接 → ③ 响度+字幕 → ④ 超分`，完成变绿打勾
- 任务进度条按阶段推进（渲染 0-100% → 规格化 → 拼接 25% → 响度 45% → 字幕 62-70% → 超分 80-95%），
  日志逐条显示 `phase/镜号/目标/分辨率/输出`
- 渲染前可选「成片后超分」（不超分 / 1080P / 2K / 4K），选定后整条链路一次跑完
- **成片与超分产物**区块：每个产物显示类型徽标、分辨率、时长、大小、内嵌播放器、
  「⬇ 下载」「⧉ 新窗口打开」「🗑 移除登记」
- 单镜片段区也加了下载按钮

### 产物落库（新表 `artifact`）

```
kind: concat(拼接原件) / final(成片) / upscale(超分) / srt(字幕)
字段: path / width / height / duration / size / target / label / meta
```

`final` 保留最近 5 个、`concat` 3 个、`upscale` 5 个（只清登记，不删磁盘）。

### 编码：NVENC

`settings.assemble.vcodec: auto` → 有 `h264_nvenc` 就用（实测 40 镜 1080×1920
规范化 40 秒完成），否则回退 libx264。

### 修的一个真 bug

西部荒漠 E01 **一句台词都没有**（全 H3 但 dialogue 全空），`srt_from_shots`
生成 0 字节 srt，libass 报 `Unable to open xxx.srt` → 整条成片链路失败。
现在无台词时返回 None 并跳过烧字幕，产物标注"（无台词，无字幕）"。

---

## 4. 跨集渲染队列（问题 ⑥，架构性改动）

### 原来的问题

一集分镜 → 立刻渲染 → 等 1-2 小时 → 才去做下一集分镜。人被 GPU 绑住。

### 现在的流程

```
第1集分镜 → 入队 ┐
第2集分镜 → 入队 ├→ 后台单卡串行消费 → 每集自动跑完 渲染→拼接→响度字幕→超分
第3集分镜 → 入队 ┘   （人继续做分镜，不等 GPU）
```

### 实现

- 新表 `render_queue`：`queued / running / done / failed / canceled`
- 新模块 `pipeline/queue.py`：单消费者线程 + 全局 `worker.LOCK`（绝无并发）
- **启动即恢复**：服务重启时把上次 `running` 的项排回 `queued`，不会幽灵卡住
- **显存预检**：只在该集还有待渲镜头时才检查；占满先 `free_vram()` 再判，
  还满就等 30s（不把任务烧成 failed）。全部渲完只成片的集**不需要**显存。
- 去重：同一集重复入队会合并到已有 queued 项

### 界面

- 顶栏 ⏳ 打开全局队列面板：消费者状态、暂停/恢复、清理已完成、每项进度、
  取消、看日志、下载成片；每 4 秒自动刷新
- 第 4/5 步都有「⏳ 加入队列」与「⏳⏳ 全部集入队」
- 第 4 步还有「⚡ 全剧批量分镜」：所有还没镜头的集按当前模式一次生成完（后台跑）
- 后台「任务日志」页也有队列监控面板

### API

```
GET    /api/queue                队列 + 消费者状态 + 统计
POST   /api/queue                入队 {episode_id} 或 {episode_ids:[]}
POST   /api/queue/all            项目内所有有镜头的集批量入队
PATCH  /api/queue/{qid}          改优先级 / cancel
DELETE /api/queue/{qid}          取消
POST   /api/queue/control        pause / resume / start / clear_finished
POST   /api/projects/{pid}/storyboard-all   全剧批量分镜
```

---

## 5. 角色库独立管理界面（问题 ④）

回答"角色库有没有单独管理界面？需不需要统一管理？"——**原来没有界面，只有导入/存入两个按钮；现在需要且已独立。**

- 顶栏 👤 或第 3 步「👤 角色库管理」打开
- 列表：参考图缩略、中英文名、标签、外貌锚点、声线、**被哪些项目用过**（`used_by`）
- 新增 / 编辑 / 上传参考图 / 删除 / 搜索
- 与项目的关系保持单向：库 → 项目（导入），项目 → 库（存入，同名去重）

为什么独立管理：同一角色会跨多集、多项目复用（西部荒漠有红发女人/拾荒者头目/
孩子），外貌锚点必须一字不差才能保持长相一致；散在各项目里改不齐。

---

## 6. 后台文件预览与下载（问题 ⑤）

原来只有「路径/大小/修改/🗑」，不能看内容。

- 5 个 scope：产物 / 模型 / 输入 / 配置 / **日志**
- 按路径搜索 + 按类型过滤（视频/图片/音频/文本/其它）
- 每行「预览」「下载」「🗑」
- 预览弹窗按类型渲染：`video` 播放器、`image` 图、`audio` 播放器、
  `text` 源码（前 400KB，超出提示下载）、`binary` 提示下载
- 下载走 `Content-Disposition: attachment`，路径越界校验

新增 API：
```
GET /api/admin/file?scope=&path=&download=   单文件预览/下载
GET /api/admin/file/meta?scope=&path=        类型/大小/mtime
GET /api/files/{dir}/{path}?download=true    产物强制下载（inline 可预览）
```

---

## 7. 其他修复

| 项 | 说明 |
|---|---|
| 删镜头断号 | 删镜后自动压缩后面的 seq（1,2,4,5 → 1,2,3） |
| 镜头表顺序 | `plan_episode` 不再按 priority 重排，界面序号稳定 |
| H3 耗时基线 | 10.6/9.0/4.7 → 实测 **15.4/13.0/6.5** 分钟（ETA 之前少算一半） |
| 引擎标签 | 鼠标悬停显示路由原因 |
| 配置迁移 | `scripts/migrate_v04.py`：只补不改、保留注释、写前 YAML 自检 |

---

## 8. 升级步骤（配置受保护，不会冲掉你改过的值）

```bash
cd /home/claw/work/projects/Video     # 或任意持有本仓库的机器
./deploy/sync_deploy.sh               # 代码同步；config/ 只补缺失文件

ssh root@YOUR_SERVER_IP
cd /mnt/disks/nvme/docker/compose/sdf
docker compose build api
docker compose up -d api

# 补 v0.4 新增配置（保留注释，只补缺失项；自动备份）
docker exec sdf-api python /app/migrate_v04.py --config /config --dry-run
docker exec sdf-api python /app/migrate_v04.py --config /config
curl -X POST localhost:8080/api/admin/config/reload
```

回滚：`config/*.bak.YYYYmmdd_HHMMSS` 直接覆盖回去 + 重载；代码 `git checkout v0.3`。

---

## 9. 验证记录（2026-10-22 线上）

| 套件 | 结果 |
|---|---|
| 浏览器回归 v0.4（前台 16 项 + 后台 5 项） | **21/21** |
| 中文分镜：wan22 模式（6 镜） | 全 wan22，无口型描述，台词保留 👍 |
| 中文分镜：auto 模式（6 镜） | H3 2 / 5B 4，对白镜头自动进 H3 👍 |
| 中文分镜：h3 模式（4 镜） | 全 H3，对白+音效齐全 👍 |
| 成片链路（西部荒漠 E01，40 镜 159s） | 规格化 40s → 拼接 3.5s → 响度 20s → 成片 210MB 👍 |
| 跨集队列（重启恢复 + 全部渲完只成片） | 未完成项自动续跑、output 落库 👍 |
| 后台文件预览/下载（mp4 206 Partial Content） | 播放器正常、下载正常 👍 |

---

## 10. 已知问题

| 项 | 状态 |
|---|---|
| AI 超分 | 仍是 Lanczos 重采样；Real-ESRGAN/SeedVR2 未装 |
| H3 16:9 横屏 | 短剧竖屏为主，横屏未调优 |
| 队列优先级 | 只有 priority 字段，界面暂未暴露拖拽排序 |
| Ref2VA 角色一致性 | 权重未下载（+21GB），未接 |
