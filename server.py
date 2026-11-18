# -*- coding: utf-8 -*-
"""小智 MCP 服务（Home Assistant 桥）。

把 Home Assistant 的设备能力封装成标准 MCP 工具，供小智（xiaozhi-esp32-server）
或其他 MCP 客户端调用。默认以 streamable-http 暴露在 /mcp，也支持 sse / stdio。

环境变量：
  HA_URL        Home Assistant 地址（必填）
  HA_TOKEN      长期访问令牌（必填）
  MCP_TRANSPORT streamable-http(默认) | sse | stdio
  MCP_HOST      默认 0.0.0.0
  MCP_PORT      默认 8090
  MCP_PATH      streamable-http 路径，默认 /mcp
  MCP_SSE_PATH  sse 路径，默认 /sse
  HA_TIMEOUT    HA 请求超时秒数，默认 10
  HA_VERIFY_SSL 是否校验 HA 的 TLS 证书，默认 true
  LOG_LEVEL     日志级别，默认 INFO
  DEFAULT_LIST_LIMIT / MAX_LIST_LIMIT  list_devices 默认/最大返回条数
"""
from __future__ import annotations

import os
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ha_client import HAError, HAClient

DEFAULT_LIST_LIMIT = int(os.environ.get("DEFAULT_LIST_LIMIT", "50"))
MAX_LIST_LIMIT = int(os.environ.get("MAX_LIST_LIMIT", "500"))

SUPPORTED_ACTIONS = [
    "turn_on", "turn_off", "toggle", "open", "close", "stop", "pause", "continue", "play",
    "set_brightness", "set_volume", "volume_mute", "volume_unmute",
    "set_temperature", "set_color_temp", "set_color",
]

# 用于判断控制前后状态是否真的变化（排除 last_changed 之类的时间戳噪声）
SNAP_KEYS = [
    "brightness", "color_temp_kelvin", "rgb_color", "volume_level", "is_volume_muted",
    "temperature", "fan_mode", "percentage", "current_position", "hvac_mode",
]

mcp = FastMCP(
    "ha-mcp",
    instructions=(
        "把 Home Assistant 的智能家居设备封装为 MCP 工具。"
        "查看设备用 list_devices（可按 keyword 名称关键字或 domain 设备类型过滤，支持分页）；"
        "查看单个设备状态用 get_device_state（entity 可传 entity_id 或中文名称）；"
        "控制设备用 control_device（action 见工具描述，value 用于亮度/音量/温度等）；"
        "不确定设备类型时先用 list_domains 查看有哪些类别。"
    ),
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8090")),
    streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
    sse_path=os.environ.get("MCP_SSE_PATH", "/sse"),
    log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    stateless_http=os.environ.get("MCP_STATELESS", "false").strip().lower() in ("1", "true", "yes", "on"),
)

_ha: Optional[HAClient] = None


def get_ha() -> HAClient:
    """惰性创建 HA 客户端：缺少配置时在调用期报错，而不是启动时崩溃。"""
    global _ha
    if _ha is None:
        _ha = HAClient()
    return _ha


def _domain_of(entity_id: str) -> str:
    return str(entity_id).split(".", 1)[0]


def _friendly_name(st: dict) -> str:
    return str((st.get("attributes") or {}).get("friendly_name") or st.get("entity_id"))


def summarize(st: dict) -> dict:
    a = st.get("attributes") or {}
    return {
        "entity_id": st.get("entity_id"),
        "name": _friendly_name(st),
        "domain": _domain_of(st.get("entity_id", "")),
        "state": st.get("state"),
        "unit": a.get("unit_of_measurement"),
        "device_class": a.get("device_class"),
    }


def snapshot(st: Optional[dict]) -> Optional[dict]:
    if not st:
        return None
    a = st.get("attributes") or {}
    d = {"state": st.get("state")}
    for k in SNAP_KEYS:
        if k in a:
            d[k] = a[k]
    return d


def _need_value(action: str, value: Any) -> None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise HAError("missing_value", "动作 " + action + " 需要提供 value 参数（如亮度 1-100、温度、色温开尔文、颜色 #RRGGBB）。")


