# -*- coding: utf-8 -*-
"""WebUI：状态查看、工具调试台、配置只读。

直接使用 .env 中的 HA_URL / HA_TOKEN 连接 Home Assistant，不再设置独立 WebUI 密码：
    python -m homeassistant_xiaozhiserver.webui

安全约定：
- WebUI 无独立登录口令，请只绑定可信内网，不要直接暴露公网；
- HA_TOKEN、MCP_ENDPOINT 的令牌只做掩码展示，绝不回传明文；
- 工具名白名单，只允许调用 server.py 暴露的六个工具。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from . import server as mcp_server
from .client import HAError
from .config import DEFAULT_LIST_LIMIT, LOG_LEVEL, MAX_LIST_LIMIT
from .logging_setup import setup_logging

log = logging.getLogger("homeassistant-xiaozhiserver.webui")

_STARTED_AT = time.time()
WEBUI_HOST = "0.0.0.0"
WEBUI_PORT = 8099

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


def create_app() -> FastAPI:
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
            ],
            "note": "配置为只读；WebUI 无独立密码，直接使用 HA_URL / HA_TOKEN 连接 HA；修改请更新 .env 并重启服务。",
        }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_render_index(await _status_data(), _config_data()))

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(await _status_data())

    @app.get("/api/config")
    async def api_config() -> JSONResponse:
        return JSONResponse(_config_data())

    @app.post("/tool", response_class=HTMLResponse)
    async def run_tool_form(request: Request) -> HTMLResponse:
        form = await request.form()
        name = str(form.get("name") or "")
        raw_params = str(form.get("params") or "").strip() or "{}"
        result, status = await _execute_tool(name, raw_params)
        return HTMLResponse(_render_tool_result(name, raw_params, result, status))

    @app.post("/api/tool/{name}")
    async def api_tool(name: str, request: Request) -> JSONResponse:
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
    import uvicorn

    log.info("startup webui host=%s port=%s version=%s", WEBUI_HOST, WEBUI_PORT, __version__)
    uvicorn.run(create_app(), host=WEBUI_HOST, port=WEBUI_PORT, log_level="warning")


if __name__ == "__main__":
    main()
