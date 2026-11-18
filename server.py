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
  HA_WS_ENABLED  是否用 WebSocket 读取设备/实体注册表以支持“按设备聚合”，默认 true
  HA_WS_URL      HA WebSocket 地址，默认由 HA_URL 推导为 ws(s)://host/api/websocket
  HA_REGISTRY_TTL 设备注册表缓存秒数，默认 60
  SERVICE_CONFIRM_DELAY   服务调用后首次回读前的等待秒数，默认 0.6
  SERVICE_CONFIRM_RETRIES 服务调用后最少额外回读次数，默认 1
  SERVICE_CONFIRM_INTERVAL 回读轮询间隔秒数，默认同 SERVICE_CONFIRM_DELAY
  SERVICE_CONFIRM_TIMEOUT 回读确认总超时秒数，默认 4.0（窗口内持续轮询直到匹配预期）
  DANGER_CONFIRM_ENABLED 是否对危险设备启用二次确认，默认 true
  DANGER_CONFIRM_TTL     危险操作确认令牌有效期（秒），默认 120
  DANGEROUS_DOMAINS      危险设备 domain 列表（逗号分隔），默认 lock,alarm_control_panel
  DANGEROUS_COVER_CLASSES 危险 cover 的 device_class 列表，默认 garage,door,gate,window
  LOG_LEVEL     日志级别，默认 INFO
  DEFAULT_LIST_LIMIT / MAX_LIST_LIMIT  list_devices 默认/最大返回条数
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ha_client import HAError, HAClient