def _clamp_pct(value: Any) -> int:
    try:
        v = float(value)
    except Exception:
        raise HAError("invalid_value", "value 必须是 1-100 的数字，收到：" + str(value))
    if v < 1 or v > 100:
        raise HAError("invalid_value", "value 必须在 1-100 之间，收到：" + str(value))
    return int(round(v))


def _parse_color(value: Any) -> list:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        rgb = [int(x) for x in value]
    else:
        s = str(value).strip().lstrip("#")
        if "," in s:
            parts = [p.strip() for p in s.split(",")]
        elif len(s) == 6:
            parts = [s[0:2], s[2:4], s[4:6]]
        else:
            raise HAError("invalid_value", "颜色格式应为 #RRGGBB 或 'r,g,b'，收到：" + str(value))
        try:
            rgb = [int(p, 16) for p in parts]
        except Exception:
            raise HAError("invalid_value", "颜色格式应为 #RRGGBB 或 'r,g,b'，收到：" + str(value))
    if len(rgb) != 3 or any(c < 0 or c > 255 for c in rgb):
        raise HAError("invalid_value", "RGB 每个分量必须在 0-255，收到：" + str(value))
    return rgb


def build_service(action: str, domain: str, value: Any) -> tuple:
    """把通用动作翻译为 (service_domain, service, extra_data)。"""
    a = (action or "").strip().lower()
    if a == "turn_on":
        if domain == "cover":
            return "cover", "open_cover", {}
        if domain == "vacuum":
            return "vacuum", "start", {}
        return domain, "turn_on", {}
    if a == "turn_off":
        if domain == "cover":
            return "cover", "close_cover", {}
        if domain == "vacuum":
            return "vacuum", "stop", {}
        return domain, "turn_off", {}
    if a == "toggle":
        return "homeassistant", "toggle", {}
    if a == "open":
        if domain == "cover":
            return "cover", "open_cover", {}
        if domain == "vacuum":
            return "vacuum", "start", {}
        return domain, "turn_on", {}
    if a == "close":
        if domain == "cover":
            return "cover", "close_cover", {}
        return domain, "turn_off", {}
    if a == "stop":
        if domain == "vacuum":
            return "vacuum", "stop", {}
        if domain == "media_player":
            return "media_player", "media_stop", {}
        return "homeassistant", "stop", {}
    if a == "pause":
        if domain == "media_player":
            return "media_player", "media_pause", {}
        if domain == "vacuum":
            return "vacuum", "pause", {}
        return "homeassistant", "pause", {}
    if a in ("continue", "resume"):
        if domain == "media_player":
            return "media_player", "media_play", {}
        if domain == "vacuum":
            return "vacuum", "start", {}
        return "homeassistant", "start", {}
    if a == "play":
        return "media_player", "media_play", {}
    if a == "set_brightness":
        _need_value(a, value)
        return "light", "turn_on", {"brightness_pct": _clamp_pct(value)}
    if a == "set_volume":
        _need_value(a, value)
        return "media_player", "volume_set", {"volume_level": round(_clamp_pct(value) / 100.0, 3)}
    if a == "volume_mute":
        return "media_player", "volume_mute", {"is_volume_muted": True}
    if a == "volume_unmute":
        return "media_player", "volume_mute", {"is_volume_muted": False}
    if a == "set_temperature":
        _need_value(a, value)
        try:
            return "climate", "set_temperature", {"temperature": float(value)}
        except Exception:
            raise HAError("invalid_value", "温度 value 必须是数字，收到：" + str(value))
    if a == "set_color_temp":
        _need_value(a, value)
        try:
            return "light", "turn_on", {"color_temp_kelvin": int(float(value))}
        except Exception:
            raise HAError("invalid_value", "色温 value 必须是开尔文数字，收到：" + str(value))
    if a == "set_color":
        _need_value(a, value)
        return "light", "turn_on", {"rgb_color": _parse_color(value)}
    raise HAError(
        "unsupported_action",
        "不支持的控制动作：" + str(action) + "。可用动作：" + ", ".join(SUPPORTED_ACTIONS),
    )


