#!/usr/bin/env bash
# 一键离线自测：启动 fake HA + 本服务 + 端到端客户端，结束后自动清理。
# 用法：export TEST_TOKEN=任意本地测试口令 && bash tests/run_local_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-python}
: "${TEST_TOKEN:?请先 export TEST_TOKEN=任意本地测试口令（模拟器与本服务共用）}"

export HA_URL=http://127.0.0.1:8123
export HA_TOKEN="$TEST_TOKEN"
export FAKE_HA_TOKEN="$TEST_TOKEN"
# 注册表走 WebSocket（fake 的 WS 端口默认 8124）
export HA_WS_URL=ws://127.0.0.1:8124/api/websocket
export FAKE_HA_WS_PORT=8124
export MCP_PORT=8090
export MCP_TRANSPORT=streamable-http

FAKE=""
SRV=""
cleanup() {
  for pid in "$SRV" "$FAKE"; do
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$SRV" "$FAKE"; do
    if [ -n "$pid" ]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

"$PY" tests/fake_ha.py & FAKE=$!
sleep 1.5
"$PY" -m homeassistant_xiaozhiserver & SRV=$!

# 等待 MCP/ready 就绪（HA 可达），而不是固定 sleep（最多 15s）
READY=0
for _ in $(seq 1 60); do
  if "$PY" - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8090/ready", timeout=1)
PY
  then
    READY=1
    break
  fi
  sleep 0.25
done
if [ "$READY" != "1" ]; then
  echo "服务未在预期时间内就绪（/ready 不可达或 HA 不可达）" >&2
  exit 1
fi

echo "===== e2e ====="
RC=0
"$PY" tests/e2e_mcp_client.py || RC=$?
echo "===== health ====="
if command -v curl >/dev/null 2>&1; then echo -n "health: "; curl -s "http://127.0.0.1:8090/health"; echo; echo -n "ready: "; curl -s "http://127.0.0.1:8090/ready"; echo; fi

exit ${RC}
