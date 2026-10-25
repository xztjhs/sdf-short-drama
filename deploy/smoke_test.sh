#!/usr/bin/env bash
# ============================================================
# 冒烟测试 —— 验证栈是否真的能出片
# 用法： ./smoke_test.sh [--quick|--full]
#   --quick  3 秒 544×960 单镜（约 2-3 分钟）
#   --full   5 秒 768×1344 带音频（约 6-8 分钟）
# ============================================================
set -uo pipefail

MODE="${1:---quick}"
HOST="${SDF_HOST:-YOUR_SERVER_IP}"
PASSW="${SDF_HOST_PASS:-CHANGE_ME}"

run(){ sshpass -p "$PASSW" ssh -o StrictHostKeyChecking=no "root@$HOST" "$@"; }

echo "=========================================="
echo " SDF 栈冒烟测试  $(date '+%F %T')  模式=$MODE"
echo "=========================================="

echo
echo "【1】容器状态"
run "docker ps --filter name=sdf- --format 'table {{.Names}}\t{{.Status}}'"

echo
echo "【2】ComfyUI 可达性"
if run "curl -sf http://localhost:8188/system_stats >/dev/null"; then
  echo "  ✅ ComfyUI 在线"
  run "curl -s http://localhost:8188/system_stats | head -c 300"
  echo
else
  echo "  ❌ ComfyUI 无响应"
  echo "     排查: docker compose logs --tail=50 comfyui"
  exit 1
fi

echo
echo "【3】H3 节点是否存在（关键）"
NODES=$(run "curl -s http://localhost:8188/object_info" 2>/dev/null)
for n in MiniMaxH3ImageToVideo BasicGuider VAEDecodeAudio CreateVideo SaveVideo; do
  if echo "$NODES" | grep -q "\"$n\""; then
    echo "  ✅ $n"
  else
    echo "  ❌ $n 缺失 —— ComfyUI 版本太老，需 ≥0.30 且更新到含 H3 的版本"
  fi
done

echo
echo "【4】模型文件"
run "cd /mnt/disks/nvme/docker/compose/sdf/models && for f in \
  diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors \
  text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors \
  vae/minimax_h3_video_vae_fp16.safetensors \
  vae/minimax_h3_audio_vae_fp32.safetensors \
  loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors; do
    [ -f \"\$f\" ] && printf '  ✅ %-62s %s\n' \"\$f\" \"\$(du -h \"\$f\"|cut -f1)\" || printf '  ❌ %-62s 缺失\n' \"\$f\"
  done"

echo
echo "【5】显存与内存"
run "nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader | sed 's/^/  GPU: /'"
run "free -h | sed -n '2,3p' | sed 's/^/  /'"

echo
echo "【6】静态工作流自检"
run "cd /mnt/disks/nvme/docker/compose/sdf && docker exec sdf-api python -m pipeline.workflow 2>&1 | sed 's/^/  /'"

if [[ "$MODE" == "--full" ]]; then
  RES="768x1344"; DUR=5; STEPS=8
else
  RES="544x960"; DUR=3; STEPS=8
fi

echo
echo "【7】真机渲染 ${RES} ${DUR}s ${STEPS}步"
echo "     （首次运行会 JIT 编译内核，比后续慢 30-50%）"
echo

run "docker exec sdf-api python - <<'PY'
import time, json
from pipeline import comfy, workflow

print('清理显存...')
comfy.free_vram()
time.sleep(2)

print(f'构造工作流 {RES} {DUR}s {STEPS}步')
wf = workflow.build_h3(
    prompt='A 26-year-old Chinese woman with long black hair stands on a rooftop at dusk, '
           'city skyline behind her, wind blowing her hair, warm golden hour light, '
           'medium shot, slow push in, cinematic, film grain, highly detailed',
    resolution='$RES', duration=$DUR, steps=$STEPS, seed=20261021)
print(f'节点数: {len(wf)}')

probs = workflow.validate(wf)
if probs:
    print('⚠️ 自检告警:', probs)

t0 = time.time()
pid = comfy.submit(wf)
print(f'已提交 prompt_id={pid}')

def prog(e):
    print(f\"  → node={e.get('node')} elapsed={e.get('elapsed')}s\")

h = comfy.wait(pid, timeout=2400, on_progress=prog)
el = time.time() - t0
outs = comfy.outputs_of(h)
print()
print(f'✅ 完成，耗时 {el/60:.1f} 分钟')
print('产物:', json.dumps(outs, ensure_ascii=False)[:600])
PY" 2>&1 | sed 's/^/  /'

echo
echo "【8】产物检查"
run "ls -lah /mnt/disks/nvme/docker/compose/sdf/output/ 2>/dev/null | tail -10 | sed 's/^/  /'"

echo
echo "【9】音频流验证（H3 必须带 stereo AAC）"
LATEST=$(run "find /mnt/disks/nvme/docker/compose/sdf/output -name '*.mp4' -newermt '-30 minutes' 2>/dev/null | head -1" | tr -d '\r')
if [[ -n "$LATEST" ]]; then
  echo "  检查: $LATEST"
  run "docker exec sdf-api ffprobe -v error -show_streams -select_streams a -of default=noprint_wrappers=1 '$LATEST' 2>&1 | head -12 | sed 's/^/    /'"
else
  echo "  ⚠️ 未找到新产物"
fi

echo
echo "=========================================="
echo " 冒烟测试结束"
echo "=========================================="