def _hint() -> str:
    return "请检查 HA_URL / HA_TOKEN 配置与网络连通性；可用 /health 或 README「排查」章节定位问题。"


@mcp.tool()
async def list_devices(keyword: Optional[str] = None, domain: Optional[str] = None,
                       limit: Optional[int] = None, offset: int = 0) -> dict:
    """列出/查询 Home Assistant 中的设备（实体）。

    可按 keyword（设备名称或 entity_id 关键字，模糊匹配）和 domain（设备类型，
    如 light/switch/sensor/climate/cover/media_player/fan/vacuum/scene）过滤；
    结果支持分页：limit 每页条数，offset 起始偏移（默认从 0 开始）。

    适用场景：用户问「有哪些设备」「列出所有的灯」「客厅有什么设备」。
    返回包含 total/count/has_more/next_offset 及设备精简列表（entity_id/名称/类型/状态/单位）。
    """
    try:
        ha = get_ha()
        states = await ha.get_states()
        items = states
        if domain:
            d = domain.strip().lower()
            items = [s for s in items if str(s.get("entity_id", "")).startswith(d + ".")]
        if keyword:
            kw = keyword.strip().lower()
            items = [
                s for s in items
                if kw in _friendly_name(s).lower() or kw in str(s.get("entity_id", "")).lower()
            ]
        total = len(items)
        lim = limit if (limit and int(limit) > 0) else DEFAULT_LIST_LIMIT
        lim = min(int(lim), MAX_LIST_LIMIT)
        off = max(0, int(offset or 0))
        page = items[off:off + lim]
        devices = [summarize(s) for s in page]
        next_off = off + len(page)
        has_more = next_off < total
        msg = "共 " + str(total) + " 个设备，本次返回 " + str(len(devices)) + " 个"
        msg += "（还有更多，可用 offset=" + str(next_off) + " 继续）" if has_more else "。"
        return {
            "ok": True, "total": total, "count": len(devices), "offset": off,
            "limit": lim, "has_more": has_more,
            "next_offset": next_off if has_more else None,
            "devices": devices, "message": msg,
        }
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": _hint()}
    except Exception as e:  # noqa: BLE001 保证不崩溃
        return {"ok": False, "code": "internal_error", "message": "查询设备列表失败：" + str(e)}


@mcp.tool()
async def get_device_state(entity: str) -> dict:
    """查询单个设备的当前状态与关键属性。

    entity 可传 entity_id（如 light.living_room）或设备的友好名称（如 客厅灯），
    服务端会先直查、再按名称精确/模糊匹配。多义时返回候选列表。

    适用场景：用户问「客厅灯现在开着吗」「卧室温度多少」「空调什么状态」。
    返回 state 及 attributes（亮度、色温、音量、温度、当前位置等）。
    """
    try:
        ha = get_ha()
        st, cands = await ha.resolve_entity(entity)
        if st is None:
            if cands:
                names = [(_friendly_name(c) + " (" + str(c.get("entity_id")) + ")") for c in cands[:10]]
                return {"ok": False, "code": "ambiguous",
                        "message": "『" + str(entity) + "』匹配到多个设备，请指定更精确的名称或 entity_id。",
                        "candidates": names}
            return {"ok": False, "code": "not_found",
                    "message": "未找到设备『" + str(entity) + "』。可先用 list_devices 查询可用设备。"}
        s = summarize(st)
        s["last_changed"] = st.get("last_changed")
        s["last_updated"] = st.get("last_updated")
        s["attributes"] = st.get("attributes") or {}
        msg = s["name"] + " 当前状态：" + str(s["state"])
        if s.get("unit"):
            msg += " " + str(s["unit"])
        return {"ok": True, "entity": s, "message": msg}
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": _hint()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "查询设备状态失败：" + str(e)}


