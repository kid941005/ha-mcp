# 小智 MCP 服务（Home Assistant 桥）

把 **Home Assistant** 的智能家居设备封装成标准 **MCP（Model Context Protocol）** 工具，
供**小智（xiaozhi-esp32-server）**或任意 MCP 客户端通过自然语言调用，支持 Docker 一键部署。

> 核心特性：配置只走环境变量（不硬编码）、只读/控制一体化、服务能力自描述（`list_services`）、
> 通用服务调用（`call_service`，覆盖任意 HA 服务）、控制后状态+属性四态回读校验（confirmed/no_change/mismatch/accepted_pending）、
> 危险设备（门锁/安防面板/车库门/门窗类 cover）二次确认令牌防误操作、
> 每个实体带 `controllable` 可控标记、`dangerous` 危险标记与 `actions` 动作清单、`group_by="device"` 按物理设备聚合、
> 错误全部转为中文可读提示且服务不崩溃、设备列表支持过滤与分页。

---

## 1. 架构与数据流

```
用户语音
  └─> 小智后端（ASR → LLM → function calling）
        └─> MCP 客户端（ServerMCPManager，server_mcp 通道）
              └─> 本服务  /mcp  (streamable-http)
                    ├─> Home Assistant REST API (/api/states、/api/services/...)
                    └─> Home Assistant WebSocket API (/api/websocket，设备/实体注册表，用于按设备聚合)
                          └─> 真实设备
```

- 小智后端启动时对本服务做 `list_tools`，把工具定义并入 LLM 的 function 列表；
- 用户自然语言 → LLM 选择工具与参数 → 小智后端 `call_tool` → 本服务翻译为 HA REST 调用 → 返回给 LLM 生成回复；
- 本服务**无状态**，水平扩展即可；只从环境变量读取 `HA_URL` / `HA_TOKEN`。

---

## 2. 目录结构

```
ha-mcp/
├── server.py                              # MCP 服务 + 6 个工具
├── ha_client.py                           # HA REST 客户端 / 错误处理 / 实体解析
├── requirements.txt
├── Dockerfile
├── docker-compose.yml                     # 独立构建运行
├── .env.example
├── integration/
│   ├── .mcp_server_settings.json          # 小智后端「服务端 MCP」配置样例
│   └── docker-compose.ha-mcp.fragment.yml # 并入现有 deploy compose 的片段
├── tests/
│   ├── fake_ha.py                         # 离线 HA 模拟器（含静默假成功复刻）
│   ├── e2e_mcp_client.py                  # 端到端 MCP 客户端测试
│   └── run_local_test.sh
└── README.md
```

---

