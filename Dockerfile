# homeassistant-xiaozhiserver — 小智 MCP 接入点 bridge 镜像
# 内容：stdio MCP server + WebSocket 桥接进程；无 HTTP 端口。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY homeassistant_xiaozhiserver ./homeassistant_xiaozhiserver
COPY server.py ./server.py
COPY mcp_pipe.py ./mcp_pipe.py

RUN useradd -m -u 10001 appuser
USER appuser

# HA_URL / HA_TOKEN / MCP_ENDPOINT 由运行时注入，不写入镜像
ENV HA_TIMEOUT=10 \
    HA_VERIFY_SSL=true \
    LOG_LEVEL=INFO \
    WEBUI_ENABLED=false \
    WEBUI_PORT=8099

# 可选 WebUI 监听端口（默认不启动服务，仅声明镜像元数据）
EXPOSE 8099

CMD ["python", "/app/mcp_pipe.py", "/app/server.py"]
