#!/usr/bin/env bash
# ============================================================
# SDF 栈环境自检 —— 只读，不改任何东西
# 用法： ./check_env.sh [--remote]
# ============================================================
set -uo pipefail

REMOTE_HOST="${SDF_HOST:-YOUR_SERVER_IP}"
REMOTE="${REMOTE_HOST}"
PASS=0; WARN=0; FAIL=0

c_pass(){ echo "  ✅ $*"; PASS=$((PASS+1)); }
c_warn(){ echo "  ⚠️  $*"; WARN=$((WARN+1)); }
c_fail(){ echo "  ❌ $*"; FAIL=$((FAIL+1)); }

run(){ sshpass -p "${SDF_HOST_PASS:-CHANGE_ME}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@${REMOTE}" "$@"; }

echo "=========================================="
echo " SDF 栈环境自检  $(date '+%F %T')"
echo " 目标: ${REMOTE}"
echo "=========================================="

echo
echo "【1】GPU"
GPU=$(run 'nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu,driver_version --format=csv,noheader' 2>&1)
if [[ "$GPU" == *"A5000"* ]]; then
  c_pass "GPU: $GPU"
else
  c_fail "GPU 异常: $GPU"
fi
VRAM_USED=$(echo "$GPU" | awk -F', ' '{gsub(/ MiB/,"",$3); print $3}')
if [[ -n "${VRAM_USED:-}" ]] && [[ "$VRAM_USED" -lt 2000 ]]; then
  c_pass "显存空闲 ${VRAM_USED}MiB —— 可渲染"
else
  c_warn "显存已占 ${VRAM_USED:-?}MiB —— 渲染前需释放（停 RAGFlow 等）"
fi

echo
echo "【2】内存（24GB 卡跑 H3 的真实瓶颈）"
MEM=$(run "free -g | awk 'NR==2{print \$2,\$3,\$7}'" 2>&1)
TOT=$(echo "$MEM"|awk '{print $1}'); AVAIL=$(echo "$MEM"|awk '{print $3}')
if [[ -n "${AVAIL:-}" ]] && [[ "$AVAIL" -ge 36 ]]; then
  c_pass "可用内存 ${AVAIL}GB / 总 ${TOT}GB —— 满足 H3 要求（≥36GB）"
elif [[ -n "${AVAIL:-}" ]] && [[ "$AVAIL" -ge 24 ]]; then
  c_warn "可用内存 ${AVAIL}GB —— 只能跑短片，长片有 OOM 风险"
else
  c_fail "可用内存 ${AVAIL:-?}GB —— 不够，必须加内存到 128GB 或先停其它栈"
fi
SWAP=$(run "free -g | awk 'NR==3{print \$2}'" 2>&1)
if [[ "${SWAP:-0}" -eq 0 ]]; then
  c_warn "无 swap —— 建议在 NVMe 上挂 64GB swap 兜底（否则 OOM 直接杀进程，无报错）"
else
  c_pass "swap ${SWAP}GB"
fi

echo
echo "【3】磁盘"
DISK=$(run "df -h /mnt/disks/nvme | tail -1 | awk '{print \$4}'" 2>&1)
c_pass "NVMe 可用: ${DISK}"
NEED_MODEL=$(run 'du -sh /mnt/disks/nvme/docker/compose/sdf/models 2>/dev/null | cut -f1' 2>&1)
echo "     当前模型占用: ${NEED_MODEL:-0}（全套约 42GB）"

echo
echo "【4】Docker"
DK=$(run 'docker --version' 2>&1)
if [[ "$DK" == *"version"* ]]; then c_pass "$DK"; else c_fail "docker 不可用: $DK"; fi
RT=$(run "docker info 2>/dev/null | grep -c nvidia" 2>&1)
if [[ "${RT:-0}" -ge 1 ]]; then c_pass "nvidia runtime 已注册"; else c_fail "nvidia runtime 缺失"; fi
GPUTEST=$(run 'timeout 30 docker run --rm --gpus all alpine echo GPU_OK 2>&1 | tail -1' 2>&1)
if [[ "$GPUTEST" == *"GPU_OK"* ]]; then c_pass "容器内 GPU 直通正常"; else c_warn "容器 GPU 直通未验证: $GPUTEST"; fi

echo
echo "【5】端口"
for p in 8188 8080; do
  OCC=$(run "ss -lnt | grep -c ':$p '" 2>&1)
  if [[ "${OCC:-0}" -gt 0 ]]; then c_warn "端口 $p 已被占用"; else c_pass "端口 $p 空闲"; fi
done

echo
echo "【6】模型完整性"
run 'cd /mnt/disks/nvme/docker/compose/sdf/models 2>/dev/null || exit 0
for f in diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors vae/minimax_h3_video_vae_fp16.safetensors vae/minimax_h3_audio_vae_fp32.safetensors; do
  if [ -f "$f" ]; then printf "  ✅ %-70s %s\n" "$f" "$(du -h "$f"|cut -f1)"; else printf "  ⬜ %-70s 缺失\n" "$f"; fi
done' 2>&1

echo
echo "=========================================="
printf " 通过 %d   警告 %d   失败 %d\n" "$PASS" "$WARN" "$FAIL"
echo "=========================================="
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