## 3. 环境变量清单

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `HA_URL` | ✅ | — | Home Assistant 地址，如 `https://ha.hqgg.top:59826`（结尾不要带 `/`） |
| `HA_TOKEN` | ✅ | — | HA 长期访问令牌（个人资料 → 安全 → 长期访问令牌） |
| `MCP_TRANSPORT` | | `streamable-http` | `streamable-http` / `sse` / `stdio` |
| `MCP_HOST` | | `0.0.0.0` | 监听地址 |
| `MCP_PORT` | | `8090` | 监听端口 |
| `MCP_PATH` | | `/mcp` | streamable-http 路径 |
| `MCP_SSE_PATH` | | `/sse` | sse 路径（仅 sse 传输时用） |
| `HA_TIMEOUT` | | `10` | 访问 HA 的超时秒数 |
| `HA_VERIFY_SSL` | | `true` | 是否校验 HA 的 TLS 证书（自签名证书设 `false`） |
| `HA_TRUST_ENV` | | `false` | 是否读取宿主 HTTP(S)_PROXY/NO_PROXY（默认关闭，避免代理干扰直连 HA） |
| `HA_WS_ENABLED` | | `true` | 是否用 WebSocket 读取设备/实体注册表以支持「按设备聚合」 |
| `HA_WS_URL` | | 由 `HA_URL` 推导 | HA WebSocket 地址，如 `ws://homeassistant:8123/api/websocket` |
| `HA_REGISTRY_TTL` | | `60` | 设备/实体注册表缓存秒数 |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DEFAULT_LIST_LIMIT` | | `50` | `list_devices` 默认每页条数 |
| `MAX_LIST_LIMIT` | | `500` | `list_devices` 单次最大条数（防止一次返回过多） |
| `SERVICE_CONFIRM_DELAY` | | `0.6` | 服务调用后回读前的等待秒数（等待设备状态刷新） |
| `SERVICE_CONFIRM_RETRIES` | | `1` | 服务调用后回读的额外重试次数 |
| `SERVICE_CONFIRM_INTERVAL` | | 同 `SERVICE_CONFIRM_DELAY` | 回读轮询间隔秒数 |
| `SERVICE_CONFIRM_TIMEOUT` | | `4.0` | 回读确认总超时秒数（窗口内持续轮询直到匹配预期） |
| `DANGER_CONFIRM_ENABLED` | | `true` | 是否对危险设备启用二次确认（设 `false` 可关闭） |
| `DANGER_CONFIRM_TTL` | | `120` | 危险操作确认令牌有效期（秒），一次性使用 |
| `DANGEROUS_DOMAINS` | | `lock,alarm_control_panel` | 视为危险设备的 domain（逗号分隔） |
| `DANGEROUS_COVER_CLASSES` | | `garage,door,gate,window` | 视为危险 cover 的 device_class（逗号分隔） |

> 安全：`HA_TOKEN` 只通过环境变量注入，不写入镜像、不写入代码、不写进 Git。

---

## 4. 快速开始

### 4.1 Docker Compose（推荐）

```bash
cd ha-mcp
cp .env.example .env
# 编辑 .env，填入 HA_URL 与 HA_TOKEN
docker compose up -d --build
docker compose logs -f          # 观察启动日志
curl -s http://127.0.0.1:8090/health   # 应返回 {"status":"ok",...,"ha":"reachable"}
```

### 4.2 docker build / run

```bash
docker build -t ha-mcp:latest .

docker run -d --name ha-mcp \
  -p 8090:8090 \
  -e HA_URL=https://ha.hqgg.top:59826 \
  -e HA_TOKEN=你的HA长期访问令牌 \
  --restart unless-stopped \
  ha-mcp:latest
```

> 若 HA 用的是自签名证书，追加 `-e HA_VERIFY_SSL=false`。

### 4.3 本地开发（无 Docker）

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export HA_URL=https://ha.hqgg.top:59826
export HA_TOKEN=你的HA长期访问令牌
python server.py
```

服务启动后：
- MCP 端点：`http://<host>:8090/mcp`（streamable-http）
- 健康检查：`http://<host>:8090/health`

---

## 5. MCP 工具说明

所有工具返回 **JSON**（结构统一为 `{"ok": true/false, ...}`），便于大模型直接理解。
失败时 `ok=false` 并带 `code` / `message` / `hint`，**不会抛异常堆栈**。

### 5.1 `list_devices` — 获取设备列表

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | string | 否 | 名称、entity_id 或所属设备名关键字，模糊匹配（如「客厅」「灯」） |
| `domain` | string | 否 | 设备类型过滤：`light`/`switch`/`sensor`/`climate`/`cover`/`media_player`/`fan`/`vacuum`/`scene` 等 |
| `controllable_only` | bool | 否 | 只返回可控制设备（自动排除 sensor 等只读实体） |
| `group_by` | string | 否 | `entity`（默认，逐实体）或 `device`（按物理设备聚合，一个设备的多个实体合并） |
| `limit` | int | 否 | 每页条数，默认取 `DEFAULT_LIST_LIMIT`，上限 `MAX_LIST_LIMIT` |
| `offset` | int | 否 | 起始偏移，默认 0 |

返回（entity 模式）：`{ok,grouped_by:"entity",registry_available,total,count,offset,limit,has_more,next_offset,devices:[{entity_id,name,domain,state,unit,device_class,controllable,actions,controllable_reason,dangerous,danger_reason,device_id,device_name,area_id}],message}`
返回（device 模式）：`{ok,grouped_by:"device",registry_available,total,count,...,devices:[{device_id,name,manufacturer,model,area_id,controllable,dangerous,entity_count,entities:[...]}],message}`
> `registry_available:false` 表示 HA WebSocket 注册表不可用（令牌无权限/WS 不通），此时自动降级为按实体展示，不影响状态查询与控制。

