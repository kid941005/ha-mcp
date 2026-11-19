# -*- coding: utf-8 -*-
"""homeassistant-xiaozhiserver — 小智 MCP 服务（Home Assistant 桥）。

把 Home Assistant 的设备能力封装成标准 MCP 工具，供小智（xiaozhi-esp32-server）
或其他 MCP 客户端调用。默认以 streamable-http 暴露在 /mcp，也支持 sse / stdio。

模块划分：
  config.py    环境变量解析与运行常量（非法值回退并告警）
  client.py    HA REST/WebSocket 客户端、鉴权与错误归一化
  service.py   动作映射、状态/属性预期、回读校验
  danger.py    危险设备判定与一次性确认令牌
  views.py     实体摘要、可控性标记、按设备聚合
  server.py    MCP 工具定义、健康检查与进程入口
"""
from __future__ import annotations

import os
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .config import (
    DEFAULT_LIST_LIMIT,
    LOG_LEVEL,
    MAX_LIST_LIMIT,
    MCP_HOST,
    MCP_PATH,
    MCP_PORT,
    MCP_SSE_PATH,
    MCP_STATELESS,
)
from .client import HAError, HAClient
from .danger import _danger_block, _danger_flags
from .service import (
    DOMAIN_ACTIONS,
    _apply_and_confirm,
    _domain_of,
    _field_brief,
    _friendly_name,
    _hint,
    build_service,
)
from .views import (
    _brief_attributes,
    _controllability,
    _entity_view,
    _group_by_device,
    _registry_maps,
)

