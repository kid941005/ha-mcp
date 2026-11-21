# -*- coding: utf-8 -*-
"""可选 WebUI：状态查看、工具调试台、配置只读。

默认关闭。仅在 WEBUI_ENABLED=true 且设置了 WEBUI_TOKEN 时启动：
    python -m homeassistant_xiaozhiserver.webui

安全约定：
- 所有请求必须携带 WEBUI_TOKEN（HTTP Basic 密码或 Bearer Token）；
- HA_TOKEN、MCP_ENDPOINT 的令牌只做掩码展示，绝不回传明文；
- 工具名白名单，只允许调用 server.py 暴露的六个工具。
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from . import server as mcp_server
from .client import HAError
from .config import (
    DEFAULT_LIST_LIMIT,
    LOG_LEVEL,
    MAX_LIST_LIMIT,
    WEBUI_ENABLED,
    WEBUI_HOST,
    WEBUI_PORT,
    WEBUI_TOKEN,
)
from .logging_setup import setup_logging

log = logging.getLogger("homeassistant-xiaozhiserver.webui")

_STARTED_AT = time.time()

# 工具白名单：名称 -> (说明, 参数示例)
_TOOLS: list[tuple[str, str, str]] = [
    ("list_devices", "列出/查询设备（keyword/domain/area/分页）", '{"keyword": "客厅"}'),
    ("get_device_state", "查询单个设备状态", '{"entity": "客厅灯"}'),
    ("control_device", "控制设备（危险设备需 confirm_token）", '{"entity": "客厅灯", "action": "turn_on"}'),
    ("list_services", "查询某类设备支持的服务与参数", '{"domain": "light"}'),
    ("call_service", "调用任意 HA 服务", '{"domain": "light", "service": "turn_on", "data": {"entity_id": "light.living_room"}}'),
    ("list_domains", "列出设备类型", "{}"),
]
_TOOL_FUNCS = {
    name: getattr(mcp_server, name)
    for name, _, _ in _TOOLS
    if hasattr(mcp_server, name)
}


def _mask_secret(value: str) -> str:
    return "已设置（掩码）" if value else "（未设置）"


def _mask_url(raw: str) -> str:
    if not raw:
        return "（未设置）"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "（格式无法解析）"
    if not parts.scheme or not parts.netloc:
        return "（格式无法解析）"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _mask_endpoint(raw: str) -> str:
    if not raw:
        return "（未设置）"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "（格式无法解析）"
    if not parts.scheme or not parts.netloc:
        return "（格式无法解析）"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    query = "&".join(
        key + "=***" if key.lower() in ("token", "key", "secret", "password") else key + "=" + value
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _provided_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    lowered = header.lower()
    if lowered.startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        _user, sep, password = decoded.partition(":")
        return password if sep else None
    if lowered.startswith("bearer "):
        return header[7:].strip()
    return None


def create_app(token: str | None = None) -> FastAPI:
    """构建 WebUI 应用；token 为空时使用环境中的 WEBUI_TOKEN。"""
    expected = (token if token is not None else WEBUI_TOKEN) or ""

    async def require_auth(request: Request) -> None:
        if not expected:
            raise HTTPException(status_code=500, detail="WEBUI_TOKEN 未配置，WebUI 拒绝服务")
        provided = _provided_token(request)
        if not provided or not secrets.compare_digest(provided, expected):
            raise HTTPException(
                status_code=401,
                detail="需要 WEBUI_TOKEN 鉴权",
                headers={"WWW-Authenticate": 'Basic realm="homeassistant-xiaozhiserver"'},
            )

    app = FastAPI(title="homeassistant-xiaozhiserver WebUI", docs_url=None, redoc_url=None, openapi_url=None)

    async def _status_data() -> dict[str, Any]:
        ha_url = os.environ.get("HA_URL", "")
        ha_token = os.environ.get("HA_TOKEN", "")
        configured = bool(ha_url and ha_token)
        reachable: bool | None = None
        error: str | None = None
        if configured:
            try:
                reachable = await asyncio.wait_for(mcp_server.get_ha().ping(), timeout=3.0)
            except asyncio.TimeoutError:
                reachable = False
                error = "timeout"
            except HAError as exc:
                reachable = False
                error = exc.code
            except Exception:  # noqa: BLE001
                reachable = False
                error = "internal_error"
        return {
            "ok": True,
            "version": __version__,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_STARTED_AT)),
            "uptime_seconds": int(time.time() - _STARTED_AT),
            "ha": {
                "configured": configured,
                "url": _mask_url(ha_url),
                "token": _mask_secret(ha_token),
                "reachable": reachable,
                "error": error,
            },
            "mcp_endpoint": {
                "configured": bool(os.environ.get("MCP_ENDPOINT")),
                "display": _mask_endpoint(os.environ.get("MCP_ENDPOINT", "")),
            },
            "tools": [
                {"name": name, "description": desc, "example": example}
                for name, desc, example in _TOOLS
            ],
            "target": "stdio MCP server + WebSocket bridge",
        }

    def _config_data() -> dict[str, Any]:
        def env(name: str, default: str) -> str:
            raw = os.environ.get(name)
            return raw.strip() if raw is not None and raw.strip() else default

        return {
            "ok": True,
            "items": [
                {"key": "HA_URL", "value": _mask_url(env("HA_URL", "")), "secret": False},
                {"key": "HA_TOKEN", "value": _mask_secret(env("HA_TOKEN", "")), "secret": True},
                {"key": "MCP_ENDPOINT", "value": _mask_endpoint(env("MCP_ENDPOINT", "")), "secret": True},
                {"key": "HA_TIMEOUT", "value": env("HA_TIMEOUT", "10"), "secret": False},
                {"key": "HA_VERIFY_SSL", "value": env("HA_VERIFY_SSL", "true"), "secret": False},
                {"key": "HA_WS_ENABLED", "value": env("HA_WS_ENABLED", "true"), "secret": False},
                {"key": "LOG_LEVEL", "value": LOG_LEVEL, "secret": False},
                {"key": "DEFAULT_LIST_LIMIT", "value": str(DEFAULT_LIST_LIMIT), "secret": False},
                {"key": "MAX_LIST_LIMIT", "value": str(MAX_LIST_LIMIT), "secret": False},
                {"key": "WEBUI_HOST", "value": WEBUI_HOST, "secret": False},
                {"key": "WEBUI_PORT", "value": str(WEBUI_PORT), "secret": False},
            ],
            "note": "配置为只读；修改请更新 .env 并重启服务。",
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(_: Any = Depends(require_auth)) -> HTMLResponse:
        return HTMLResponse(_render_index(await _status_data(), _config_data()))

    @app.get("/api/status")
    async def api_status(_: Any = Depends(require_auth)) -> JSONResponse:
        return JSONResponse(await _status_data())

    @app.get("/api/config")
    async def api_config(_: Any = Depends(require_auth)) -> JSONResponse:
        return JSONResponse(_config_data())

    @app.post("/tool", response_class=HTMLResponse)
    async def run_tool_form(request: Request, _: Any = Depends(require_auth)) -> HTMLResponse:
        form = await request.form()
        name = str(form.get("name") or "")
        raw_params = str(form.get("params") or "").strip() or "{}"
        result, status = await _execute_tool(name, raw_params)
        return HTMLResponse(_render_tool_result(name, raw_params, result, status))

    @app.post("/api/tool/{name}")
    async def api_tool(name: str, request: Request, _: Any = Depends(require_auth)) -> JSONResponse:
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw and raw.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"ok": False, "code": "invalid_argument", "message": "请求体不是合法 JSON：" + str(exc)},
                status_code=400,
            )
        result, status = await _execute_tool(name, payload)
        return JSONResponse(result, status_code=status)

    async def _execute_tool(name: str, params: Any) -> tuple[Any, int]:
        fn = _TOOL_FUNCS.get(name)
        if fn is None:
            return {"ok": False, "code": "unknown_tool",
                    "message": "未知工具：" + str(name) + "。可用工具：" + "、".join(_TOOL_FUNCS)}, 404
        if isinstance(params, str):
            try:
                params = json.loads(params) if params.strip() else {}
            except json.JSONDecodeError as exc:
                return {"ok": False, "code": "invalid_argument",
                        "message": "参数不是合法 JSON：" + str(exc)}, 400
        if not isinstance(params, dict):
            return {"ok": False, "code": "invalid_argument", "message": "参数必须是 JSON 对象。"}, 400
        try:
            result = await fn(**params)
        except TypeError as exc:
            return {"ok": False, "code": "invalid_argument",
                    "message": "参数与工具签名不匹配：" + str(exc)}, 400
        except Exception:  # noqa: BLE001
            log.exception("webui tool=%s failed", name)
            return {"ok": False, "code": "internal_error", "message": "工具调用失败，详见服务端日志。"}, 500
        return result, 200

    return app


_PAGE_STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;margin:0;background:#f5f6f8;color:#1f2328}
header{background:#1f2937;color:#fff;padding:16px 24px}
header h1{margin:0;font-size:18px;font-weight:600}
main{max-width:1040px;margin:20px auto;padding:0 16px}
section{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 12px;color:#111827}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eef0f3;vertical-align:top}
th{width:220px;color:#4b5563;font-weight:500}
code,pre{font-family:ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace;font-size:13px}
pre{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:6px;overflow:auto;max-height:520px}
.ok{color:#047857;font-weight:600}.bad{color:#b91c1c;font-weight:600}.muted{color:#6b7280}
form{display:grid;gap:10px}
select,textarea,button{font:inherit;padding:8px 10px;border:1px solid #d1d5db;border-radius:6px}
textarea{min-height:120px;font-family:ui-monospace,Consolas,monospace}
button{background:#2563eb;color:#fff;border:0;cursor:pointer;width:120px}
button:hover{background:#1d4ed8}
small{color:#6b7280}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>" + html.escape(title) + "</title><style>" + _PAGE_STYLE + "</style></head>"
        "<body><header><h1>homeassistant-xiaozhiserver · WebUI</h1></header>"
        "<main>" + body + "</main></body></html>"
    )


def _render_index(status: dict[str, Any], config: dict[str, Any]) -> str:
    ha = status["ha"]
    if ha["reachable"] is True:
        ha_text = "<span class='ok'>正常</span>"
    elif ha["reachable"] is False:
        ha_text = "<span class='bad'>不可用</span>" + ("（" + html.escape(str(ha["error"])) + "）" if ha["error"] else "")
    else:
        ha_text = "<span class='muted'>未配置</span>"
    endpoint = status["mcp_endpoint"]
    endpoint_text = html.escape(endpoint["display"]) if endpoint["configured"] else "<span class='bad'>未配置</span>"
    status_rows = "".join(
        "<tr><th>" + html.escape(k) + "</th><td>" + v + "</td></tr>"
        for k, v in [
            ("版本", html.escape(status["version"])),
            ("启动时间", html.escape(status["started_at"])),
            ("运行时长", str(status["uptime_seconds"]) + " 秒"),
            ("HA 配置", "<span class='ok'>已配置</span>" if ha["configured"] else "<span class='bad'>未配置</span>"),
            ("HA 连通性", ha_text),
            ("HA 地址", html.escape(ha["url"])),
            ("MCP 接入点", endpoint_text),
            ("运行形态", html.escape(status["target"])),
        ]
    )
    config_rows = "".join(
        "<tr><th>" + html.escape(item["key"]) + "</th><td>" + html.escape(str(item["value"])) + "</td></tr>"
        for item in config["items"]
    )
    options = "".join(
        "<option value='" + html.escape(t["name"]) + "'>" + html.escape(t["name"]) + " — " + html.escape(t["description"]) + "</option>"
        for t in status["tools"]
    )
    examples = "".join(
        "<tr><th>" + html.escape(t["name"]) + "</th><td><code>" + html.escape(t["example"]) + "</code></td></tr>"
        for t in status["tools"]
    )
    body = (
        "<section><h2>运行状态</h2><table>" + status_rows + "</table></section>"
        "<section><h2>配置（只读，敏感值已掩码）</h2><table>" + config_rows + "</table>"
        "<p><small>" + html.escape(config["note"]) + "</small></p></section>"
        "<section><h2>工具调试台</h2>"
        "<form method='post' action='/tool'>"
        "<label>选择工具<select name='name'>" + options + "</select></label>"
        "<label>参数（JSON 对象）<textarea name='params' spellcheck='false'>{}</textarea></label>"
        "<button type='submit'>调用</button>"
        "</form>"
        "<p><small>参数示例：</small></p><table>" + examples + "</table></section>"
    )
    return _page("homeassistant-xiaozhiserver WebUI", body)


def _render_tool_result(name: str, params: str, result: Any, status: int) -> str:
    pretty = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    badge = "<span class='ok'>成功</span>" if status == 200 else "<span class='bad'>失败（HTTP " + str(status) + "）</span>"
    body = (
        "<section><h2>调用结果 " + badge + "</h2>"
        "<table><tr><th>工具</th><td><code>" + html.escape(name) + "</code></td></tr>"
        "<tr><th>参数</th><td><code>" + html.escape(params) + "</code></td></tr></table></section>"
        "<section><h2>返回</h2><pre>" + html.escape(pretty) + "</pre></section>"
        "<p><a href='/'>← 返回状态页</a></p>"
    )
    return _page("调用结果", body)


def main() -> None:
    setup_logging()
    if not WEBUI_ENABLED:
        log.info("WEBUI_ENABLED=false，WebUI 不启动（默认关闭）")
        return
    if not WEBUI_TOKEN:
        log.error("WEBUI_ENABLED=true 但未设置 WEBUI_TOKEN，拒绝启动 WebUI")
        raise SystemExit(2)
    import uvicorn

    log.info("startup webui host=%s port=%s version=%s", WEBUI_HOST, WEBUI_PORT, __version__)
    uvicorn.run(create_app(WEBUI_TOKEN), host=WEBUI_HOST, port=WEBUI_PORT, log_level="warning")


if __name__ == "__main__":
    main()
