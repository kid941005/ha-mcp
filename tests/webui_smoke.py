# -*- coding: utf-8 -*-
"""WebUI 冒烟测试：鉴权、状态、配置掩码、工具白名单，不需要真实 HA。

用法：WEBUI_TOKEN=xxx python tests/webui_smoke.py
HA_URL/HA_TOKEN 若已导出则顺带验证连通性字段，否则只验证未配置分支。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("WEBUI_ENABLED", "true")
os.environ.setdefault("WEBUI_TOKEN", "smoke-token")

from fastapi.testclient import TestClient  # noqa: E402

from homeassistant_xiaozhiserver.webui import create_app  # noqa: E402

TOKEN = os.environ["WEBUI_TOKEN"]
AUTH = ("admin", TOKEN)
client = TestClient(create_app(TOKEN))


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print("[ok] " + name)
    else:
        print("[FAIL] " + name + ("" if not extra else " -> " + extra))
        sys.exit(1)


r = client.get("/")
check("未鉴权 GET / 返回 401", r.status_code == 401, str(r.status_code))

r = client.get("/", auth=("admin", "wrong-token"))
check("错误口令返回 401", r.status_code == 401, str(r.status_code))

r = client.get("/", auth=AUTH)
check("Basic 鉴权访问首页 200", r.status_code == 200, str(r.status_code))
check("首页包含运行状态", "运行状态" in r.text)
check("首页包含工具调试台", "工具调试台" in r.text)

r = client.get("/api/status", headers={"Authorization": "Bearer " + TOKEN})
check("Bearer 访问 /api/status 200", r.status_code == 200, str(r.status_code))
status = r.json()
check("status.ok 为 True", status.get("ok") is True)
check("status 含版本号", isinstance(status.get("version"), str) and status["version"])
check("status 工具数为 6", len(status.get("tools") or []) == 6, str(len(status.get("tools") or [])))
ha_token_env = os.environ.get("HA_TOKEN", "")
check("HA_TOKEN 不回传明文", ha_token_env == "" or ha_token_env not in json.dumps(status, ensure_ascii=False))

r = client.get("/api/config", auth=AUTH)
check("访问 /api/config 200", r.status_code == 200, str(r.status_code))
config = r.json()
check("配置标注只读", "只读" in str(config.get("note", "")))
keys = {item["key"]: item["value"] for item in config.get("items", [])}
check("HA_TOKEN 掩码展示", keys.get("HA_TOKEN") == "已设置（掩码）" or keys.get("HA_TOKEN") == "（未设置）", str(keys.get("HA_TOKEN")))
mcp_raw = os.environ.get("MCP_ENDPOINT", "")
if mcp_raw:
    check("MCP_ENDPOINT 不回传 token", "token=" not in str(keys.get("MCP_ENDPOINT", "")).lower())

r = client.post("/api/tool/not_a_tool", json={}, auth=AUTH)
check("未知工具返回 404", r.status_code == 404, str(r.status_code))
check("未知工具错误码", r.json().get("code") == "unknown_tool", str(r.json()))

r = client.post("/api/tool/list_domains", content="{bad json", headers={"Content-Type": "application/json"}, auth=AUTH)
check("非法 JSON 返回 400", r.status_code == 400, str(r.status_code))

r = client.post("/tool", data={"name": "list_domains", "params": "[1,2]"}, auth=AUTH)
check("非对象参数返回失败页", r.status_code == 200 and "HTTP 400" in r.text, str(r.status_code))

if os.environ.get("HA_URL") and os.environ.get("HA_TOKEN"):
    r = client.post("/api/tool/list_domains", json={}, auth=AUTH)
    check("list_domains 调用成功", r.status_code == 200 and r.json().get("ok") is True, str(r.status_code) + " " + r.text[:200])
else:
    print("[skip] 未配置 HA_URL/HA_TOKEN，跳过真实工具调用")

print("SMOKE OK")