DEFAULT_LIST_LIMIT = int(os.environ.get("DEFAULT_LIST_LIMIT", "50"))
MAX_LIST_LIMIT = int(os.environ.get("MAX_LIST_LIMIT", "500"))
SERVICE_CONFIRM_DELAY = float(os.environ.get("SERVICE_CONFIRM_DELAY", "0.6"))
SERVICE_CONFIRM_RETRIES = int(os.environ.get("SERVICE_CONFIRM_RETRIES", "1"))
# 回读轮询间隔与总超时：在窗口内反复读取，直到状态/属性与预期一致
SERVICE_CONFIRM_INTERVAL = float(os.environ.get("SERVICE_CONFIRM_INTERVAL", str(SERVICE_CONFIRM_DELAY)))
SERVICE_CONFIRM_TIMEOUT = float(os.environ.get("SERVICE_CONFIRM_TIMEOUT", "4.0"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_set(name: str, default: str) -> set:
    raw = os.environ.get(name, default)
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


# 误操作防护：危险设备首次调用只返回确认令牌，用户确认后再带 token 调用才执行
DANGER_CONFIRM_ENABLED = _env_bool("DANGER_CONFIRM_ENABLED", True)
DANGER_CONFIRM_TTL = float(os.environ.get("DANGER_CONFIRM_TTL", "120"))
DANGEROUS_DOMAINS = _env_set("DANGEROUS_DOMAINS", "lock,alarm_control_panel")
DANGEROUS_COVER_CLASSES = _env_set("DANGEROUS_COVER_CLASSES", "garage,door,gate,window")

SUPPORTED_ACTIONS = [
    "turn_on", "turn_off", "toggle", "open", "close", "stop", "pause", "continue", "play",
    "set_brightness", "set_volume", "volume_mute", "volume_unmute",
    "set_temperature", "set_color_temp", "set_color", "lock", "unlock",
]

# 各设备类型内置支持的控制动作（与 build_service 对齐）。不在表内的类型视为“无内置控制”，
# 仍可用 list_services + call_service 直接调用任意 HA 服务。
DOMAIN_ACTIONS = {
    "light": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color_temp", "set_color"],
    "switch": ["turn_on", "turn_off", "toggle"],
    "fan": ["turn_on", "turn_off", "toggle"],
    "input_boolean": ["turn_on", "turn_off", "toggle"],
    "automation": ["turn_on", "turn_off", "toggle"],
    "cover": ["open", "close", "stop", "turn_on", "turn_off"],
    "climate": ["turn_on", "turn_off", "set_temperature"],
    "media_player": ["turn_on", "turn_off", "play", "pause", "stop", "continue",
                     "set_volume", "volume_mute", "volume_unmute"],
    "vacuum": ["turn_on", "turn_off", "stop", "pause", "continue"],
    "lock": ["lock", "unlock"],
    "scene": ["turn_on"],
    "script": ["turn_on"],
}

# 用于判断控制前后状态是否真的变化（排除 last_changed 之类的时间戳噪声）
SNAP_KEYS = [
    "brightness", "color_temp_kelvin", "rgb_color", "volume_level", "is_volume_muted",
    "temperature", "fan_mode", "percentage", "current_position", "hvac_mode",
]

mcp = FastMCP(
    "ha-mcp",
    instructions=(
        "把 Home Assistant 的智能家居设备封装为 MCP 工具。"
        "查看设备用 list_devices（可按 keyword 名称关键字或 domain 设备类型过滤，支持分页；"
        "结果带 controllable 标记说明是否可控制；controllable_only=true 只看可控设备；"
        "group_by=\"device\" 按物理设备聚合多个实体）；"
        "查看单个设备状态用 get_device_state（entity 可传 entity_id 或中文名称）；"
        "查询某类设备支持哪些服务与参数用 list_services；"
        "常规控制用 control_device（action 见工具描述，value 用于亮度/音量/温度等，"
        "控制后自动回读校验状态与属性）；"
        "对 lock、alarm_control_panel 以及车库门/门窗类 cover 等危险设备的操作会先返回 "
        "confirmation_required 与 confirm_token；必须让用户明确确认后，带 confirm_token 再调用一次；"
        "需要执行上述未覆盖的任意 HA 服务时用 call_service（服务名 + 参数 JSON）；"
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


def _danger_flags(entity_id: str, attributes: Optional[dict]) -> tuple:
    """判断实体是否属于高风险设备（操作需二次确认）。"""
    domain = _domain_of(entity_id)
    if domain in DANGEROUS_DOMAINS:
        return True, "该设备类型（" + domain + "）属于高风险设备"
    if domain == "cover":
        dc = str((attributes or {}).get("device_class") or "").strip().lower()
        if dc in DANGEROUS_COVER_CLASSES:
            return True, "该 cover 设备类型（" + dc + "）属于高风险（门窗/车库等）"
    return False, None


def _danger_info(entity_id: str, attributes: Optional[dict]) -> dict:
    dangerous, reason = _danger_flags(entity_id, attributes)
    return {"dangerous": dangerous, "danger_reason": reason}


_CONFIRM_TOKENS: dict = {}


def _prune_confirm_tokens() -> None:
    now = time.monotonic()
    for token in [k for k, v in _CONFIRM_TOKENS.items() if float(v.get("expires_at", 0)) <= now]:
        _CONFIRM_TOKENS.pop(token, None)


def _confirm_intent(service_domain: str, service: str, payload: dict) -> str:
    try:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        body = str(payload)
    return str(service_domain) + "." + str(service) + "|" + body


def _issue_confirm_token(entity_id: str, service_domain: str, service: str, payload: dict) -> str:
    _prune_confirm_tokens()
    token = secrets.token_urlsafe(12)
    _CONFIRM_TOKENS[token] = {
        "intent": _confirm_intent(service_domain, service, payload),
        "entity_id": entity_id,
        "service": str(service_domain) + "." + str(service),
        "expires_at": time.monotonic() + max(1.0, DANGER_CONFIRM_TTL),
    }
    return token


def _danger_block(st: dict, service_domain: str, service: str,
                  payload: dict, confirm_token: Optional[str]) -> Optional[dict]:
    """危险设备二次确认闸门：返回拦截结果；非危险或确认通过返回 None。"""
    if not DANGER_CONFIRM_ENABLED:
        return None
    eid = str(st.get("entity_id"))
    dangerous, reason = _danger_flags(eid, st.get("attributes") or {})
    if not dangerous:
        return None
    name = _friendly_name(st)
    svc_str = str(service_domain) + "." + str(service)
    expected_intent = _confirm_intent(service_domain, service, payload)
    ttl = max(1, int(round(DANGER_CONFIRM_TTL)))
    base = {"ok": False, "dangerous": True, "requires_confirmation": True,
            "entity_id": eid, "name": name, "service": svc_str,
            "risk_reason": reason}
    token = (confirm_token or "").strip()
    if not token:
        new_token = _issue_confirm_token(eid, service_domain, service, payload)
        return {**base, "code": "confirmation_required",
                "confirm_token": new_token, "expires_in": ttl,
                "message": "『" + name + "』是危险设备（" + str(reason) + "），为避免误操作未执行。"
                           "请先向用户明确确认动作，确认后再带上 confirm_token 调用一次；"
                           "令牌 " + str(ttl) + " 秒内有效且仅可使用一次。"}
    rec = _CONFIRM_TOKENS.get(token)
    if rec is None:
        return {**base, "code": "invalid_confirm_token",
                "message": "确认令牌无效或已使用。请重新发起原操作获取新的 confirm_token，并在用户确认后使用。"}
    if float(rec.get("expires_at", 0)) <= time.monotonic():
        _CONFIRM_TOKENS.pop(token, None)
        new_token = _issue_confirm_token(eid, service_domain, service, payload)
        return {**base, "code": "confirm_token_expired", "confirm_token": new_token,
                "expires_in": ttl,
                "message": "确认令牌已过期。已生成新的 confirm_token；请让用户确认后重新调用。"}
    if rec.get("intent") != expected_intent:
        return {**base, "code": "confirm_token_mismatch",
                "message": "确认令牌与本次操作不匹配（设备、服务或参数已变化）。请重新发起原操作获取新的 confirm_token。"}
    _CONFIRM_TOKENS.pop(token, None)  # 一次性：校验通过即消费
    return None


def _controllability(entity_id: str, state: Any) -> dict:
    """判断一个实体是否可控制，并给出内置动作列表。"""
    domain = _domain_of(entity_id)
    actions = list(DOMAIN_ACTIONS.get(domain, []))
    if not actions:
        return {"controllable": False, "actions": [],
                "controllable_reason": "该类型无内置控制动作，可先用 list_services 查能力，再用 call_service 调用"}
    if str(state) in ("unavailable", "unknown"):
        return {"controllable": False, "actions": actions,
                "controllable_reason": "设备当前状态不可用（" + str(state) + "）"}
    return {"controllable": True, "actions": actions, "controllable_reason": "可控制"}


def _registry_maps(reg: Optional[dict]) -> tuple:
    devices: dict = {}
    ents: dict = {}
    if not reg:
        return devices, ents
    for d in reg.get("devices") or []:
        if isinstance(d, dict) and d.get("id"):
            devices[d["id"]] = d
    for e in reg.get("entities") or []:
        if isinstance(e, dict) and e.get("entity_id"):
            ents[e["entity_id"]] = e
    return devices, ents


def _device_label(d: Optional[dict]) -> Optional[str]:
    if not d:
        return None
    return d.get("name_by_user") or d.get("name") or d.get("id")


def _entity_view(st: dict, devices: dict, ents: dict) -> dict:
    """在实体摘要基础上补充 controllable 标记与所属设备信息。"""
    v = summarize(st)
    v.update(_controllability(st.get("entity_id", ""), st.get("state")))
    v.update(_danger_info(st.get("entity_id", ""), st.get("attributes") or {}))
    eid = str(st.get("entity_id", ""))
    re = ents.get(eid) or {}
    did = re.get("device_id")
    v["device_id"] = did
    d = devices.get(did) if did else None
    v["device_name"] = _device_label(d)
    v["area_id"] = (d.get("area_id") if d else None) or re.get("area_id")
    return v


def _group_by_device(views: list, devices: dict) -> list:
    """把实体按 device_id 聚合为设备；无 device_id 的实体各自成组。"""
    groups: dict = {}
    order: list = []
    for v in views:
        did = v.get("device_id")
        key = did or ("entity:" + str(v.get("entity_id")))
        g = groups.get(key)
        if g is None:
            d = devices.get(did) if did else None
            g = {
                "device_id": did,
                "name": _device_label(d) if d else v.get("name"),
                "manufacturer": (d.get("manufacturer") if d else None),
                "model": (d.get("model") if d else None),
                "area_id": (d.get("area_id") if d else None) or v.get("area_id"),
                "controllable": False,
                "dangerous": False,
                "entity_count": 0,
                "entities": [],
            }
            groups[key] = g
            order.append(key)
        g["entities"].append(v)
        g["entity_count"] += 1
        if v.get("controllable"):
            g["controllable"] = True
        if v.get("dangerous"):
            g["dangerous"] = True
    return [groups[k] for k in order]


def snapshot(st: Optional[dict]) -> Optional[dict]:
    if not st:
        return None
    a = st.get("attributes") or {}
    d = {"state": st.get("state")}
    for k in SNAP_KEYS:
        if k in a:
            d[k] = a[k]
    return d


def _classify_service_response(resp: Any) -> str:
    """把 HA service 调用的返回体归类为三态之一。

    - states : 非空数组/对象（旧版 HA 返回受影响的实体状态，或 return_response 有数据）
    - empty  : 空数组 []（调用被接受，但没有任何状态变化）
    - none   : null（新版 HA 不返回调用结果，需乐观推断 + 延迟回读确认）
    """
    if resp is None:
        return "none"
    if isinstance(resp, list):
        return "states" if len(resp) > 0 else "empty"
    if isinstance(resp, dict):
        return "states" if resp else "empty"
    return "none"


# 通用服务 -> 乐观预期状态（HA 不返回结果时，告诉调用方“预期会变成什么”）
_EXPECTED_STATE = {
    "turn_on": "on", "turn_off": "off",
    "open_cover": "open", "close_cover": "closed",
    "start": "on", "stop": "off",
    "media_play": "playing", "media_pause": "paused", "media_stop": "idle",
    "lock": "locked", "unlock": "unlocked",
}


def _expected_state(service: str, before_state: Optional[str]) -> Optional[str]:
    svc = (service or "").strip().lower()
    if svc == "toggle":
        if before_state == "on":
            return "off"
        if before_state == "off":
            return "on"
        return None
    return _EXPECTED_STATE.get(svc)


def _field_brief(fname: str, fdef: dict) -> dict:
    """把 HA 服务参数字段压缩成便于大模型理解的说明。"""
    fdef = fdef or {}
    sel = fdef.get("selector") or {}
    sel_type = next(iter(sel.keys()), None) if isinstance(sel, dict) else None
    out = {"name": fname, "required": bool(fdef.get("required"))}
    if fdef.get("description"):
        out["description"] = fdef["description"]
    if sel_type:
        out["selector"] = sel_type
    if "example" in fdef:
        out["example"] = fdef["example"]
    return out


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _value_matches(actual: Any, expected: Any, tolerance: float = 0.0) -> bool:
    """比较回读值与预期值，数字按容差、列表逐项、布尔按真值。"""
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
            return False
        return all(_value_matches(a, e, tolerance) for a, e in zip(actual, expected))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is not None and bool(actual) == bool(expected)
    na, ne = _num(actual), _num(expected)
    if na is not None and ne is not None:
        return abs(na - ne) <= float(tolerance or 0)
    return actual == expected


def _expected_attributes(payload: dict) -> dict:
    """从服务调用参数推断需要回读核对的属性预期（含合理容差）。"""
    exp: dict = {}
    if not isinstance(payload, dict):
        return exp
    pct = _num(payload.get("brightness_pct"))
    if pct is not None:
        exp["brightness"] = {"expected": int(round(pct * 2.55)), "tolerance": 3}
    bright = _num(payload.get("brightness"))
    if bright is not None:
        exp["brightness"] = {"expected": int(round(bright)), "tolerance": 1}
    kelvin = _num(payload.get("color_temp_kelvin"))
    if kelvin is not None:
        exp["color_temp_kelvin"] = {"expected": int(round(kelvin)), "tolerance": 10}
    if isinstance(payload.get("rgb_color"), (list, tuple)):
        exp["rgb_color"] = {"expected": [int(x) for x in payload["rgb_color"]], "tolerance": 0}
    vol = _num(payload.get("volume_level"))
    if vol is not None:
        exp["volume_level"] = {"expected": vol, "tolerance": 0.02}
    temp = _num(payload.get("temperature"))
    if temp is not None:
        exp["temperature"] = {"expected": temp, "tolerance": 0.5}
    if "is_volume_muted" in payload:
        exp["is_volume_muted"] = {"expected": bool(payload.get("is_volume_muted")), "tolerance": 0}
    if "hvac_mode" in payload:
        exp["hvac_mode"] = {"expected": payload.get("hvac_mode"), "tolerance": 0}
    return exp


def _check_after(after: Optional[dict], expected_state: Optional[str],
                 attr_expect: dict) -> dict:
    """对一次回读做状态 + 属性逐项校验，返回 checks / mismatches。"""
    checks: list = []
    mismatches: list = []
    if expected_state is not None:
        actual = after.get("state") if after else None
        ok = actual == expected_state
        item = {"kind": "state", "field": "state", "expected": expected_state,
                "actual": actual, "ok": ok}
        checks.append(item)
        if not ok:
            mismatches.append(item)
    for field, spec in attr_expect.items():
        actual = after.get(field) if after else None
        expected = spec.get("expected")
        ok = _value_matches(actual, expected, spec.get("tolerance", 0))
        item = {"kind": "attribute", "field": field, "expected": expected,
                "actual": actual, "ok": ok}
        checks.append(item)
        if not ok:
            mismatches.append(item)
    return {"verified": not mismatches, "checks": checks, "mismatches": mismatches}


async def _apply_and_confirm(ha: "HAClient", st: dict, service_domain: str,
                             service: str, payload: dict, action_label: str) -> dict:
    """执行 HA 服务调用，并在超时窗口内轮询回读，校验「状态 + 属性」是否达到预期。

    不再只判断「有没有变化」，而是按服务语义推导预期：
      - 状态预期：turn_on→on、turn_off→off、open_cover→open、set_hvac_mode→指定模式等；
      - 属性预期：brightness_pct→brightness、temperature、volume_level、color_temp_kelvin、rgb_color 等。
    回读在 SERVICE_CONFIRM_TIMEOUT 秒内按 SERVICE_CONFIRM_INTERVAL 轮询，直到全部匹配或超时。

    status 语义：
      confirmed        回读确认状态/属性已达到预期（且确有变化）
      no_change        HA 已接受但无变化；若校验通过表示「本来就是目标状态」
      mismatch         回读观察到变化，但状态或属性与预期不符（设备能力/参数问题）
      accepted_pending HA 返回 null（新版 HA 常见），回读窗口内仍未完全匹配预期
    """
    eid = str(st.get("entity_id"))
    before = snapshot(st)
    before_state = st.get("state") if st else None
    expected = _expected_state(service, before_state)
    # set_hvac_mode 等服务的预期状态来自参数
    if isinstance(payload, dict) and payload.get("hvac_mode"):
        expected = str(payload["hvac_mode"])
    attr_expect = _expected_attributes(payload)
    has_expectation = expected is not None or bool(attr_expect)

    started = time.monotonic()
    resp = await ha.call_service(service_domain, service, payload)
    resp_kind = _classify_service_response(resp)

    min_attempts = max(1, int(SERVICE_CONFIRM_RETRIES) + 1)
    interval = SERVICE_CONFIRM_INTERVAL if SERVICE_CONFIRM_INTERVAL > 0 else 0.0
    attempts = 0
    after = None
    changed = False
    check = {"verified": False, "checks": [], "mismatches": []}

    while True:
        if attempts == 0:
            if SERVICE_CONFIRM_DELAY > 0:
                await asyncio.sleep(SERVICE_CONFIRM_DELAY)
        elif interval > 0:
            await asyncio.sleep(interval)
        attempts += 1
        try:
            after_st = await ha.get_state(eid)
        except HAError:
            after_st = None
        after = snapshot(after_st)
        changed = after is not None and after != before
        check = _check_after(after, expected, attr_expect)
        if has_expectation:
            if check["verified"]:
                break
        elif changed:
            break
        if attempts >= min_attempts and (time.monotonic() - started) >= SERVICE_CONFIRM_TIMEOUT:
            break

    verified = bool(check["verified"]) if has_expectation else bool(changed)
    if has_expectation and check["verified"]:
        status = "confirmed" if changed else "no_change"
    elif changed:
        # 无明确预期时只能确认「有变化」，不能判为 mismatch
        status = "mismatch" if has_expectation else "confirmed"
    elif resp_kind in ("empty", "states"):
        status = "no_change"
    else:  # none
        status = "accepted_pending"

    svc_str = service_domain + "." + service
    name = _friendly_name(st)
    msg = "已对 " + name + " 执行 " + str(action_label) + "（服务 " + svc_str + "）"
    if after is not None:
        msg += "，当前状态：" + str(after.get("state"))
    if status == "confirmed":
        if has_expectation:
            msg += "。已回读确认达到预期（" + str(attempts) + " 次）"
        else:
            msg += "。已回读确认状态发生变化（" + str(attempts) + " 次；该服务无明确预期，按变化确认）"
    elif status == "mismatch":
        detail = "；".join(
            str(m.get("field")) + " 期望 " + str(m.get("expected")) + "，实际 " + str(m.get("actual"))
            for m in check["mismatches"]
        )
        msg += "。已观察到变化，但与预期不符：" + detail + "（回读 " + str(attempts) + " 次）。请检查设备能力或参数。"
    elif status == "no_change":
        if verified:
            msg += "。回读确认设备已处于目标状态（" + str(attempts) + " 次）"
        else:
            msg += "。注意：HA 已接受调用，但回读未见状态变化——设备可能不支持该操作或当前已是目标状态。"
    else:  # accepted_pending
        msg += "。HA 未返回调用结果（新版 HA 常见），已下发但回读 " + str(attempts) + " 次仍未完全匹配预期"
        if expected:
            msg += "（预期状态：" + expected + "）"
        msg += "；若稍后仍未变化，可用 get_device_state 复查。"

    verification = {
        "verified": verified,
        "attempts": attempts,
        "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
        "expectations": {"state": expected, "attributes": attr_expect},
        "checks": check["checks"],
        "mismatches": check["mismatches"],
    }
    return {
        "entity_id": eid, "name": name, "service": svc_str,
        "response_kind": resp_kind, "status": status,
        "expected_state": expected, "before": before, "after": after,
        "changed": changed, "verified": verified,
        "verification": verification, "message": msg,
    }


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
    if a == "lock":
        return "lock", "lock", {}
    if a == "unlock":
        return "lock", "unlock", {}
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
                       controllable_only: bool = False, group_by: str = "entity",
                       limit: Optional[int] = None, offset: int = 0) -> dict:
    """列出/查询 Home Assistant 中的设备。

    可按 keyword（设备名称或 entity_id 关键字，模糊匹配）和 domain（设备类型，
    如 light/switch/sensor/climate/cover/media_player/fan/vacuum/scene）过滤；
    controllable_only=true 只返回可控制的设备；group_by="device" 时按物理设备聚合
    （一个设备下的多个实体合并为一项，含 manufacturer/model/area 与 entities 列表）；
    结果支持分页：limit 每页条数，offset 起始偏移（默认从 0 开始）。

    每个实体带 controllable（是否可控制）、actions（内置动作）、device_id/device_name/area_id。
    适用场景：用户问「有哪些设备」「列出所有的灯」「客厅有什么设备」「哪些设备能控制」。
    """
    try:
        ha = get_ha()
        states = await ha.get_states()
        try:
            reg = await ha.get_registries()
        except HAError:
            reg = None
        devices_map, ents_map = _registry_maps(reg)
        views = [_entity_view(s, devices_map, ents_map) for s in states]
        if domain:
            d = domain.strip().lower()
            views = [v for v in views if str(v.get("entity_id", "")).startswith(d + ".")]
        if keyword:
            kw = keyword.strip().lower()
            views = [
                v for v in views
                if kw in str(v.get("name", "")).lower()
                or kw in str(v.get("entity_id", "")).lower()
                or kw in str(v.get("device_name") or "").lower()
            ]
        if controllable_only:
            views = [v for v in views if v.get("controllable")]

        mode = (group_by or "entity").strip().lower()
        if mode in ("device", "devices", "设备"):
            items = _group_by_device(views, devices_map)
            grouped_by = "device"
        else:
            items = views
            grouped_by = "entity"

        total = len(items)
        lim = limit if (limit and int(limit) > 0) else DEFAULT_LIST_LIMIT
        lim = min(int(lim), MAX_LIST_LIMIT)
        off = max(0, int(offset or 0))
        page = items[off:off + lim]
        next_off = off + len(page)
        has_more = next_off < total
        if grouped_by == "device":
            ent_total = sum(int(g.get("entity_count") or 0) for g in items)
            msg = "共 " + str(total) + " 个设备（聚合 " + str(ent_total) + " 个实体），本次返回 " + str(len(page)) + " 个"
        else:
            msg = "共 " + str(total) + " 个设备，本次返回 " + str(len(page)) + " 个"
        msg += "（还有更多，可用 offset=" + str(next_off) + " 继续）" if has_more else "。"
        return {
            "ok": True, "grouped_by": grouped_by, "registry_available": reg is not None,
            "total": total, "count": len(page), "offset": off,
            "limit": lim, "has_more": has_more,
            "next_offset": next_off if has_more else None,
            "devices": page, "message": msg,
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
        try:
            reg = await ha.get_registries()
        except HAError:
            reg = None
        devices_map, ents_map = _registry_maps(reg)
        s = _entity_view(st, devices_map, ents_map)
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
async def control_device(entity: str, action: str, value: Optional[float] = None,
                         confirm_token: Optional[str] = None) -> dict:
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
      confirm_token 危险设备二次确认令牌。门锁/安防面板/车库门等危险设备首次调用会返回
                   code=confirmation_required 与 confirm_token（不执行）；用户确认后带上该
                   token 再调用一次才真正执行。令牌一次性且绑定本次操作。

    控制后会在超时窗口内轮询回读，校验状态 + 亮度/温度/音量/颜色等属性是否达到预期，
    返回 verified 与 verification（attempts/checks/mismatches/elapsed_ms）。
    若 HA 接受调用但未达预期会明确返回 mismatch 或 no_change，避免静默假成功。
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
        info = _controllability(eid, st.get("state"))
        if not info["controllable"]:
            return {
                "ok": False, "code": "not_controllable",
                "message": "『" + _friendly_name(st) + "』（" + domain + "）不可控制：" + info["controllable_reason"] + "。",
                "entity_id": eid, "domain": domain, "actions": info["actions"],
            }
        sd, svc, extra = build_service(action, domain, value)
        payload = {"entity_id": eid}
        payload.update(extra)
        guard = _danger_block(st, sd, svc, payload, confirm_token)
        if guard is not None:
            return guard
        result = await _apply_and_confirm(ha, st, sd, svc, payload, action)
        result["ok"] = True
        result["action"] = action
        return result
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": "可先用 list_devices / get_device_state 确认设备与能力后重试。"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "控制设备失败：" + str(e)}


@mcp.tool()
async def list_services(entity: Optional[str] = None, domain: Optional[str] = None,
                        keyword: Optional[str] = None, detailed: bool = False) -> dict:
    """查询 Home Assistant 中某类设备/某个设备支持的服务（能力）及参数。

    这是“能力自描述”工具：让调用方在不了解设备细节时，先查清能对这个设备做什么、
    每个服务需要什么参数，再用 call_service 精确调用。

    参数：
      entity   可选，设备 entity_id 或友好名称；给出时只返回该设备所属 domain 的服务。
      domain   可选，直接指定设备类型（如 light/climate/media_player/cover/vacuum）。
      keyword  可选，按服务名或描述关键字过滤（如 "temperature"）。
      detailed 是否返回每个服务的字段参数明细（默认：指定 entity/domain 时为 true，否则仅名称）。
    返回：domain 列表及每个 domain 的服务名/描述/字段。

    适用场景：「空调能做什么操作」「灯支持调色温吗」「这个设备有哪些可控项」。
    """
    try:
        ha = get_ha()
        catalog = await ha.get_services()
        target = None
        if entity:
            st, cands = await ha.resolve_entity(entity)
            if st is None:
                if cands:
                    return {"ok": False, "code": "ambiguous",
                            "message": "『" + str(entity) + "』匹配到多个设备，请指定更精确名称或 entity_id。",
                            "candidates": [str(c.get("entity_id")) for c in cands[:10]]}
                return {"ok": False, "code": "not_found",
                        "message": "未找到设备『" + str(entity) + "』。可先用 list_devices 查询。"}
            target = _domain_of(str(st.get("entity_id")))
        elif domain:
            target = domain.strip().lower()

        kw = (keyword or "").strip().lower()
        detailed = bool(detailed) or bool(target)

        def match(svc_name: str, svc_def: dict) -> bool:
            if not kw:
                return True
            svc_def = svc_def or {}
            text = (svc_name + " " + str(svc_def.get("description", "")) + " " +
                    str(svc_def.get("name", ""))).lower()
            return kw in text

        domains_out = []
        service_total = 0
        for entry in catalog:
            d = str((entry or {}).get("domain", ""))
            if not d:
                continue
            if target and d != target:
                continue
            services = (entry or {}).get("services") or {}
            if not isinstance(services, dict):
                continue
            svc_list = []
            for sname, sdef in services.items():
                if not match(sname, sdef):
                    continue
                item = {"service": sname}
                if isinstance(sdef, dict):
                    if sdef.get("name"):
                        item["name"] = sdef["name"]
                    if sdef.get("description"):
                        item["description"] = sdef["description"]
                    if detailed:
                        fields = sdef.get("fields") or {}
                        if isinstance(fields, dict) and fields:
                            item["fields"] = [_field_brief(fn, fd) for fn, fd in fields.items()]
                svc_list.append(item)
            if svc_list:
                svc_list.sort(key=lambda x: x["service"])
                service_total += len(svc_list)
                domains_out.append({
                    "domain": d,
                    "service_count": len(svc_list),
                    "services": svc_list if detailed else [x["service"] for x in svc_list],
                })

        if target and not domains_out:
            return {"ok": False, "code": "no_services",
                    "message": "没有找到 domain=『" + str(target) + "』的服务（或该设备类型无可用服务）。"}
        domains_out.sort(key=lambda x: x["domain"])
        if target:
            msg = "domain=「" + str(target) + "」共 " + str(service_total) + " 个服务。"
        else:
            msg = "共 " + str(len(domains_out)) + " 个 domain、" + str(service_total) + " 个服务。"
        return {"ok": True, "domain": target, "domain_count": len(domains_out),
                "service_count": service_total, "domains": domains_out, "message": msg}
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": _hint()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "查询服务列表失败：" + str(e)}


@mcp.tool()
async def call_service(entity: str, service: str, data: Optional[dict] = None,
                      confirm_token: Optional[str] = None) -> dict:
    """通用服务调用：对任意设备执行 Home Assistant 的任意服务（覆盖 control_device 未封装的场景）。

    参数：
      entity  目标设备：entity_id 或友好名称（会先精确/模糊匹配）。
      service 服务名，可写 "turn_on"（默认用设备所属 domain），或 "climate.set_hvac_mode"
              （显式带 domain 时以该 domain 为准）。可用 list_services 查询可用服务名。
      data    额外参数（JSON 对象），如 {"brightness":128}、{"temperature":26}、
              {"hvac_mode":"heat"}、{"rgb_color":[255,0,0]}；无需参数时省略。
      confirm_token 危险设备二次确认令牌，机制同 control_device：对 lock/alarm_control_panel
              及车库门/门窗类 cover 等危险设备的服务调用，首次只返回确认令牌不执行。

    返回状态回读校验结果（verified / verification）：
      status=confirmed        回读确认状态/属性已达到预期
      status=no_change        HA 已接受但无变化（verified=true 表示本来就是目标状态）
      status=mismatch         回读有变化，但状态/属性与预期不符
      status=accepted_pending HA 返回 null（新版 HA），回读窗口内仍未完全匹配预期
    适用场景：「把空调设成制热模式」「灯切成暖光」「设置风扇风速」「执行自定义脚本」。
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
                    "message": "未找到设备『" + str(entity) + "』，无法调用服务。可先用 list_devices 查询。"}
        eid = str(st.get("entity_id"))
        spec = (service or "").strip()
        if not spec:
            return {"ok": False, "code": "invalid_service", "message": "service 不能为空。"}
        if "." in spec:
            service_domain, svc = spec.split(".", 1)
            service_domain = service_domain.strip()
            svc = svc.strip()
        else:
            service_domain = _domain_of(eid)
            svc = spec
        if not service_domain or not svc:
            return {"ok": False, "code": "invalid_service",
                    "message": "服务名格式应为 'service' 或 'domain.service'，收到：" + str(service)}
        if data is None:
            extra = {}
        elif isinstance(data, dict):
            extra = dict(data)
        else:
            return {"ok": False, "code": "invalid_data",
                    "message": "data 必须是 JSON 对象（键值对），收到：" + str(type(data).__name__)}
        payload = {"entity_id": eid}
        payload.update(extra)
        guard = _danger_block(st, service_domain, svc, payload, confirm_token)
        if guard is not None:
            return guard
        result = await _apply_and_confirm(ha, st, service_domain, svc, payload, spec)
        result["ok"] = True
        return result
    except HAError as e:
        out = {"ok": False, **e.to_dict()}
        if e.code in ("not_found", "http_error", "upstream_error"):
            out["hint"] = ("该服务可能不存在或参数不匹配。可先用 list_services(domain=…) 查询支持的服"
                           "务与字段，再核对 data 参数名。")
        else:
            out["hint"] = _hint()
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "调用服务失败：" + str(e)}


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
        ctrl = [d for d in counts if DOMAIN_ACTIONS.get(d)]
        danger = [d for d in counts if d in DANGEROUS_DOMAINS]
        msg = "共 " + str(len(counts)) + " 种设备类型：" + ", ".join(k + "(" + str(v) + ")" for k, v in counts.items())
        if ctrl:
            msg += "；可控类型：" + ", ".join(ctrl)
        if danger:
            msg += "；需二次确认的危险类型：" + ", ".join(danger)
        return {"ok": True, "domains": counts, "controllable_domains": ctrl,
                "dangerous_domains": danger, "message": msg}
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
