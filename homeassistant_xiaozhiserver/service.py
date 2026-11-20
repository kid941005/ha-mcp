# -*- coding: utf-8 -*-
"""HA 服务映射、状态预期与回读校验（与 MCP 传输层解耦）。

本模块只依赖 HAClient/HAError 与配置，不导入 MCPServer，便于单独测试。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .config import (
    SERVICE_CONFIRM_DELAY,
    SERVICE_CONFIRM_INTERVAL,
    SERVICE_CONFIRM_RETRIES,
    SERVICE_CONFIRM_TIMEOUT,
)
from .client import HAError, HAClient

log = logging.getLogger("homeassistant_xiaozhiserver.service")

SUPPORTED_ACTIONS = [
    "turn_on", "turn_off", "toggle", "open", "close", "stop", "pause", "continue", "resume", "play",
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
    "media_player": ["turn_on", "turn_off", "play", "pause", "stop", "continue", "resume",
                     "set_volume", "volume_mute", "volume_unmute"],
    "vacuum": ["turn_on", "turn_off", "stop", "pause", "continue", "resume"],
    "lock": ["lock", "unlock"],
    "scene": ["turn_on"],
    "script": ["turn_on"],
}

# 用于判断控制前后状态是否真的变化（排除 last_changed 之类的时间戳噪声）
SNAP_KEYS = [
    "brightness", "color_temp_kelvin", "rgb_color", "volume_level", "is_volume_muted",
    "temperature", "fan_mode", "percentage", "current_position", "hvac_mode",
]

def _domain_of(entity_id: str) -> str:
    return str(entity_id).split(".", 1)[0]


def _friendly_name(st: dict) -> str:
    return str((st.get("attributes") or {}).get("friendly_name") or st.get("entity_id"))


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


# 通用服务 -> 乐观预期状态（HA 不返回结果时，告诉调用方“预期会变成什么”）。
# 注意：turn_on/turn_off 对部分 domain 不能一概预期为 on/off（例如 climate 打开后是
# heat/cool/auto，media_player 打开后可能是 idle/playing），需由 _expected_state 按 domain 判断。
_EXPECTED_STATE = {
    "open_cover": "open", "close_cover": "closed",
    "media_play": "playing", "media_pause": "paused", "media_stop": "idle",
    "lock": "locked", "unlock": "unlocked",
}

# 这些 domain 的 turn_on/turn_off 目标状态由设备能力决定，不能只用单一字符串判定。
_NON_BINARY_TURN_DOMAINS = {"climate", "media_player", "vacuum", "scene", "script"}


def _expected_state(service: str, before_state: Optional[str],
                    domain: Optional[str] = None) -> Optional[str]:
    """按服务语义 + 设备 domain 推导乐观预期状态；无法可靠推导时返回 None。"""
    svc = (service or "").strip().lower()
    dom = (domain or "").strip().lower()
    if svc == "toggle":
        if before_state == "on":
            return "off"
        if before_state == "off":
            return "on"
        return None
    if svc in ("turn_on", "turn_off"):
        if dom in _NON_BINARY_TURN_DOMAINS:
            return None
        return "on" if svc == "turn_on" else "off"
    if svc in ("start", "stop") and dom in ("vacuum", "homeassistant"):
        return None
    return _EXPECTED_STATE.get(svc)


def _field_brief(fname: str, fdef: dict) -> dict:
    """把 HA 服务参数字段压缩成便于大模型理解的说明。"""
    if not isinstance(fdef, dict):
        return {"name": fname, "required": False}
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


def _rgb_components(value: Any, code: str = "invalid_value", label: str = "颜色") -> list:
    """校验并归一化 [r,g,b]；非法值抛 HAError（不冒泡为 internal_error）。"""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise HAError(code, label + " 必须是 [r,g,b] 三个 0-255 整数，收到：" + str(value))
    try:
        rgb = [int(x) for x in value]
    except (TypeError, ValueError):
        raise HAError(code, label + " 必须是 [r,g,b] 三个 0-255 整数，收到：" + str(value))
    if any(c < 0 or c > 255 for c in rgb):
        raise HAError(code, label + " 每个分量必须在 0-255，收到：" + str(value))
    return rgb


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
    if "rgb_color" in payload:
        exp["rgb_color"] = {"expected": _rgb_components(payload.get("rgb_color"), "invalid_data", "data.rgb_color"),
                            "tolerance": 0}
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
    expected = _expected_state(service, before_state, service_domain)
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
    readback_errors: list = []
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
        except HAError as e:
            after_st = None
            # 服务已下发，回读失败不应伪造成“无变化”；记录错误供调用方判断。
            if e.code not in readback_errors:
                readback_errors.append(e.code)
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
    elif readback_errors and after is None:
        # 服务已下发但一次都没回读到状态：不能当作 no_change，标记为待确认。
        status = "accepted_pending"
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
        if readback_errors and after is None:
            msg += "。HA 已接受调用，但回读状态失败（" + "、".join(readback_errors) + "），无法确认结果；请稍后用 get_device_state 复查。"
        else:
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
        "readback_errors": readback_errors,
    }
    log.info("action entity=%s service=%s status=%s verified=%s attempts=%s elapsed_ms=%s",
             eid, svc_str, status, verified, attempts, verification["elapsed_ms"])
    if status == "mismatch":
        log.warning("action mismatch entity=%s service=%s mismatches=%s", eid, svc_str, check["mismatches"])
    return {
        "entity_id": eid, "name": name, "service": svc_str,
        "response_kind": resp_kind, "status": status,
        "expected_state": expected, "before": before, "after": after,
        "changed": changed, "verified": verified,
        "readback_errors": readback_errors,
        "verification": verification, "message": msg,
    }


def _need_value(action: str, value: Any) -> None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise HAError("missing_value", "动作 " + action + " 需要提供 value 参数（如亮度 1-100、温度、色温开尔文、颜色 #RRGGBB）。")


def _clamp_pct(value: Any) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise HAError("invalid_value", "value 必须是 1-100 的数字，收到：" + str(value))
    if v < 1 or v > 100:
        raise HAError("invalid_value", "value 必须在 1-100 之间，收到：" + str(value))
    return int(round(v))


def _parse_color(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return _rgb_components(value)
    s = str(value).strip().lstrip("#")
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        base = 10  # "r,g,b" 按十进制解析
    elif len(s) == 6:
        parts = [s[0:2], s[2:4], s[4:6]]
        base = 16  # #RRGGBB 按十六进制解析
    else:
        raise HAError("invalid_value", "颜色格式应为 #RRGGBB 或 'r,g,b'，收到：" + str(value))
    try:
        rgb = [int(p, base) for p in parts]
    except (TypeError, ValueError):
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
        if domain == "cover":
            return "cover", "stop_cover", {}
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
        except (TypeError, ValueError):
            raise HAError("invalid_value", "温度 value 必须是数字，收到：" + str(value))
    if a == "set_color_temp":
        _need_value(a, value)
        try:
            return "light", "turn_on", {"color_temp_kelvin": int(float(value))}
        except (TypeError, ValueError):
            raise HAError("invalid_value", "色温 value 必须是开尔文数字，收到：" + str(value))
    if a == "set_color":
        _need_value(a, value)
        return "light", "turn_on", {"rgb_color": _parse_color(value)}
    raise HAError(
        "unsupported_action",
        "不支持的控制动作：" + str(action) + "。可用动作：" + ", ".join(SUPPORTED_ACTIONS),
    )


def _hint() -> str:
    return "请检查 HA_URL / HA_TOKEN 配置与网络连通性；可用 /ready 或 README「排查」章节定位问题。"