### 5.2 `get_device_state` — 获取设备状态

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `entity` | string | 是 | entity_id（如 `light.living_room`）或友好名称（如「客厅灯」）；服务端自动解析，多义时返回候选 |

返回：`{ok,entity:{entity_id,name,domain,state,unit,attributes,controllable,actions,controllable_reason,dangerous,danger_reason,device_id,device_name,area_id,last_changed,last_updated},message}`

### 5.3 `control_device` — 控制设备

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `entity` | string | 是 | 目标设备：entity_id 或友好名称 |
| `action` | string | 是 | 见下表 |
| `value` | number | 视动作 | 亮度/音量百分比(1-100)、温度(℃)、色温(K) |
| `confirm_token` | string | 危险设备时 | 危险设备二次确认令牌（首次调用返回，用户确认后再带此参数重试） |

`action` 取值（服务端按设备 domain 自动映射到 HA 服务）：

| action | 作用 | 需要的 value |
|---|---|---|
| `turn_on` / `turn_off` | 开关（cover 自动→ open_cover/close_cover，vacuum → start/stop） | — |
| `toggle` | 切换 | — |
| `open` / `close` / `stop` / `pause` / `continue` / `play` | 按设备能力映射 | — |
| `lock` / `unlock` | 上锁 / 解锁（lock） | — |
| `set_brightness` | 设置亮度（light） | 1-100 |
| `set_volume` | 设置音量（media_player） | 1-100 |
| `volume_mute` / `volume_unmute` | 静音 / 取消静音 | — |
| `set_temperature` | 设置温度（climate） | 摄氏度 |
| `set_color_temp` | 设置色温（light） | 开尔文 |
| `set_color` | 设置颜色（light） | `#RRGGBB` 或 `"r,g,b"` |

返回：`{ok,entity_id,name,action,service,response_kind,status,expected_state,before,after,changed,verified,verification,message}`
**控制后状态+属性回读校验**：服务调用后会在 `SERVICE_CONFIRM_TIMEOUT` 秒内轮询回读，既核对状态（如 turn_on→on），也核对属性（亮度、温度、音量、色温、颜色等），`status` 为：
- `confirmed`：回读确认状态/属性已达到预期（且确有变化）；
- `no_change`：HA 已接受但无变化；若 `verified=true` 表示设备本来就是目标状态；
- `mismatch`：回读看到变化，但状态或属性与预期不符（设备能力/参数问题）；
- `accepted_pending`：HA 返回 null（新版 HA 常见），已下发但回读窗口内仍未完全匹配。
`verification` 给出 `attempts`（回读次数）、`elapsed_ms`、`expectations`、`checks`、`mismatches`，便于调用方判断是否真正生效，避免 HA「对不存在实体也返回 200」造成的静默假成功。
> 若设备类型本身不可控制（如 `sensor`），返回 `code:"not_controllable"` 并说明原因，不会误发服务。

> **危险设备二次确认（防误操作）**：`lock`、`alarm_control_panel` 以及 device_class 为 `garage`/`door`/`gate`/`window` 的 `cover` 默认视为危险设备。首次调用会返回 `code:"confirmation_required"`、`dangerous:true`、`requires_confirmation:true`、`confirm_token`、`expires_in` 与 `risk_reason`，**不会执行**；请先向用户明确确认动作，再带同一个 `confirm_token` 重试。令牌**一次性**、绑定“实体 + 服务 + 参数”，默认 `DANGER_CONFIRM_TTL=120` 秒有效；令牌无效/过期/操作不匹配分别返回 `invalid_confirm_token` / `confirm_token_expired` / `confirm_token_mismatch`。

### 5.4 `list_services` — 查询设备能力（服务目录）

让调用方先了解「某类/某个设备支持哪些服务、每个服务要什么参数」，再精确调用（能力自描述）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `entity` | string | 否 | 设备 entity_id 或友好名称；给出时只返回该设备所属 domain 的服务 |
| `domain` | string | 否 | 直接指定设备类型（如 `light`/`climate`/`media_player`） |
| `keyword` | string | 否 | 按服务名或描述关键字过滤（如 `temperature`） |
| `detailed` | bool | 否 | 是否返回字段参数明细（指定 entity/domain 时默认 true，否则仅名称） |