mcp = FastMCP(
    "homeassistant-xiaozhiserver",
    instructions=(
        "把 Home Assistant 的智能家居设备封装为 MCP 工具。"
        "查看设备用 list_devices（可按 keyword 名称关键字或 domain 设备类型过滤，支持分页；"
        "结果带 controllable 标记说明是否可控制；controllable_only=true 只看可控设备；"
        "group_by=\"device\" 按物理设备聚合多个实体）；"
        "查看单个设备状态用 get_device_state（entity 可传 entity_id 或中文名称；"
        "默认只返回关键属性，include_all_attributes=true 可返回完整属性）；"
        "查询某类设备支持哪些服务与参数用 list_services；"
        "常规控制用 control_device（action 见工具描述，value 用于亮度/音量/温度等，"
        "控制后自动回读校验状态与属性）；"
        "对 lock、alarm_control_panel 以及车库门/门窗类 cover 等危险设备的操作会先返回 "
        "confirmation_required 与 confirm_token；必须让用户明确确认后，带 confirm_token 再调用一次；"
        "需要执行上述未覆盖的任意 HA 服务时用 call_service（服务名 + 参数 JSON）；"
        "不确定设备类型时先用 list_domains 查看有哪些类别。"
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path=MCP_PATH,
    sse_path=MCP_SSE_PATH,
    log_level=LOG_LEVEL,
    stateless_http=MCP_STATELESS,
)

_ha: Optional[HAClient] = None


def get_ha() -> HAClient:
    global _ha
    if _ha is None:
        _ha = HAClient()
    return _ha


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
        elif mode in ("entity", "entities", "实体"):
            items = views
            grouped_by = "entity"
        else:
            return {"ok": False, "code": "invalid_group_by",
                    "message": "group_by 仅支持 entity 或 device，收到：" + str(group_by) + "。entity 按实体逐条返回，device 按物理设备聚合。"}

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
async def get_device_state(entity: str, include_all_attributes: bool = False) -> dict:
    """查询单个设备的当前状态与关键属性。

    entity 可传 entity_id（如 light.living_room）或设备的友好名称（如 客厅灯），
    服务端会先直查、再按名称精确/模糊匹配。多义时返回候选列表。

    默认只返回关键属性（亮度、色温、音量、温度、当前位置等），避免媒体/摄像头等
    实体的巨大 attributes 占用上下文；需要完整属性时传 include_all_attributes=true。
    适用场景：用户问「客厅灯现在开着吗」「卧室温度多少」「空调什么状态」。
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
        all_attrs = st.get("attributes")
        if not isinstance(all_attrs, dict):
            all_attrs = {}
        if include_all_attributes:
            attrs, truncated = all_attrs, False
        else:
            attrs, truncated = _brief_attributes(all_attrs)
        s["attributes"] = attrs
        s["attributes_truncated"] = truncated
        s["attribute_count"] = len(all_attrs)
        msg = s["name"] + " 当前状态：" + str(s["state"])
        if s.get("unit"):
            msg += " " + str(s["unit"])
        if truncated:
            msg += "（默认仅关键属性；完整属性请传 include_all_attributes=true）"
        return {"ok": True, "entity": s, "message": msg}
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": _hint()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "查询设备状态失败：" + str(e)}


@mcp.tool()
async def control_device(entity: str, action: str, value: Optional[Any] = None,
                         confirm_token: Optional[str] = None) -> dict:
    """控制设备（开关、亮度、音量、温度、颜色等）。

    参数：
      entity  目标设备：entity_id 或友好名称（会先精确/模糊匹配）。
      action  动作，支持：
        turn_on/turn_off   开关（cover 自动映射 open/close，vacuum 映射 start/stop）
        toggle             切换
        open/close/stop/pause/continue/play  按设备能力映射到对应服务
        lock/unlock        上锁 / 解锁（仅 lock）
        set_brightness     value=亮度百分比 1-100（仅 light）
        set_volume         value=音量百分比 1-100（media_player）
        volume_mute/volume_unmute  静音/取消静音
        set_temperature    value=目标温度（摄氏度，climate）
        set_color_temp     value=色温（开尔文，light）
        set_color          value=颜色 #RRGGBB、"r,g,b" 或 [r,g,b]（light）
      value   动作参数，按 action 需要提供：数字（亮度/音量百分比、温度、色温）、
              颜色（#RRGGBB 或 "r,g,b"，也可传 [r,g,b] 数组）。
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
        action_name = (action or "").strip().lower()
        allowed = info.get("actions") or []
        if action_name not in allowed:
            return {
                "ok": False, "code": "unsupported_action",
                "message": "『" + _friendly_name(st) + "』（" + domain + "）不支持动作：" + str(action) +
                           "。该类型可用动作：" + ", ".join(allowed) +
                           "。如需调用其它服务，请先用 list_services 查询，再用 call_service 调用。",
                "entity_id": eid, "domain": domain, "actions": allowed,
                "hint": "control_device 只做常用动作封装；任意 HA 服务请用 call_service。",
            }
        sd, svc, extra = build_service(action_name, domain, value)
        payload = {"entity_id": eid}
        payload.update(extra)
        guard = _danger_block(st, sd, svc, payload, confirm_token)
        if guard is not None:
            return guard
        result = await _apply_and_confirm(ha, st, sd, svc, payload, action_name)
        result["ok"] = True
        result["action"] = action_name
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
        if "entity_id" in extra:
            return {"ok": False, "code": "invalid_data",
                    "message": "data 不应包含 entity_id；目标实体由 entity 参数指定，避免覆盖解析结果。"}
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
        if e.code in ("not_found", "http_error", "upstream_error", "invalid_data", "invalid_value"):
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
    同时返回危险设备明细（含 garage/door 类 cover），便于确认哪些操作需要二次确认。
    适用场景：用户问「家里都有哪些类型的设备」「哪些设备操作要确认」。
    """
    try:
        ha = get_ha()
        states = await ha.get_states()
        counts: dict = {}
        dangerous_entities: list = []
        for s in states:
            eid = str(s.get("entity_id", ""))
            d = _domain_of(eid)
            if d:
                counts[d] = counts.get(d, 0) + 1
            is_danger, reason = _danger_flags(eid, s.get("attributes") or {})
            if is_danger:
                dangerous_entities.append({
                    "entity_id": eid,
                    "name": _friendly_name(s),
                    "domain": d,
                    "state": s.get("state"),
                    "reason": reason,
                })
        counts = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
        ctrl = [d for d in counts if DOMAIN_ACTIONS.get(d)]
        danger = sorted({str(e.get("domain") or "") for e in dangerous_entities if e.get("domain")})
        msg = "共 " + str(len(counts)) + " 种设备类型：" + ", ".join(k + "(" + str(v) + ")" for k, v in counts.items())
        if ctrl:
            msg += "；可控类型：" + ", ".join(ctrl)
        if danger:
            msg += "；需二次确认的危险类型：" + ", ".join(danger)
        if dangerous_entities:
            msg += "（共 " + str(len(dangerous_entities)) + " 个危险实体）"
        return {"ok": True, "domains": counts, "controllable_domains": ctrl,
                "dangerous_domains": danger,
                "dangerous_entities": dangerous_entities[:50],
                "dangerous_entity_count": len(dangerous_entities),
                "message": msg}
    except HAError as e:
        return {"ok": False, **e.to_dict(), "hint": _hint()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "internal_error", "message": "查询设备类型失败：" + str(e)}


async def _health(request):
    """liveness：只表示 MCP 进程/HTTP 服务在运行，不依赖 HA。"""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "status": "ok",
        "service": "homeassistant-xiaozhiserver",
        "transport": os.environ.get("MCP_TRANSPORT", "streamable-http"),
    })


async def _ready(request):
    """readiness：HA 可达且鉴权通过才返回 200，否则 503。"""
    from starlette.responses import JSONResponse
    info: dict = {
        "status": "ok",
        "service": "homeassistant-xiaozhiserver",
        "transport": os.environ.get("MCP_TRANSPORT", "streamable-http"),
    }
    code = 200
    try:
        await get_ha().ping()
        info["ha"] = "reachable"
    except HAError as e:
        code = 503
        info.update({"status": "error", "ha": "error", "ha_error": e.to_dict()})
    except Exception as e:  # noqa: BLE001
        code = 503
        info.update({"status": "error", "ha": "error", "ha_error": str(e)})
    return JSONResponse(info, status_code=code)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http").strip().lower()
    allowed = ("streamable-http", "sse", "stdio")
    if transport not in allowed:
        raise SystemExit(
            "无效的 MCP_TRANSPORT=" + repr(transport) + "；允许的值为：" + ", ".join(allowed)
        )
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
    app.router.routes.append(Route("/ready", _ready, methods=["GET"]))
    uvicorn.run(
        app,
        host=MCP_HOST,
        port=MCP_PORT,
        log_level=LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