@mcp.tool()
async def control_device(entity: str, action: str, value: Optional[float] = None) -> dict:
    """控制设备（开关、亮度、音量、温度、颜色等）。

    参数：
      entity  目标设备：entity_id 或友好名称（会先精确/模糊匹配）。
      action  动作，支持：
        turn_on/turn_off   开关（cover 自动映射 open/close，vacuum 映射 start/stop）
        toggle             切换
        open/close/stop/pause/continue/play  按设备能力映射到对应服务
        set_brightness     value=亮度百分比 1-100（仅 light）
        set_volume         value=音量百分比 1-100（media_player）
        volume_mute/volume_unmute  静音/取消静音
        set_temperature    value=目标温度（摄氏度，climate）
        set_color_temp     value=色温（开尔文，light）
        set_color          value=颜色 #RRGGBB 或 "r,g,b"（light）
      value   数字参数，按 action 需要提供。

    控制后会回读设备状态确认；若 HA 接受调用但状态未变化会给出提示（避免静默假成功）。
    适用场景：「把客厅灯亮度调到 50」「关掉风扇」「空调设到 26 度」「电视音量 30」。
    """
    try:
        ha = get_ha()
        st, cands = await ha.resolve_entity(entity)
        if st is None:
            if cands:
                return {"ok": False, "code": "ambiguous",
                        "message": "『" + str(entity) + "』匹配到多个设备，无法确定控制目标，请指定更精确名称或 entity_id。",
                        "candidates": [str(c.get("entity_id")) for c in cands[:10]]}
            return {"ok": False, "code": "not_found",
                    "message": "未找到设备『" + str(entity) + "』，无法控制。可先用 list_devices 查询。"}
        eid = str(st.get("entity_id"))
        domain = _domain_of(eid)
        sd, svc, extra = build_service(action, domain, value)
        payload = {"entity_id": eid}
        payload.update(extra)
        before = snapshot(st)
        await ha.call_service(sd, svc, payload)
        # 回读复核（HA 对不存在实体也可能返回 200，必须靠回读判断真实效果）
        after_st = None
        try:
            after_st = await ha.get_state(eid)
        except HAError:
            after_st = None
        after = snapshot(after_st)
        changed = after is not None and after != before
        msg = "已对 " + _friendly_name(st) + " 执行 " + str(action) + "（服务 " + sd + "." + svc + "）"
        if after is not None:
            msg += "，当前状态：" + str(after.get("state"))
            if not changed:
                msg += "。注意：服务调用已被 HA 接受，但未观察到状态变化——该设备可能不支持此操作或当前已是该状态。"
        return {
            "ok": True, "entity_id": eid, "name": _friendly_name(st),
            "action": action, "service": sd + "." + svc,
            "before": before, "after": after, "changed": changed, "message": msg,
        }
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": "可先用 list_devices / get_device_state 确认设备与能力后重试。"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "控制设备失败：" + str(e)}


@mcp.tool()
async def list_domains() -> dict:
    """列出当前所有设备类型（domain）及其数量。

    便于了解家庭有哪些类别的设备，或为 list_devices 选择 domain 过滤条件。
    适用场景：用户问「家里都有哪些类型的设备」。
    """
    try:
        ha = get_ha()
        states = await ha.get_states()
        counts: dict = {}
        for s in states:
            d = _domain_of(s.get("entity_id", ""))
            if d:
                counts[d] = counts.get(d, 0) + 1
        counts = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
        msg = "共 " + str(len(counts)) + " 种设备类型：" + ", ".join(k + "(" + str(v) + ")" for k, v in counts.items())
        return {"ok": True, "domains": counts, "message": msg}
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": _hint()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "查询设备类型失败：" + str(e)}


async def _health(request):
    from starlette.responses import JSONResponse
    info: dict = {"status": "ok", "service": "ha-mcp", "transport": os.environ.get("MCP_TRANSPORT", "streamable-http")}
    try:
        await get_ha().ping()
        info["ha"] = "reachable"
    except HAError as e:
        info["ha"] = "error"
        info["ha_error"] = e.to_dict()
    except Exception as e:  # noqa: BLE001
        info["ha"] = "error"
        info["ha_error"] = str(e)
    return JSONResponse(info)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http").strip().lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    import uvicorn
    from starlette.routing import Route

    if transport == "sse":
        app = mcp.sse_app()
    else:  # streamable-http
        app = mcp.streamable_http_app()
    app.router.routes.append(Route("/health", _health, methods=["GET"]))
    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8090")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