返回：`{ok,domain,domain_count,service_count,domains:[{domain,service_count,services:[{service,name?,description?,fields?:[{name,required,selector?,example?}]}]}],message}`
数据源为 HA `GET /api/services`。

### 5.5 `call_service` — 通用服务调用

执行 `control_device` 未封装的任意 HA 服务（如空调模式、风扇风速、自定义脚本）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `entity` | string | 是 | 目标设备：entity_id 或友好名称 |
| `service` | string | 是 | 服务名：`turn_on`（默认用设备 domain）或 `climate.set_hvac_mode`（显式带 domain 以它为准） |
| `data` | object | 否 | 额外参数 JSON，如 `{"temperature":26}`、`{"hvac_mode":"heat"}`、`{"rgb_color":[255,0,0]}` |
| `confirm_token` | string | 危险设备时 | 危险设备二次确认令牌，机制同 `control_device` |

返回：`{ok,entity_id,name,service,response_kind,status,expected_state,before,after,changed,verified,verification,message}`
`status` 四态同 `control_device`：`confirmed` / `no_change` / `mismatch` / `accepted_pending`；`verification` 含 `attempts`、`elapsed_ms`、`checks`、`mismatches`。

> 危险设备保护与 `control_device` 相同：`lock`/`alarm_control_panel` 及车库门/门窗类 `cover` 的首次调用只返回 `confirmation_required` 与 `confirm_token`，确认后带 token 重试才执行。

### 5.6 `list_domains` — 获取设备类型

无参数。返回 `{ok,domains:{light:3,switch:2,...},controllable_domains:["light","switch",...],dangerous_domains:["lock",...],message}`，便于了解家庭设备类别、哪些类型可控制、哪些类型需二次确认，或选择 `domain` 过滤。

---

## 6. 接入小智后端（xiaozhi-esp32-server）

小智后端的外部 MCP 接入有两条路径，本服务两者都支持。

### 方式 B（推荐）：服务端 MCP（ServerMCPManager）

原理：xiaozhi-server 通过 `core/providers/tools/server_mcp` 读取 `data/.mcp_server_settings.json`，
用标准 MCP 客户端（streamable-http / sse / stdio）直连本服务。

**步骤：**

1. 让 `ha-mcp` 与 `xiaozhi-server` 处于**同一个 Docker 网络**（例如都写进 `deploy/docker-compose.yml`，或用 `docker network connect`）。
   见 `integration/docker-compose.ha-mcp.fragment.yml`，追加到 `deploy/docker-compose.yml` 的 `services:` 下即可。
2. 在宿主机 **`deploy/data/.mcp_server_settings.json`** 写入（该目录已挂载到容器 `/opt/xiaozhi-esp32-server/data`）：

```json
{
  "mcpServers": {
    "ha": {
      "url": "http://ha-mcp:8090/mcp",
      "transport": "streamable-http",
      "timeout": 30,
      "sse_read_timeout": 300
    }
  }
}
```

> ⚠️ `transport` 必须显式写 `"streamable-http"`；不写时 xiaozhi 默认按 `sse` 连接，会失败。

3. 重启 xiaozhi-server：

```bash
cd deploy && docker compose restart xiaozhi-esp32-server
docker logs xiaozhi-esp32-server 2>&1 | grep -i "MCP"
# 期望看到：初始化服务端MCP客户端: ha  ...  可用工具: ['list_devices','get_device_state','control_device','list_services','call_service','list_domains']
```

4. 之后用户在语音里提到的设备操作，会被 LLM 自动路由到上述工具。

### 方式 A：MCP 接入点（mcp-endpoint-server）

适用于「MCP 接入点」集中管理（工具服务以 **stdio** 方式运行，再由 `mcp_pipe.py` 通过 WebSocket 连到接入点地址 `ws://<host>:8004/mcp_endpoint/mcp/?token=xxx`）：

