# v0.1 — 首个可用版本

发布日期：2026-10-21
状态：**可运行**（H3 + Wan2.2 双引擎均已真机验证出片）

---

## 这个版本能做什么

| 能力 | 状态 | 实测证据 |
|---|---|---|
| MiniMax H3 文生/图生视频 + 原生立体声 | ✅ | 544p 4.7min / 768p 10.6min，AAC 32kHz 2ch |
| Wan2.2-5B 空镜/动作镜头 | ✅ | 544p 1.3min / 768p 2.6min，快 3.6-4× |
| LLM 自动分镜（icompify） | ✅ | deepseek-v4-flash，8 镜 16.7s |
| 镜头路由（H3 vs 5B 自动决策） | ✅ | 按对白/音效/动作/空镜关键词 |
| 串行渲染 + 三档重试 + 断点续跑 | ✅ | e2e 验证 |
| ffmpeg 成片（规范化/拼接/响度/字幕） | ✅ | 1080×1920 H.264+AAC 2ch，中文字幕烧录确认 |
| Web 界面（分镜/镜头/渲染/成片） | ✅ | HTTP 200，API 全通 |
| 单镜重渲染 | ✅ | 后端就绪 |
| **整集自动成片** | ✅ | 4 镜 14.7min，1080×1920 H.264+AAC，字幕正确 |

## 未完成 / 已知短板

| 项 | 说明 |
|---|---|
| ~~整集完整链路~~ | ✅ **已通过**（2026-10-21 17:26）：4 镜 2H3+2Wan2.2 → 自动成片 14.7min |
| 角色一致性（Ref2VA） | 权重未下载（+21GB），代码未接 |
| 2K 输出 | 开源权重只到 768 短边，做不到 |
| 并发渲染 | 设计为单卡串行，不做并发 |
| swap | **未配置**，OOM 时进程会被静默杀掉 |
| 内存 | 64GB（建议 128GB），渲染时可用约 30GB |

---

## 整集端到端实测（2026-10-21 17:26）

项目 `prj_bd501c7c17bf`，4 镜，全部 768×1344：

| 镜 | 引擎 | 耗时 | 说明 |
|---|---|---|---|
| 1 | Wan2.2 | 82s | 空镜/远景 |
| 2 | H3 | 340s | 有对白 → 原生音频 |
| 3 | H3 | 341s | 有音效 → 原生音频 |
| 4 | Wan2.2 | 88s | 空镜/远景 |

**总耗时 14.7 分钟**，产出 `E01_final.mp4`：1080×1920 / 24fps / 11.84s / 3.77MB / H.264+AAC 2ch 48kHz。
字幕烧录确认有效（抽帧核对，中文字体正常、位置正确）。

### 发现的画质问题

**Wan2.2 在夜景/低光场景明显欠曝。** 同一提示词（"Empty street at night, rain reflecting neon signs"）
H3 能出正常亮度的夜景，Wan2.2 出的偏黑，细节丢得多。

**缓解办法**：
- 提示词加 `well lit` / `bright neon lighting` / `visible details in shadows`
- 或夜景镜头优先路由给 H3
- 或后期 ffmpeg `eq=brightness=0.05:contrast=1.05` 提亮

当前 `router.py` 未针对低光场景做特殊处理，属已知短板。

## 硬件基线

| 项 | 值 |
|---|---|
| GPU | RTX A5000 24GB（Ampere sm_86，768 GB/s） |
| CPU | Threadripper 2990WX 32C/64T |
| 内存 | 64GB DDR4（空 4 个 DIMM 槽） |
| 存储 | NVMe 1.9T |
| 系统 | Unraid 7.3.2 / Docker 29.5.3 |
| 运行时 | ComfyUI 0.42.1 / PyTorch 2.14.1+cu126 |

**实测资源**：768p 渲染时显存峰值 22.1/24.5GB（90%），温度 85°C，功耗 227W。

---

## 实测性能

### 单镜耗时

| 引擎 | 分辨率 | 5s 片段 |
|---|---|---|
| H3 | 544×960 | 4.7 min |
| Wan2.2 | 544×960 | 1.3 min |
| H3 | 768×1344 | 10.6 min |
| Wan2.2 | 768×1344 | 2.6 min |

### 产能（单卡）

一集 40 镜 / 90 秒，混合 768p（16 H3 + 24 5B）≈ **3.9 小时**，日产能约 **1.4 集**（含重试）。

---

## 部署

```bash
# 1. 环境自检
./deploy/check_env.sh

# 2. 拉模型（H3 42.5GB + Wan2.2 18.1GB，ModelScope ~40MB/s）
./deploy/pull_models.sh
./deploy/pull_wan22.sh

# 3. 配 key
cp .env.example .env && vi .env      # 填 ICOMPIFY_API_KEY

# 4. 起栈
docker compose up -d

# 5. 冒烟
./deploy/smoke_test.sh

# 6. 开界面
open http://YOUR_SERVER_IP:8080
```

---

## 关键设计决策

1. **H3 + Wan2.2 混合**，不是纯 H3。H3 不可替代的只有「一次出画面+对白+音效」，其余场景它只是慢 4 倍。
2. **单卡串行**，不做并发。24GB 上 H3 峰值 22GB，并发必 OOM。
3. **LLM 走远程**（icompify），本地 GPU 全部留给扩散模型。
4. **不用容器 healthcheck**。本机 dockerd 侧 runc exec 必失败，会误判 unhealthy。
5. **模型只走 ModelScope**。HF 实测 300-550KB/s，不可用。

---

## 踩过的坑（全部已修）

详见 `docs/06-踩坑记录.md`，共 16+ 条，其中最关键的：

- H3 节点签名与网上教程不符（无 negative/turbo_mode，必须 BasicGuider）
- `CLIPLoader` type 是 `minimax` 不是 `minimax_h3`
- 24GB 上必须 `--disable-pinned-memory`，否则 host RAM OOM 静默杀进程
- `deepseek-v4-pro` 是推理模型，max_tokens 小时 content 返回 None
- 同图 OOM 一次后永远 OOM（坏预算被缓存），需改参数才能恢复

---

## 合规提醒

- **H3 License**：免版税商用，但排除美/欧/英/韩；商用 UI 需显示 "MiniMax H3"；不得用输出训练其它模型
- **国内**：AI 生成内容需显式+隐式标识（2025-09-01 起）
- 「本地无审查」≠「可以发违规内容」

---

## 文件清单

```
Video/
├── docker-compose.yml
├── .env.example
├── deploy/           check_env / pull_models / pull_wan22 / sync_deploy / smoke_test
├── pipeline/         db / comfy / workflow / router / script / worker / assemble
├── web/              app.py + static/index.html + Dockerfile
├── scripts/          smoke_render / e2e_test / test_storyboard / test_assemble / test_episode / diag_route
├── docs/             01-08 共 8 本手册
└── notes/            实测基准与工作记录
```
