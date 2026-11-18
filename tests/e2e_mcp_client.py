#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：通过真实 MCP 客户端（streamable-http）连接本服务并调用全部工具。

前置：先启动 fake_ha.py 与 server.py（见 tests/run_local_test.sh 或 README）。
环境变量：MCP_URL 默认 http://127.0.0.1:8090/mcp
"""
import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8090/mcp")


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
    return {"isError": bool(res.isError), "data": data}


async def main():
    async with streamable_http_client(MCP_URL) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])

            cases = [
                ("list_devices", {}),
                ("list_devices", {"domain": "light"}),
                ("list_devices", {"keyword": "客厅"}),
                ("list_devices", {"controllable_only": True}),
                ("list_devices", {"group_by": "device"}),
                ("list_devices", {"keyword": "客厅", "group_by": "device"}),
                ("list_domains", {}),
                ("list_services", {}),
                ("list_services", {"domain": "light"}),
                ("get_device_state", {"entity": "客厅灯"}),
                ("get_device_state", {"entity": "sensor.living_temp"}),
                ("control_device", {"entity": "客厅灯", "action": "set_brightness", "value": 50}),
                ("control_device", {"entity": "switch.fan", "action": "turn_on"}),
                ("control_device", {"entity": "空调", "action": "set_temperature", "value": 26}),
                ("control_device", {"entity": "窗帘", "action": "turn_on"}),
                ("control_device", {"entity": "电视", "action": "set_volume", "value": 30}),
                ("control_device", {"entity": "sensor.living_temp", "action": "turn_on"}),
                ("control_device", {"entity": "不存在设备", "action": "turn_on"}),
                ("control_device", {"entity": "客厅灯", "action": "fly"}),
                ("call_service", {"entity": "客厅灯", "service": "light.turn_on", "data": {"brightness_pct": 50}}),
                ("call_service", {"entity": "switch.fan", "service": "turn_off"}),
                ("call_service", {"entity": "switch.fan", "service": "turn_off"}),
                ("call_service", {"entity": "switch.fan", "service": "silent"}),
                ("call_service", {"entity": "不存在设备", "service": "turn_on"}),
                # P2 状态回读校验：延迟生效（需轮询多次才确认）与属性不生效（应识别为 mismatch）
                ("call_service", {"entity": "客厅灯", "service": "light.turn_off_delayed"}),
                ("call_service", {"entity": "客厅灯", "service": "light.turn_on_ignore_brightness",
                                  "data": {"brightness_pct": 80}}),
            ]
            for name, args in cases:
                r = await call(session, name, args)
                d = r["data"]
                ok = d.get("ok") if isinstance(d, dict) else None
                msg = d.get("message") if isinstance(d, dict) else d
                print("\n== " + name + " " + json.dumps(args, ensure_ascii=False))
                print("   ok=" + str(ok) + " isError=" + str(r["isError"]))
                print("   msg=" + str(msg))
                if name == "list_devices" and isinstance(d, dict):
                    print("   total=" + str(d.get("total")) + " count=" + str(d.get("count")) +
                          " has_more=" + str(d.get("has_more")) + " grouped_by=" + str(d.get("grouped_by")) +
                          " registry=" + str(d.get("registry_available")))
                    if d.get("devices"):
                        first = d["devices"][0]
                        if d.get("grouped_by") == "device":
                            print("   first_device=" + str(first.get("name")) + " entity_count=" +
                                  str(first.get("entity_count")) + " controllable=" + str(first.get("controllable")))
                        else:
                            print("   first_entity=" + str(first.get("entity_id")) + " controllable=" +
                                  str(first.get("controllable")) + " actions=" + str(first.get("actions")))
                if name == "list_services" and isinstance(d, dict) and d.get("ok"):
                    print("   domains=" + str(d.get("domain_count")) + " services=" + str(d.get("service_count")))
                if name in ("control_device", "call_service") and isinstance(d, dict) and d.get("ok"):
                    print("   status=" + str(d.get("status")) + " changed=" + str(d.get("changed")) +
                          " service=" + str(d.get("service")) + " expected=" + str(d.get("expected_state")))
                    v = d.get("verification") or {}
                    print("   verified=" + str(d.get("verified")) + " attempts=" + str(v.get("attempts")) +
                          " elapsed_ms=" + str(v.get("elapsed_ms")))
                    if v.get("mismatches"):
                        print("   mismatches=" + json.dumps(v["mismatches"], ensure_ascii=False))
                    print("   before=" + json.dumps(d.get("before"), ensure_ascii=False) + " after=" + json.dumps(d.get("after"), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
