#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：通过真实 MCP 客户端（streamable-http）连接本服务并调用全部工具。

前置：先启动 fake_ha.py 与 python -m homeassistant_xiaozhiserver（见 tests/run_local_test.sh 或 README）。
环境变量：MCP_URL 默认 http://127.0.0.1:8090/mcp

本脚本对关键结果做断言；任一用例失败会以非 0 退出，便于 CI/回归。
"""
import asyncio
import json
import os
import sys

from mcp import Client

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8090/mcp")
EXPECTED_TOOLS = {
    "list_devices", "get_device_state", "control_device",
    "list_services", "call_service", "list_domains",
}
failures = []


def check(cond, label, detail=""):
    if cond:
        print("   [ok] " + label)
    else:
        msg = label + ((" | " + str(detail)) if detail else "")
        failures.append(msg)
        print("   [FAIL] " + msg)


async def call(session, name, args):
    res = await session.call_tool(name, args)
    text = ""
    for c in res.content:
        if getattr(c, "type", "") == "text":
            text += c.text
    try:
        data = json.loads(text)
    except Exception:
        data = text
    return {"isError": bool(res.is_error), "data": data}


def verify_case(index, name, args, r, expected):
    """校验通用断言集合；expected 里的特殊键见下方。"""
    d = r["data"]
    label = str(index) + " " + name + " " + json.dumps(args, ensure_ascii=False)
    check(isinstance(d, dict), label + " 返回 dict", type(d).__name__)
    if not isinstance(d, dict):
        return
    check(not r["isError"], label + " 不是 MCP isError", r["isError"])
    if "ok" in expected:
        check(d.get("ok") is expected["ok"], label + " ok", d.get("ok"))
    for key in ("status", "code", "total", "count", "grouped_by", "domain",
                "domain_count", "service_count"):
        if key in expected:
            check(d.get(key) == expected[key], label + " " + key, d.get(key))
    if "status_in" in expected:
        check(d.get("status") in expected["status_in"], label + " status_in",
              d.get("status"))
    if "service" in expected:
        check(d.get("service") == expected["service"], label + " service",
              d.get("service"))
    if "entity_state" in expected:
        check((d.get("entity") or {}).get("state") == expected["entity_state"],
              label + " entity.state", (d.get("entity") or {}).get("state"))
    if "entity_dangerous" in expected:
        check((d.get("entity") or {}).get("dangerous") is expected["entity_dangerous"],
              label + " entity.dangerous", (d.get("entity") or {}).get("dangerous"))
    if "after_state" in expected:
        check((d.get("after") or {}).get("state") == expected["after_state"],
              label + " after.state", (d.get("after") or {}).get("state"))
    if "after_brightness" in expected:
        check((d.get("after") or {}).get("brightness") == expected["after_brightness"],
              label + " after.brightness", (d.get("after") or {}).get("brightness"))
    if "after_volume" in expected:
        check((d.get("after") or {}).get("volume_level") == expected["after_volume"],
              label + " after.volume_level", (d.get("after") or {}).get("volume_level"))
    if "dangerous_domains_contains" in expected:
        check(expected["dangerous_domains_contains"] in (d.get("dangerous_domains") or []),
              label + " dangerous_domains", d.get("dangerous_domains"))
    if "dangerous_entities_contains" in expected:
        ids = [e.get("entity_id") for e in (d.get("dangerous_entities") or [])]
        check(expected["dangerous_entities_contains"] in ids,
              label + " dangerous_entities", ids)
    if "attributes_truncated" in expected:
        check((d.get("entity") or {}).get("attributes_truncated") is expected["attributes_truncated"],
              label + " entity.attributes_truncated",
              (d.get("entity") or {}).get("attributes_truncated"))


async def main():
    async with Client(MCP_URL) as session:
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        print("TOOLS:", names)
        check(set(names) == EXPECTED_TOOLS, "工具清单为 6 个预期工具", names)

        cases = [
            # 列表 / 过滤 / 分页 / 聚合
            ("list_devices", {}, {"ok": True, "total": 7, "count": 7,
                                   "grouped_by": "entity"}),
            ("list_devices", {"domain": "light"}, {"ok": True, "total": 1}),
            ("list_devices", {"keyword": "客厅"}, {"ok": True, "total": 3}),
            ("list_devices", {"controllable_only": True}, {"ok": True, "total": 6}),
            ("list_devices", {"group_by": "device"}, {"ok": True, "total": 6,
                                                       "grouped_by": "device"}),
            ("list_devices", {"keyword": "客厅", "group_by": "device"},
             {"ok": True, "total": 2, "grouped_by": "device"}),
            ("list_devices", {"group_by": "bogus"},
             {"ok": False, "code": "invalid_group_by"}),
            ("list_domains", {}, {"ok": True, "dangerous_domains_contains": "lock",
                                   "dangerous_entities_contains": "lock.front_door"}),
            # 能力自描述
            ("list_services", {}, {"ok": True, "domain_count": 6, "service_count": 20}),
            ("list_services", {"domain": "light"}, {"ok": True, "domain": "light"}),
            # 状态查询
            ("get_device_state", {"entity": "客厅灯"},
             {"ok": True, "entity_state": "off"}),
            ("get_device_state", {"entity": "sensor.living_temp"},
             {"ok": True, "entity_state": "24.5"}),
            ("get_device_state", {"entity": "大门门锁"},
             {"ok": True, "entity_dangerous": True}),
            ("get_device_state", {"entity": "电视"},
             {"ok": True, "attributes_truncated": True}),
            ("get_device_state", {"entity": "电视", "include_all_attributes": True},
             {"ok": True, "attributes_truncated": False}),
            # 常用控制
            ("control_device", {"entity": "客厅灯", "action": "set_brightness", "value": 50},
             {"ok": True, "status": "confirmed", "after_brightness": 127}),
            ("control_device", {"entity": "switch.fan", "action": "turn_on"},
             {"ok": True, "status": "confirmed", "after_state": "on"}),
            ("control_device", {"entity": "空调", "action": "set_temperature", "value": 26},
             {"ok": True}),
            ("control_device", {"entity": "窗帘", "action": "turn_on"},
             {"ok": True, "after_state": "open"}),
            ("control_device", {"entity": "窗帘", "action": "stop"},
             {"ok": True, "service": "cover.stop_cover"}),
            ("control_device", {"entity": "电视", "action": "set_volume", "value": 30},
             {"ok": True, "after_volume": 0.3}),
            # 边界与错误
            ("control_device", {"entity": "sensor.living_temp", "action": "turn_on"},
             {"ok": False, "code": "not_controllable"}),
            ("control_device", {"entity": "不存在设备", "action": "turn_on"},
             {"ok": False, "code": "not_found"}),
            ("control_device", {"entity": "客厅灯", "action": "fly"},
             {"ok": False, "code": "unsupported_action"}),
            ("control_device", {"entity": "客厅灯", "action": "lock"},
             {"ok": False, "code": "unsupported_action"}),
            # 通用服务调用
            ("call_service", {"entity": "客厅灯", "service": "light.turn_on",
                              "data": {"brightness_pct": 50}}, {"ok": True}),
            ("call_service", {"entity": "switch.fan", "service": "turn_off"}, {"ok": True}),
            ("call_service", {"entity": "switch.fan", "service": "turn_off"},
             {"ok": True, "status": "no_change"}),
            ("call_service", {"entity": "switch.fan", "service": "silent"},
             {"ok": True, "status": "accepted_pending"}),
            ("call_service", {"entity": "不存在设备", "service": "turn_on"},
             {"ok": False, "code": "not_found"}),
            ("call_service", {"entity": "客厅灯", "service": "light.turn_on",
                              "data": {"entity_id": "switch.fan"}},
             {"ok": False, "code": "invalid_data"}),
            ("call_service", {"entity": "客厅灯", "service": "light.turn_on",
                              "data": {"rgb_color": [300, 0, 0]}},
             {"ok": False, "code": "invalid_data"}),
            ("control_device", {"entity": "客厅灯", "action": "set_color", "value": "#ff0000"},
             {"ok": True}),
            # P2：延迟生效与属性不生效
            ("call_service", {"entity": "客厅灯", "service": "light.turn_off_delayed"},
             {"ok": True, "status": "confirmed", "after_state": "off"}),
            ("call_service", {"entity": "客厅灯", "service": "light.turn_on_ignore_brightness",
                              "data": {"brightness_pct": 80}},
             {"ok": True, "status": "mismatch"}),
        ]
        for index, (name, args, expected) in enumerate(cases, 1):
            r = await call(session, name, args)
            d = r["data"]
            print("\n== " + name + " " + json.dumps(args, ensure_ascii=False))
            print("   result=" + json.dumps(d, ensure_ascii=False)[:1000])
            verify_case(index, name, args, r, expected)

        # P3：危险设备二次确认完整流程
        print("\n== P3 危险设备二次确认 ==")
        first = await call(session, "control_device", {"entity": "大门门锁", "action": "unlock"})
        d1 = first["data"] if isinstance(first["data"], dict) else {}
        token = d1.get("confirm_token")
        print("first=" + json.dumps(d1, ensure_ascii=False)[:1000])
        check(d1.get("code") == "confirmation_required", "P3 首次 confirmation_required", d1.get("code"))
        check(d1.get("dangerous") is True, "P3 首次 dangerous=true", d1.get("dangerous"))
        check(bool(token), "P3 首次返回 confirm_token", token)
        second = await call(session, "control_device", {"entity": "大门门锁", "action": "unlock",
                                                       "confirm_token": token})
        d2 = second["data"] if isinstance(second["data"], dict) else {}
        check(d2.get("ok") is True and d2.get("status") == "confirmed",
              "P3 带 token 执行成功", d2)
        check((d2.get("after") or {}).get("state") == "unlocked",
              "P3 门锁已解锁", d2.get("after"))
        reuse = await call(session, "control_device", {"entity": "大门门锁", "action": "unlock",
                                                      "confirm_token": token})
        d3 = reuse["data"] if isinstance(reuse["data"], dict) else {}
        check(d3.get("code") == "invalid_confirm_token", "P3 复用令牌被拒", d3.get("code"))
        bad = await call(session, "control_device", {"entity": "大门门锁", "action": "lock",
                                                    "confirm_token": "bad-token"})
        d4 = bad["data"] if isinstance(bad["data"], dict) else {}
        check(d4.get("code") == "invalid_confirm_token", "P3 无效令牌被拒", d4.get("code"))
        req = await call(session, "call_service", {"entity": "大门门锁", "service": "lock.lock"})
        d5 = req["data"] if isinstance(req["data"], dict) else {}
        tok2 = d5.get("confirm_token")
        check(d5.get("code") == "confirmation_required", "P3 call_service 也需确认", d5.get("code"))
        mismatch = await call(session, "call_service", {"entity": "大门门锁", "service": "lock.unlock",
                                                       "confirm_token": tok2})
        d6 = mismatch["data"] if isinstance(mismatch["data"], dict) else {}
        check(d6.get("code") == "confirm_token_mismatch", "P3 意图不匹配被拒", d6.get("code"))

    if failures:
        print("\n===== FAILED " + str(len(failures)) + " 项 =====")
        for f in failures:
            print(" - " + f)
        return 1
    print("\n===== ALL CHECKS PASSED =====")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))