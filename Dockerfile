# 小智 MCP 服务（Home Assistant 桥）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝源码
COPY ha_client.py server.py ./

# 以非 root 用户运行
RUN useradd -m -u 10001 appuser
USER appuser

# 运行时默认值（HA_URL / HA_TOKEN 必须由外部注入，不写入镜像）
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8090 \
    MCP_PATH=/mcp \
    HA_TIMEOUT=10 \
    HA_VERIFY_SSL=true \
    LOG_LEVEL=INFO

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('MCP_PORT','8090')+'/health', timeout=3)" || exit 1

CMD ["python", "server.py"]
