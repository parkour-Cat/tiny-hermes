#!/usr/bin/env bash
# 把整套栈重建到当前工作树的代码，并**验证每个容器真的换了代码**。
#
# 存在的理由：这套栈已经因为「只重建了一部分」坏过两次，而症状都不是报错——
# 是某个组件安静地跑着旧代码，别的组件全对，于是看起来像功能没实现。
# 最后一次是 scheduler 跑着早于整条分支的镜像，命令识别、撤回、写回执全对，
# 唯独没有人把回执发出去。
#
# 用法：deploy/compose/redeploy.sh [验证用的符号 ...]
#   例：deploy/compose/redeploy.sh pending_command_receipts
set -euo pipefail
cd "$(dirname "$0")"

# `env -u`：shell 里的这些变量会盖过 --env-file，而开发机上它们常常指向别处。
# `--env-file`：不带它 SANDBOX_IMAGE_DIGEST 会是空的，等于「不批准任何沙箱镜像」，
# 于是每个带工具的 Run 都以 sandbox_not_configured 失败。
DC=(env -u EGRESS_PROXY_URL -u EGRESS_PROXY_TOKEN -u DATABASE_URL -u TEST_DATABASE_URL
    docker compose --env-file ../../.env)

echo "==> build（全部，不挑）"
"${DC[@]}" build
echo "==> up --force-recreate（全部）"
"${DC[@]}" up -d --force-recreate

echo "==> 验证：每个服务的代码版本"
FAIL=0
for S in api worker scheduler controller; do
  GIT=$("${DC[@]}" exec -T "$S" sh -c 'cat /app/GIT_SHA 2>/dev/null || echo unknown' 2>/dev/null | tr -d '\r')
  printf '  %-11s git=%s\n' "$S" "${GIT:-unknown}"
  for SYM in "$@"; do
    N=$("${DC[@]}" exec -T "$S" sh -c "grep -rl '$SYM' /app/packages/backend/src 2>/dev/null | wc -l" 2>/dev/null | tr -d ' \r')
    if [ "${N:-0}" -eq 0 ]; then
      echo "     !! $S 里找不到 $SYM —— 这个容器跑的是旧代码"
      FAIL=1
    fi
  done
done

DIGEST=$("${DC[@]}" exec -T worker sh -c 'echo "$SANDBOX_IMAGE_DIGEST"' 2>/dev/null | tr -d '\r')
if [ -z "$DIGEST" ]; then
  echo "     !! SANDBOX_IMAGE_DIGEST 是空的 —— 带工具的 Run 会全部 sandbox_not_configured"
  FAIL=1
else
  echo "  sandbox digest ok"
fi

[ "$FAIL" -eq 0 ] || { echo "==> 部署没通过验证"; exit 1; }
echo "==> 全部服务代码一致"
