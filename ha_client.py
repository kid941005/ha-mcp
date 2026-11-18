# -*- coding: utf-8 -*-
"""Home Assistant REST API 客户端。

设计要点：
- 所有配置只从环境变量读取，绝不硬编码地址或令牌。
- 网络/鉴权/超时等异常统一转换为 HAError（带错误码 + 中文可读信息），
  由上层 MCP 工具捕获后转成结构化结果，保证服务不崩溃。
"""
from __future__ import annotations

import os
from typing import Any

import httpx


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
