#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 Home Assistant 模拟器，仅用于离线自测本 MCP 服务。

只实现本服务用到的 /api 子集，并刻意复刻 HA 的真实行为：
对「不存在的实体」调用 service 也返回 200 []（静默假成功），以验证回读复核逻辑。

运行：TEST_TOKEN=yourpass python tests/fake_ha.py
（默认监听 127.0.0.1:8123；令牌通过 FAKE_HA_TOKEN 指定，缺省 devtoken）
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("FAKE_HA_TOKEN", "devtoken")
PORT = int(os.environ.get("FAKE_HA_PORT", "8123"))
BIND = os.environ.get("FAKE_HA_BIND", "127.0.0.1")

STATES = {
    "light.living_room": {
        "entity_id": "light.living_room", "state": "off",
        "attributes": {"friendly_name": "客厅灯", "brightness": None,
                       "color_temp_kelvin": 2700, "rgb_color": [255, 255, 255]},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "switch.fan": {
        "entity_id": "switch.fan", "state": "off",
        "attributes": {"friendly_name": "电风扇"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "sensor.living_temp": {
        "entity_id": "sensor.living_temp", "state": "24.5",
        "attributes": {"friendly_name": "客厅温度", "unit_of_measurement": "°C", "device_class": "temperature"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "climate.ac": {
        "entity_id": "climate.ac", "state": "off",
        "attributes": {"friendly_name": "空调", "temperature": 26, "current_temperature": 28, "hvac_mode": "off"},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "media_player.tv": {
        "entity_id": "media_player.tv", "state": "idle",
        "attributes": {"friendly_name": "电视", "volume_level": 0.3, "is_volume_muted": False},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
    "cover.curtain": {
        "entity_id": "cover.curtain", "state": "closed",
        "attributes": {"friendly_name": "窗帘", "current_position": 0},
        "last_changed": "2026-01-01T00:00:00+00:00", "last_updated": "2026-01-01T00:00:00+00:00",
    },
}


def apply_service(domain, service, body):
    eid = body.get("entity_id")
    if eid not in STATES:
        return []  # 复刻 HA：对不存在实体返回 200 空列表
    st = STATES[eid]
    a = st["attributes"]
    if service in ("turn_on", "open_cover", "start", "media_play"):
        if domain == "cover":
            st["state"] = "open"
        else:
            st["state"] = "on"
        if "brightness_pct" in body:
            a["brightness"] = int(round(float(body["brightness_pct"]) * 2.55))
        if "color_temp_kelvin" in body:
            a["color_temp_kelvin"] = body["color_temp_kelvin"]
        if "rgb_color" in body:
            a["rgb_color"] = body["rgb_color"]
    elif service in ("turn_off", "close_cover", "stop", "media_stop"):
        st["state"] = "closed" if domain == "cover" else "off"
    elif service == "toggle":
        st["state"] = "off" if st["state"] == "on" else "on"
    elif service == "volume_set":
        a["volume_level"] = body.get("volume_level")
    elif service == "volume_mute":
        a["is_volume_muted"] = body.get("is_volume_muted")
    elif service == "set_temperature":
        a["temperature"] = body.get("temperature")
    elif service == "media_pause":
        st["state"] = "paused"
    return []


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self._send({"message": "Unauthorized"}, 401)
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        path = self.path.split("?")[0]
        if path in ("/api", "/api/"):
            self._send({"message": "API running."})
        elif path == "/api/states":
            self._send(list(STATES.values()))
        elif path.startswith("/api/states/"):
            eid = path[len("/api/states/"):]
            if eid in STATES:
                self._send(STATES[eid])
            else:
                self._send({"message": "Entity not found."}, 404)
        else:
            self._send({"message": "Not found"}, 404)

    def do_POST(self):
        if not self._authed():
            return
        path = self.path.split("?")[0]
        if path.startswith("/api/services/"):
            parts = path[len("/api/services/"):].split("/")
            domain = parts[0] if parts else ""
            service = parts[1] if len(parts) > 1 else ""
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(apply_service(domain, service, body))
        else:
            self._send({"message": "Not found"}, 404)

    def log_message(self, fmt, *args):
        print("[fake-ha] " + (fmt % args))


if __name__ == "__main__":
    print("fake HA on " + BIND + ":" + str(PORT) + " (token from FAKE_HA_TOKEN)", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
