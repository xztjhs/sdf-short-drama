#!/usr/bin/env bash
# ============================================================
# 同步 SDF 项目到 .253 并重建 api
# 用法： ./sync_deploy.sh [--rebuild] [--sync-config]
#
# ⚠️ config/ 目录策略（2026-10-24 修正）：
#   旧做法是 rsync --ignore-existing（只补缺失文件），本意是保护界面改过的值，
#   但结果连正常发布也被挡住 —— 服务器配置长期停在 v0.2 基线，
#   11 个风格预设、storyboard_v2 模板、assemble.subtitle 全都没生效（Bug4/5b/功能2 假完成）。
#
#   现在改为「合并式发布」：./publish_config.py
#     · 内容库（风格 render / 提示词模板）以仓库为准
#     · 运行时参数（画布/帧率/交付档/字幕样式/监控栏）保留服务器值
#     · settings.yaml 走文本补丁，保住运维注释
#     · 写前服务器留 .bak.<时间戳>，写后 YAML 自检
#
#   配置默认**不随代码自动发布**（避免误盖线上参数）。
#   要发布配置显式加 --sync-config，或单独跑 ./deploy/publish_config.py。
# ============================================================
set -euo pipefail

REMOTE="${SDF_HOST:-YOUR_SERVER_IP}"
PASSW="${SDF_HOST_PASS:-CHANGE_ME}"
DEST="/mnt/disks/nvme/docker/compose/sdf"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
SYNC_CONFIG=0
REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=1 ;;
    --sync-config) SYNC_CONFIG=1 ;;
  esac
done

echo "同步 $SRC → $REMOTE:$DEST"

sshpass -p "$PASSW" ssh -o StrictHostKeyChecking=no "root@$REMOTE" \
  "mkdir -p $DEST/{data,logs,input,output,models,workflows,config}"

# 1) 代码/文档：完全同步（远端这些是只读产物，可以覆盖）
# ⚠️ 安全须知：rsync -az --delete 会删除远端“源里没有”的任何文件/目录。
# 因此**所有运行时数据目录必须显式排除**，包括后来新建的：
#   models / models_backup（模型归档）/ output / input / data / logs / config / workflows
# 2026-10-22 事故：models_backup 没在排除列表里，一次部署把刚归档的 18GB
# wan2.2 权重删掉了（幸好 sha256 清单已先算出）。新增目录务必同步加到这里。
sshpass -p "$PASSW" rsync -az --delete \
  --exclude='.git' --exclude='models' --exclude='models_backup' \
  --exclude='output' --exclude='workflows' \
  --exclude='data' --exclude='logs' --exclude='input' \
  --exclude='config' \
  --exclude='__pycache__' --exclude='.venv' --exclude='*.pyc' \
  "$SRC/" "root@$REMOTE:$DEST/"

# 2) 配置：补齐镜像内种子（首次部署用），不改运行时 /config
echo "配置：仅补齐缺失文件（不覆盖远端已有配置）"
sshpass -p "$PASSW" rsync -a --ignore-existing \
  --exclude='secrets.env' --exclude='*.bak.*' \
  "$SRC/config/" "root@$REMOTE:$DEST/config/"

# 2b) 合并式发布运行时配置（显式开关，避免误盖线上参数）
if [[ "$SYNC_CONFIG" == "1" ]]; then
  echo "配置：合并式发布（内容库以仓库为准，运行时参数保留服务器值）"
  python3 "$SRC/deploy/publish_config.py"
fi

echo "✅ 同步完成"

if [[ "$REBUILD" == "1" ]]; then
  echo "重建 api 镜像..."
  sshpass -p "$PASSW" ssh -o StrictHostKeyChecking=no "root@$REMOTE" \
    "cd $DEST && docker compose build api && docker compose up -d"
  echo "✅ 重建完成"
fi
