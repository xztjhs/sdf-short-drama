#!/usr/bin/env bash
# ============================================================
# SDF 栈健康自检（底线守门）
#
# 检查「SDF 必须由 docker compose 栈统一管理」这条底线是否被破坏：
#   1. 栈名必须是 sdf（不是 video / 空）
#   2. sdf-api / sdf-comfyui 必须带 com.docker.compose.project=sdf 标签
#   3. 两个容器必须属于同一个 compose 项目（防"两个独立容器"）
#   4. 栈必须处于 running
#
# 用法：
#   ./deploy/check_stack.sh            # 只看结果
#   ./deploy/check_stack.sh --fix      # 发现游离容器时打印修复命令（不自动执行）
#
# 退出码：0=合规，1=不合规
# ============================================================
set -uo pipefail

REMOTE="${SDF_HOST:-YOUR_SERVER_IP}"
PASSW="${SDF_HOST_PASS:-CHANGE_ME}"
DEST="/mnt/disks/nvme/docker/compose/sdf"
EXPECT_PROJECT="sdf"
EXPECT_CONTAINERS=("sdf-api" "sdf-comfyui")

SSH=(sshpass -p "$PASSW" ssh -o StrictHostKeyChecking=no "root@$REMOTE")

fail=0
ok()   { echo "  ✅ $*"; }
bad()  { echo "  ❌ $*"; fail=1; }
warn() { echo "  ⚠️  $*"; }

echo "=== SDF 栈自检（$REMOTE）==="

# 1) 栈名
project=$("${SSH[@]}" "cd $DEST && docker compose config --format json 2>/dev/null | sed -n 's/.*\"name\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p' | head -1")
if [[ "$project" == "$EXPECT_PROJECT" ]]; then
  ok "栈名 = $project"
else
  bad "栈名 = '${project:-未知}'（应为 $EXPECT_PROJECT）—— compose 文件顶层 name 被改过？"
fi

# 2) compose ls 里能看到 sdf running
ls_line=$("${SSH[@]}" "docker compose ls -a" | awk -v p="$EXPECT_PROJECT" '$1==p {print $2}')
if [[ "$ls_line" == running* ]]; then
  ok "docker compose ls：$EXPECT_PROJECT $ls_line"
else
  bad "docker compose ls 里 $EXPECT_PROJECT 不是 running（实际：${ls_line:-未登记}）"
fi

# 3) 每个容器必须带同一个 compose project 标签
for c in "${EXPECT_CONTAINERS[@]}"; do
  lbl=$("${SSH[@]}" "docker inspect $c --format '{{index .Config.Labels \"com.docker.compose.project\"}}' 2>/dev/null")
  svc=$("${SSH[@]}" "docker inspect $c --format '{{index .Config.Labels \"com.docker.compose.service\"}}' 2>/dev/null")
  if [[ "$lbl" == "$EXPECT_PROJECT" ]]; then
    ok "$c 属于栈 $lbl（service=$svc）"
  else
    bad "$c 的 compose project 标签 = '${lbl:-无}'（应为 $EXPECT_PROJECT）"
    if [[ "${1:-}" == "--fix" ]]; then
      echo "     修复：cd $DEST && docker compose -p $EXPECT_PROJECT up -d --force-recreate $svc"
    fi
  fi
done

# 4) 健康
health=$("${SSH[@]}" "curl -s -m 8 localhost:8080/api/health" 2>/dev/null)
if [[ "$health" == *'"ok":true'* ]]; then
  ok "API 健康：$health"
else
  bad "API 健康检查失败：${health:-无响应}"
fi

echo
if [[ $fail -eq 0 ]]; then
  echo "✅ 合规：SDF 由栈 $EXPECT_PROJECT 统一管理"
  exit 0
fi
echo "❌ 不合规：SDF 未由栈 $EXPECT_PROJECT 统一管理"
echo "   排查：cd $DEST && docker compose -p $EXPECT_PROJECT ps"
echo "   重建：cd $DEST && docker compose -p $EXPECT_PROJECT up -d"
exit 1
