# -*- coding: utf-8 -*-
"""Home Assistant REST API 客户端。

设计要点：
- 所有配置只从环境变量读取，绝不硬编码地址或令牌。
- 网络/鉴权/超时等异常统一转换为 HAError（带错误码 + 中文可读信息），
  由上层 MCP 工具捕获后转成结构化结果，保证服务不崩溃。
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from typing import Any

import httpx

try:  # 设备/实体注册表需通过 HA WebSocket API 读取；缺失时降级为按实体展示
    import websockets
except Exception:  # noqa: BLE001
    websockets = None


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


class HAError(Exception):
    """带错误码与用户可读信息的统一异常。"""

    def __init__(self, code: str, message: str, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d


class HAClient:
    def __init__(self, url: str | None = None, token: str | None = None,
                 timeout: float | None = None, verify_ssl: bool | None = None):
        self.base_url = (url if url is not None else os.environ.get("HA_URL") or "").rstrip("/")
        self.token = (token if token is not None else os.environ.get("HA_TOKEN") or "").strip()
        self.timeout = float(timeout if timeout is not None else os.environ.get("HA_TIMEOUT", "10"))
        self.verify_ssl = env_bool("HA_VERIFY_SSL", True) if verify_ssl is None else verify_ssl
        # 默认不读取宿主的 HTTP_PROXY/NO_PROXY 等环境变量，避免代理干扰容器内直连 HA。
        self.trust_env = env_bool("HA_TRUST_ENV", False)
        # 设备/实体注册表：HA 只在 WebSocket API 暴露（REST 无此端点），用于“按设备聚合”。
        self.ws_url = (os.environ.get("HA_WS_URL") or "").strip() or self._derive_ws_url()
        self.ws_enabled = env_bool("HA_WS_ENABLED", True)
        self.registry_ttl = float(os.environ.get("HA_REGISTRY_TTL", "60"))
        self._registry_cache: dict | None = None
        self._registry_ts: float = 0.0
        self._validate_config()

    def _validate_config(self) -> None:
        missing = []
        if not self.base_url:
            missing.append("HA_URL")
        if not self.token:
            missing.append("HA_TOKEN")
        if missing:
            raise HAError(
                "config_missing",
                "缺少必需的环境变量：" + ", ".join(missing) + "。请在 .env 或 docker run -e 中配置后重启服务。",
            )

    # ---------------- 底层 HTTP ----------------
    def _client(self) -> httpx.AsyncClient:
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        }
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
            trust_env=self.trust_env,
        )

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        try:
            async with self._client() as client:
                resp = await client.request(method, path, json=json)
        except httpx.ConnectError as e:
            raise HAError(
                "unreachable",
                "无法连接 Home Assistant（" + self.base_url + "）。请检查 HA_URL 是否正确、HA 是否在运行、本服务能否访问该地址。",
                str(e),
            ) from e
        except httpx.TimeoutException as e:
            raise HAError(
                "timeout",
                "访问 Home Assistant 超时（" + str(int(self.timeout)) + "s）。可增大环境变量 HA_TIMEOUT，或检查网络/HA 负载。",
                str(e),
            ) from e
        except httpx.HTTPError as e:
            raise HAError("network_error", "访问 Home Assistant 发生网络错误：" + str(e), str(e)) from e

        if resp.status_code in (401, 403):
            raise HAError(
                "auth_failed",
                "Home Assistant 鉴权失败：令牌无效/已过期，或该令牌无权限。请重新生成长期访问令牌并更新 HA_TOKEN。",
                "HTTP " + str(resp.status_code),
            )
        if resp.status_code == 404:
            raise HAError("not_found", "请求的资源不存在（HTTP 404）。", method + " " + path)
        if resp.status_code >= 500:
            raise HAError("upstream_error", "Home Assistant 内部错误（HTTP " + str(resp.status_code) + "）。", resp.text[:300])
        if resp.status_code >= 400:
            raise HAError("http_error", "Home Assistant 返回错误（HTTP " + str(resp.status_code) + "）。", resp.text[:300])
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return resp.text
        return None

    # ---------------- 上层 API ----------------
    async def get_states(self) -> list:
        data = await self._request("GET", "/api/states")
        return data if isinstance(data, list) else []

    async def get_services(self) -> list:
        """GET /api/services，返回服务目录（每个 domain 及其服务与参数字段）。"""
        data = await self._request("GET", "/api/services")
        return data if isinstance(data, list) else []

    async def get_state(self, entity_id: str) -> dict | None:
        try:
            return await self._request("GET", "/api/states/" + entity_id)
        except HAError as e:
            if e.code == "not_found":
                return None
            raise

    async def call_service(self, domain: str, service: str, data: dict) -> Any:
        return await self._request("POST", "/api/services/" + domain + "/" + service, json=data)

    async def ping(self) -> bool:
        await self._request("GET", "/api/")
        return True

    # ---------------- 设备/实体注册表（WebSocket） ----------------
    def _derive_ws_url(self) -> str:
        u = self.base_url
        if u.startswith("https://"):
            u = "wss://" + u[len("https://"):]
        elif u.startswith("http://"):
            u = "ws://" + u[len("http://"):]
        return u.rstrip("/") + "/api/websocket"

    def _ws_ssl(self):
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def get_registries(self, force: bool = False) -> dict | None:
        """读取 HA 的设备注册表与实体注册表（带 TTL 缓存）。

        返回 {"devices": [...], "entities": [...]}；WebSocket 不可用/无权限时返回 None，
        调用方应降级为按实体展示，不影响其它功能。
        """
        if not self.ws_enabled or websockets is None:
            return None
        now = time.time()
        if not force and self._registry_cache is not None and (now - self._registry_ts) < self.registry_ttl:
            return self._registry_cache
        try:
            data = await self._ws_registries()
        except HAError:
            data = None
        except Exception:  # noqa: BLE001 注册表仅是增强信息，任何异常都降级
            data = None
        self._registry_cache = data
        self._registry_ts = now
        return data

    async def _ws_registries(self) -> dict:
        kwargs: dict = {"open_timeout": self.timeout, "close_timeout": 2}
        if self.ws_url.startswith("wss://"):
            kwargs["ssl"] = self._ws_ssl()
        try:
            async with websockets.connect(self.ws_url, **kwargs) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.timeout))
                if hello.get("type") == "auth_required":
                    await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                    auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.timeout))
                    if auth.get("type") != "auth_ok":
                        raise HAError("auth_failed", "Home Assistant WebSocket 鉴权失败：请检查 HA_TOKEN 是否有效。")
                elif hello.get("type") != "auth_ok":
                    raise HAError("ws_error", "Home Assistant WebSocket 握手异常：" + str(hello.get("type")))
                cmds = [(1, "config/device_registry/list"), (2, "config/entity_registry/list")]
                for cid, ctype in cmds:
                    await ws.send(json.dumps({"id": cid, "type": ctype}))
                out: dict = {"devices": [], "entities": []}
                for _ in cmds:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.timeout))
                    if msg.get("type") != "result":
                        continue
                    if not msg.get("success"):
                        err = msg.get("error") or {}
                        raise HAError("registry_error", "读取 Home Assistant 注册表失败：" + str(err.get("message") or err))
                    if msg.get("id") == 1:
                        out["devices"] = msg.get("result") or []
                    elif msg.get("id") == 2:
                        out["entities"] = msg.get("result") or []
                return out
        except asyncio.TimeoutError as e:
            raise HAError("timeout", "连接 Home Assistant WebSocket 超时。", str(e)) from e
        except HAError:
            raise
        except Exception as e:  # noqa: BLE001
            raise HAError("ws_error", "连接 Home Assistant WebSocket 失败：" + str(e), str(e)) from e

    async def resolve_entity(self, ref: str) -> tuple:
        """把 entity_id 或友好名解析成一个实体。

        返回 (entity, candidates)：
        - entity 非空：唯一命中
        - entity 为空且 candidates 非空：多个候选（歧义），需调用方消歧
        - 两者皆空：未找到
        """
        ref = (ref or "").strip()
        if not ref:
            return None, []
        # 1) 直接当作 entity_id
        if "." in ref and " " not in ref:
            st = await self.get_state(ref)
            if st is not None:
                return st, []
        states = await self.get_states()
        low = ref.lower()

        def fname(s: dict) -> str:
            return str((s.get("attributes") or {}).get("friendly_name", ""))

        exact = [s for s in states if fname(s).lower() == low]
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            return None, exact
        contains = [s for s in states if low in fname(s).lower()]
        if len(contains) == 1:
            return contains[0], []
        if len(contains) > 1:
            return None, contains
        byid = [s for s in states if low in str(s.get("entity_id", "")).lower()]
        if len(byid) == 1:
            return byid[0], []
        return None, byid