```bash
# 以 stdio 方式运行本服务
export HA_URL=... HA_TOKEN=... MCP_TRANSPORT=stdio
python mcp_pipe.py server.py --endpoint "ws://<host>:8004/mcp_endpoint/mcp/?token=xxx"
```

> `mcp_pipe.py` 来自 xinnan-tech/mcp-endpoint-server，按该仓库说明使用；接入点在智控台配置。

---

## 7. 使用示例（自然语言 → 工具 → 参数 → 返回）

| 用户说 | 调用工具 | 参数 | 返回（节选） |
|---|---|---|---|
| 家里有哪些设备？ | `list_devices` | `{}` | `{"ok":true,"total":7,"count":7,"has_more":false,"devices":[...]}` |
| 列出所有的灯 | `list_devices` | `{"domain":"light"}` | `total=1, devices=[{"name":"客厅灯","state":"off"}]` |
| 客厅都有什么设备？ | `list_devices` | `{"keyword":"客厅"}` | `total=3` |
| 哪些设备能控制？ | `list_devices` | `{"controllable_only":true}` | `total=6`（自动排除只读 sensor） |
| 按物理设备看看家里有什么 | `list_devices` | `{"group_by":"device"}` | `devices=[{"name":"客厅环境组","entity_count":2,"controllable":true,"entities":[...]}] ` |
| 家里有哪些类型的设备？ | `list_domains` | `{}` | `{"domains":{"light":1,"switch":1,...}}` |
| 客厅灯现在开着吗？ | `get_device_state` | `{"entity":"客厅灯"}` | `{"ok":true,"entity":{"state":"off","attributes":{...}}}` |
| 把客厅灯亮度调到 50% | `control_device` | `{"entity":"客厅灯","action":"set_brightness","value":50}` | `{"ok":true,"service":"light.turn_on","changed":true,"after":{"state":"on","brightness":127}}` |
| 关掉电风扇 | `control_device` | `{"entity":"电风扇","action":"turn_off"}` | `{"ok":true,"service":"switch.turn_off","changed":true}` |
| 空调调到 26 度 | `control_device` | `{"entity":"空调","action":"set_temperature","value":26}` | `{"ok":true,"service":"climate.set_temperature",...}` |
| 电视音量调到 30 | `control_device` | `{"entity":"电视","action":"set_volume","value":30}` | `{"ok":true,"service":"media_player.volume_set",...}` |
| 打开窗帘 | `control_device` | `{"entity":"窗帘","action":"open"}` | `{"ok":true,"service":"cover.open_cover","changed":true,"after":{"state":"open"}}` |
| 打开大门门锁 | `control_device` | `{"entity":"大门门锁","action":"unlock"}` | `{"ok":false,"code":"confirmation_required","dangerous":true,"confirm_token":"...","expires_in":120}`（**未执行**） |
| （用户确认后）打开大门门锁 | `control_device` | `{"entity":"大门门锁","action":"unlock","confirm_token":"..."}` | `{"ok":true,"status":"confirmed","after":{"state":"unlocked"}}` |
| 空调能做什么操作？ | `list_services` | `{"entity":"空调"}` | `{"ok":true,"domain":"climate","service_count":4,"services":[{"service":"set_temperature","fields":[{"name":"temperature","required":true}]},...]}` |
| 空调设成制热模式 | `call_service` | `{"entity":"空调","service":"climate.set_hvac_mode","data":{"hvac_mode":"heat"}}` | `{"ok":true,"status":"confirmed","after":{"state":"heat"}}` |
| 把客厅灯切成暖光 | `call_service` | `{"entity":"客厅灯","service":"light.turn_on","data":{"color_temp_kelvin":2700}}` | `{"ok":true,"status":"confirmed","verified":true,"verification":{"attempts":1,...}}` |
| 把客厅灯亮度调到 80（设备没调到） | `call_service` | `{"entity":"客厅灯","service":"light.turn_on","data":{"brightness_pct":80}}` | `{"ok":true,"status":"mismatch","verified":false,"verification":{"mismatches":[{"field":"brightness","expected":204,"actual":127}]}}` |

失败示例（清晰提示，不崩溃）：

