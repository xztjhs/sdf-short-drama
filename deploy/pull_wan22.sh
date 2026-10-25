#!/bin/bash
# ============================================================
# Wan2.2 TI2V-5B 模型拉取（ModelScope）
# 这是「空镜/动作」轨道的引擎，比 H3 快 4 倍，无音轨
# 用法： ./pull_wan22.sh
# ============================================================
set -uo pipefail

REMOTE="${SDF_HOST:-YOUR_SERVER_IP}"
PASSW="${SDF_HOST_PASS:-CHANGE_ME}"
ROOT="/mnt/disks/nvme/docker/compose/sdf/models"
BASE="https://modelscope.cn/models/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/master"

FILES=(
  "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors|diffusion_models/wan2.2_ti2v_5B_fp16.safetensors|10.0"
  "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors|text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors|6.7"
  "split_files/vae/wan2.2_vae.safetensors|vae/wan2.2_vae.safetensors|1.4"
)

echo "Wan2.2-5B 共 3 个文件，约 18.1GB"
echo "目标: $REMOTE:$ROOT"

GEN=$(mktemp)
{
  echo '#!/bin/bash'
  echo "BASE='$BASE'"
  echo "ROOT='$ROOT'"
  echo 'echo "[$(date +%H:%M:%S)] === wan22 start ==="'
  for entry in "${FILES[@]}"; do
    IFS='|' read -r remote localp gb <<< "$entry"
    echo "mkdir -p \"\$ROOT/\$(dirname '$localp')\""
    echo "if [ -f \"\$ROOT/$localp\" ]; then echo \"[skip] $localp\"; else"
    echo "  echo \"[$(date +%H:%M:%S)] GET $localp ($gb GB)\""
    echo "  curl -sL --retry 5 --retry-delay 3 -C - -o \"\$ROOT/$localp.part\" \"\$BASE/$remote\" \\"
    echo "    && mv \"\$ROOT/$localp.part\" \"\$ROOT/$localp\" && echo \"[done] $localp \$(du -h \"\$ROOT/$localp\"|cut -f1)\" \\"
    echo "    || echo \"[FAIL] $localp\""
    echo "fi"
  done
  echo 'echo "[$(date +%H:%M:%S)] === wan22 done ==="'
  echo 'du -sh "$ROOT"/*'
} > "$GEN"

sshpass -p "$PASSW" scp -o StrictHostKeyChecking=no "$GEN" "root@${REMOTE}:/tmp/dl_wan22.sh" >/dev/null
rm -f "$GEN"
sshpass -p "$PASSW" ssh -o StrictHostKeyChecking=no "root@${REMOTE}" \
  'chmod +x /tmp/dl_wan22.sh; mkdir -p /mnt/disks/nvme/docker/compose/sdf/logs; setsid nohup /tmp/dl_wan22.sh > /mnt/disks/nvme/docker/compose/sdf/logs/wan22_download.log 2>&1 < /dev/null & echo "已启动"'

echo
echo "进度： ssh root@${REMOTE} 'tail -f /mnt/disks/nvme/docker/compose/sdf/logs/wan22_download.log'"
