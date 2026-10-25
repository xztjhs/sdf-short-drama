#!/usr/bin/env bash
# ============================================================
# H3 模型拉取（ModelScope，国内 25MB/s）
# 用法： ./pull_models.sh [--all|--minimal|--ref]
#   --minimal  4 文件最小集（T2V/I2V，42.5GB）  [默认]
#   --ref      加 Ref2VA（角色一致性，+21GB）
#   --all      全量含超分/LoRA 合集
# ============================================================
set -uo pipefail

BASE="https://modelscope.cn/models/Comfy-Org/MiniMax-H3/resolve/master"
MODE="${1:---minimal}"
REMOTE="${SDF_HOST:-YOUR_SERVER_IP}"
PASSW="${SDF_HOST_PASS:-CHANGE_ME}"
ROOT="/mnt/disks/nvme/docker/compose/sdf/models"

case "$MODE" in
  --ref|--all)
    FILES=(
      "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"
      "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors"
      "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
      "vae/minimax_h3_video_vae_fp16.safetensors"
      "vae/minimax_h3_audio_vae_fp32.safetensors"
      "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
      "loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
    ) ;;
  *)
    FILES=(
      "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"
      "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
      "vae/minimax_h3_video_vae_fp16.safetensors"
      "vae/minimax_h3_audio_vae_fp32.safetensors"
      "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
    ) ;;
esac

# Wan2.2-5B（空镜/动作轨道）—— 默认一起拉，不是可选
echo "模式: $MODE   文件数: ${#FILES[@]}  (+ Wan2.2-5B 3 个)"
echo "目标: $REMOTE:$ROOT"
echo

# 生成远端脚本
GEN=$(mktemp)
{
  echo '#!/bin/bash'
  echo "BASE='$BASE'"
  echo "ROOT='$ROOT'"
  echo 'echo "[$(date +%H:%M:%S)] === start ==="'
  for f in "${FILES[@]}"; do
    echo "mkdir -p \"\$ROOT/\$(dirname '$f')\""
    echo "if [ -f \"\$ROOT/$f\" ]; then echo \"[skip] $f \$(du -h \"\$ROOT/$f\"|cut -f1)\"; else"
    echo "  echo \"[$(date +%H:%M:%S)] GET $f\""
    echo "  curl -sL --retry 5 --retry-delay 3 -C - -o \"\$ROOT/$f.part\" \"\$BASE/$f\" \\"
    echo "    && mv \"\$ROOT/$f.part\" \"\$ROOT/$f\" && echo \"[done] $f \$(du -h \"\$ROOT/$f\"|cut -f1)\" \\"
    echo "    || echo \"[FAIL] $f\""
    echo "fi"
  done
  echo 'echo "[$(date +%H:%M:%S)] === all done ==="'
  echo 'du -sh "$ROOT"/*'
} > "$GEN"

sshpass -p "$PASSW" scp -o StrictHostKeyChecking=no "$GEN" "root@${REMOTE}:/tmp/pull_h3.sh" >/dev/null
rm -f "$GEN"
sshpass -p "$PASSW" ssh -o StrictHostKeyChecking=no "root@${REMOTE}" \
  'chmod +x /tmp/pull_h3.sh; mkdir -p /mnt/disks/nvme/docker/compose/sdf/logs; nohup /tmp/pull_h3.sh > /mnt/disks/nvme/docker/compose/sdf/logs/model_download.log 2>&1 & echo "后台下载已启动 PID=$!"'

echo
echo "查看进度："
echo "  ssh root@${REMOTE} 'tail -f /mnt/disks/nvme/docker/compose/sdf/logs/model_download.log'"
echo
echo "Wan2.2-5B 单独拉（另一模型仓库）："
echo "  ssh root@${REMOTE} 'nohup /tmp/dl_wan22.sh > /mnt/disks/nvme/docker/compose/sdf/logs/wan22_download.log 2>&1 &'"