```json
// 设备不存在
{"ok": false, "code": "not_found", "message": "未找到设备『不存在设备』，无法控制。可先用 list_devices 查询。"}
// 动作不支持
{"ok": false, "code": "unsupported_action", "message": "不支持的控制动作：fly。可用动作：turn_on, turn_off, ..."}
// 设备不可控制（如传感器）
{"ok": false, "code": "not_controllable", "message": "『客厅温度』（sensor）不可控制：该类型无内置控制动作..."}
// 危险设备未二次确认
{"ok": false, "code": "confirmation_required", "dangerous": true, "confirm_token": "...", "expires_in": 120, "message": "『大门门锁』是危险设备..."}
```

---

## 8. 错误处理与排查

### 8.1 错误码

| code | 含义 | 处理建议 |
|---|---|---|
| `config_missing` | 缺少 `HA_URL` / `HA_TOKEN` | 补齐环境变量后重启 |
| `auth_failed` | 令牌无效/过期/无权限（HTTP 401/403） | 重新生成长期令牌，更新 `HA_TOKEN` |
| `unreachable` | 连不上 HA | 检查 `HA_URL`、网络连通、容器能否访问该地址 |
| `timeout` | 请求 HA 超时 | 增大 `HA_TIMEOUT` 或检查网络/HA 负载 |
| `upstream_error` | HA 5xx | 查看 HA 自身日志/健康状况 |
| `not_found` | 实体/资源不存在 | 用 `list_devices` 查正确 entity_id |
| `not_controllable` | 设备类型无内置控制动作（如 sensor） | 只用 `get_device_state` 查询，或 `list_services` 后用 `call_service` |
| `ambiguous` | 名称匹配到多个设备 | 改用更精确名称或 entity_id |
| `missing_value` / `invalid_value` | 动作缺参数或取值越界 | 按工具描述传 1-100 等合法值 |
| `unsupported_action` | 动作不支持 | 使用 `SUPPORTED_ACTIONS` 中的动作 |
| `invalid_service` | `call_service` 的 service 为空/格式错 | 用 `list_services` 查服务名，格式 `service` 或 `domain.service` |
| `invalid_data` | `data` 不是 JSON 对象 | 传键值对对象，如 `{"temperature":26}` |
| `no_services` | 指定 domain 无可用服务 | 核对 domain 或用 `list_services` 不带过滤查看 |
| `confirmation_required` | 危险设备首次调用，未带 `confirm_token`（未执行） | 向用户确认后带返回的 `confirm_token` 重试 |
| `invalid_confirm_token` | 令牌无效或已使用（一次性） | 重新发起原操作获取新令牌 |
| `confirm_token_expired` | 令牌超过 `DANGER_CONFIRM_TTL` | 使用新返回的 `confirm_token`（已自动刷新） |
| `confirm_token_mismatch` | 令牌与本次实体/服务/参数不匹配 | 重新发起原操作获取新令牌，勿改参数复用 |

### 8.2 排查步骤

1. **看服务健康**：`curl http://127.0.0.1:8090/health`
   - `"ha":"reachable"` 正常；`"ha":"error"` 时看 `ha_error` 的具体 code。
2. **看日志**：`docker logs ha-mcp 2>&1 | tail -50`。
3. **单独验证 HA**（排除本服务问题）：
   ```bash
   curl -s -H "Authorization: Bearer <你的令牌>" https://ha.hqgg.top:59826/api/states | head -c 300
   ```
   - 401 → 令牌问题；连接被拒 → 地址/网络问题。
4. **网络**：容器内访问 HA 注意
   - 同一 compose 网络用服务名（如 `http://homeassistant:8123`）；
   - 访问宿主机上的 HA 用 `http://host.docker.internal:8123`（Docker Desktop）；
   - Linux 下可加 `--add-host=host.docker.internal:host-gateway`。
5. **TLS**：自签名证书导致 `SSLError` 时设 `HA_VERIFY_SSL=false`（仅受信网络）。
6. **代理**：若宿主设置了 `HTTP_PROXY/NO_PROXY` 干扰直连，保持 `HA_TRUST_ENV=false`（默认）。
7. **按设备聚合不可用**：`list_devices(group_by="device")` 依赖 HA WebSocket `config/device_registry/list`。
   若返回 `registry_available:false`（令牌无权限、WS 被代理/防火墙拦截），会自动降级为按实体展示；
   可用 `HA_WS_URL` 指定地址，或设 `HA_WS_ENABLED=false` 明确关闭。
