#!/bin/sh
# 单容器入口：WebUI 后台运行，MCP WebSocket bridge 前台运行。
set -u

python -m homeassistant_xiaozhiserver.webui &
WEBUI_PID=$!

python /app/mcp_pipe.py /app/server.py &
BRIDGE_PID=$!

cleanup() {
    kill -TERM "$BRIDGE_PID" "$WEBUI_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
    wait "$WEBUI_PID" 2>/dev/null || true
}

trap 'cleanup; exit 0' TERM INT

wait "$BRIDGE_PID"
STATUS=$?
cleanup
exit "$STATUS"