8. **控制回读不符（status=mismatch）**：说明 HA 接受了调用、状态也变了，但属性未达目标（如设备不支持该亮度/温度范围）。
   核对 `list_services` 的字段与设备能力，必要时用 `get_device_state` 复查。
9. **危险设备一直返回 `confirmation_required`**：这是防误操作设计，不是故障。把首次返回的 `confirm_token` 原样传入再次调用即可；若令牌过期，用新返回的 token。需要临时关闭可设 `DANGER_CONFIRM_ENABLED=false`（不建议）。

> 已知 HA 行为：对**不存在的实体**调用 service 也会返回 HTTP 200（响应为空）；新版 HA 对多数服务调用返回 **null**。
> 本服务在 `control_device` / `call_service` 后执行**状态+属性回读校验**：`response_kind`(states/empty/none)、`status`(confirmed/no_change/mismatch/accepted_pending)、`verified` 与 `verification`(checks/mismatches)，避免误报成功。

---

## 9. 离线测试（无需真实 HA）

提供模拟器与端到端脚本，可在没有 HA 的情况下验证全部工具。
测试口令由你自己指定（模拟器与本服务共用同一个值，**不要用真实 HA 令牌**）。

```bash
# 一键运行（推荐）
export TEST_TOKEN=任意本地测试口令
bash tests/run_local_test.sh
```

也可分三个终端手动运行：

```bash
# 终端 1：启动模拟 HA（令牌取自 FAKE_HA_TOKEN，缺省 devtoken）
export FAKE_HA_TOKEN=任意本地测试口令
python tests/fake_ha.py

# 终端 2：启动本服务并指向模拟 HA（HA_TOKEN 与模拟器一致）
export HA_URL=http://127.0.0.1:8123
export HA_TOKEN=任意本地测试口令
export HA_WS_URL=ws://127.0.0.1:8124/api/websocket   # 注册表 WS（fake_ha 默认 8124）
python server.py

# 终端 3：用真实 MCP 客户端跑端到端用例
python tests/e2e_mcp_client.py
```

`fake_ha.py` 特意复刻了「对不存在实体返回 200」的行为，并内置「延迟生效（*_delayed）」「只改状态不改属性（turn_on_ignore_brightness）」两种场景，用于验证回读确认与 mismatch 识别；并内置极简 WebSocket 注册表（默认 8124 端口）用于验证按设备聚合，未安装 websockets 时自动跳过。P3 还内置 `lock.front_door`（大门门锁）与 `lock.lock`/`lock.unlock` 服务，端到端脚本会验证「首次 confirmation_required → 带 token 执行成功 → 复用/错误 token 被拒 → 意图不匹配被拒」全流程。

---

## 10. 关于原需求中「(待补充)」项的处理

- **传输方式 / 接入方式**：默认 `streamable-http`，并同时支持 `sse` 与 `stdio`；第 6 节给出
  小智后端两条已验证接入路径（Path B 服务端 MCP 推荐；Path A MCP 接入点）。
  若你的小智版本/入口不同，按第 6 节对应调整即可。
- **设备较多时的返回规模**：`list_devices` 支持 `keyword`/`domain` 过滤 + `limit`/`offset` 分页
  （默认 50、上限 500），避免实体过多撑爆上下文；返回含 `has_more`/`next_offset` 便于续拉。
  另提供 `group_by="device"` 按物理设备聚合、`controllable_only=true` 只看可控设备，进一步压缩结果。
- 其余未提供的信息未做假设；如需与特定小智版本/回调协议对接，请补充说明。

---

## 11. 安全建议

- `HA_TOKEN` 仅环境变量注入，不写入镜像/代码；`.env` 不要提交到 Git（建议加入 `.gitignore`）。
- 容器以非 root 用户（uid 10001）运行。
- 建议仅在受信内网暴露 `8090`；如需公网访问，请前置反向代理并加鉴权/TLS。
- 危险设备二次确认 token 只用于**防误操作**，不是权限控制；真正的访问权限仍由 HA 长期令牌决定。
