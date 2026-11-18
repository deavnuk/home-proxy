# 技术方案

> 对应 [PRD](PRD.md)。每节标注实现的需求 ID。

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│  浏览器                                                       │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTP Basic
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  UI 容器 (bridge + ports 8081)                                │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ FastAPI                                                 │ │
│  │  ├─ /api/*        REST（业务整形层，隔离 mihomo 版本差异）│ │
│  │  ├─ /ws/logs      WebSocket 日志流                       │ │
│  │  └─ /static/*     无构建前端                             │ │
│  └────────────────────────────────────────────────────────┘ │
└────────────────────────┬────────────────────────────────────┘
                         │ unix:///run/mihomo/ctl.sock
                         │ （不用 TCP：宿主 ufw 默认 DROP，
                         │   容器→宿主任何端口都被拦）
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  宿主机 (systemd)                                             │
│  ├─ mihomo.service       root，TPROXY + DNS + 控制接口        │
│  ├─ mihomo-route.service 策略路由 fwmark 0x1 -> table 100     │
│  └─ mihomo-nftables      TPROXY / DNS 劫持 / NAT 规则         │
│                                                              │
│  配置文件 /opt/mihomo/config.yaml（同路径挂进容器）             │
└─────────────────────────────────────────────────────────────┘
```

### 1.1 为什么用 unix socket（约束 1、2）

实测数据：

| 路径 | 结果 |
|---|---|
| 容器 → `host.docker.internal:9090` | ❌ 000（ufw DROP）|
| 容器 → 宿主 LAN IP:9090 | ❌ 000 |
| 容器 → docker 网桥网关:9090 | ❌ 000 |
| 容器 → 同网络其他容器 | ✅ 200 |
| 容器 → unix socket | ✅ 200 |

结论：宿主 `ufw` 的 `DEFAULT_INPUT_POLICY=DROP` 拦的是「容器→宿主」方向，与端口无关。唯一可靠通道是 unix socket。

mihomo 启动参数：
```
mihomo -d /opt/mihomo -f /opt/mihomo/config.yaml -ext-ctl-unix /run/mihomo/ctl.sock
```

UI 侧（`mihomo.py`）：
```python
if settings.is_unix_socket:
    url = f"http://localhost{path}"
    transport = httpx.AsyncHTTPTransport(uds=settings.unix_socket_path)
```

### 1.2 配置写入的关键约束

mihomo 只允许热重载**自己 `-d` 目录下**的文件，且按**绝对路径字符串**校验：

```
400: path is not subpath of home directory or SAFE_PATHS:
  /etc/mihomo/config.yaml    ← 传入的
  allowed paths: [/opt/mihomo]
```

所以容器必须**同路径挂载**：宿主 `/opt/mihomo` → 容器 `/opt/mihomo`，不能映射成 `/etc/mihomo`。

---

## 2. 订阅管理（F-1.x）

### 2.1 为什么不能只靠 mihomo 的 proxy-providers

`proxy-providers` 只能读 `config.yaml` 里静态写死的 URL。要支持 UI 增删改，必须由 UI 层**改写配置文件**。

### 2.2 实现方案：YAML 结构化读写

新增 `config_editor.py`，只处理 `proxy-providers` 段，其余内容原样保留：

```python
def upsert_subscription(text, name, url, options) -> str
def remove_subscription(text, name) -> str
def list_subscriptions(text) -> list[dict]
```

**关键设计：不用 `yaml.dump` 重写整个文件**（会丢注释、打乱顺序）。改用：

- 读：`yaml.safe_load` 解析出 `proxy-providers` 段
- 写：定位该段的文本范围，**只替换这一段**，其余逐字节保留

```python
def _find_block(text: str, key: str) -> tuple[int, int]:
    """返回 `key:` 段落的 [start, end) 字符偏移，end 到下一个顶层键为止。"""
```

这样用户的注释、顺序、缩进全都保住。

### 2.3 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/subscriptions` | 列表（含状态）|
| POST | `/api/subscriptions` | 新增 {name, url, interval, enabled} |
| PUT | `/api/subscriptions/{name}` | 修改 |
| DELETE | `/api/subscriptions/{name}` | 删除 |
| PUT | `/api/subscriptions/{name}/refresh` | 立即拉取 |

所有写操作走统一流程：

```
校验参数 → 读现有 config.yaml → 生成新文本
  → YAML 语法校验 → 备份旧文件 → 原子写入
  → PUT /configs 热重载 → 失败则回滚
```

### 2.4 状态合并（F-1.6）

mihomo 的 `/providers/proxies` 返回订阅状态，字段：

```json
{
  "sub-main": {
    "vehicleType": "HTTP",
    "proxies": [...],
    "updatedAt": "2026-11-17T16:12:16Z",
    "subscriptionInfo": {"Upload": 0, "Download": 0, "Total": 0, "Expire": 0}
  }
}
```

UI 把「配置文件里的订阅定义」与「mihomo 的运行时状态」按 name 合并：

```python
def merge(cfg_subs, runtime_subs):
    by_name = {s["name"]: s for s in runtime_subs}
    for s in cfg_subs:
        rt = by_name.get(s["name"], {})
        s["count"] = rt.get("count", 0)
        s["alive"] = rt.get("alive", 0)
        s["updated_at"] = rt.get("updated_at")
        s["traffic"] = rt.get("subscription_info", {})
```

### 2.5 订阅失效兜底（约束 3）

实测：订阅 URL 404 时 mihomo 日志报错但**继续运行**，其他功能正常。UI 层额外做：

- 拉取失败 → 保留旧 `path` 文件，不清空
- UI 显示红色状态 + 具体错误
- 不因为单个订阅失败阻塞其他订阅

---

## 3. 配置持久化（F-2.x）

### 3.1 双轨制

| 场景 | 方式 | 持久化 |
|---|---|---|
| 临时调试（切模式、改日志级别）| `PATCH /configs` | ❌ 内存 |
| 长期修改 | 写 config.yaml + 热重载 | ✅ 磁盘 |

PRD 要求 F-2.1 落盘，所以 UI 的「设置」页改为**写文件**，而非 PATCH。

### 3.2 保存流程（F-2.2、F-2.3）

```
新文本
  ├─ yaml.safe_load 语法校验
  ├─ 必需字段检查（proxies / proxy-groups / rules）
  ├─ 写出到 <path>.tmp
  ├─ 备份现有 -> <backup_dir>/config.yaml.bak.<ts>
  ├─ mv .tmp -> path（原子替换）
  └─ PUT /configs 热重载
        └─ 失败 → 从备份恢复 + 重新热重载 + 返回错误
```

### 3.3 备份管理（F-2.4）

```
<backup_dir>/config.yaml.bak.<YYYYmmdd-HHMMSS>
```
- 保留最近 20 个，超出自动清理
- `GET /api/backups` 列表，`POST /api/backups/{name}/restore` 恢复

⚠️ 备份目录可写性：只挂单文件时同目录常属 root。降级链：
`MIHOMO_BACKUP_DIR` → 配置同目录 → `/tmp`

---

## 4. 开机自启（F-3.x）

### 4.1 systemd unit（关键：带 socket 参数）

```ini
[Unit]
Description=mihomo transparent proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/opt/mihomo/mihomo -d /opt/mihomo -f /opt/mihomo/config.yaml \
          -ext-ctl-unix /run/mihomo/ctl.sock
Restart=always
RestartSec=3
RuntimeDirectory=mihomo
RuntimeDirectoryMode=0750
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

`RuntimeDirectory=/run/mihomo` 确保重启后目录存在、权限正确。

### 4.2 容器侧重连

UI 不需要特殊处理：httpx 每次请求新建 client，socket 恢复后自动可用。

### 4.3 状态展示（F-3.3）

```
GET /api/health/detail
→ { uptime, restarts, socket_ok, ... }
```
uptime 由 `systemctl show -p ActiveEnterTimestamp` 得到。

---

## 5. 日志查看（F-4.x）

### 5.1 WebSocket 桥接

mihomo 的 `/logs?level=info` 是**流式端点**，普通 GET 会永久挂起。必须用 WS。

```
浏览器 --ws--> UI /ws/logs --ws--> mihomo /logs
```

服务端用 `websockets` 库做双向 pump：

```python
async with websockets.connect(ws_url, additional_headers={"Authorization": ...}) as up:
    async def up_to_client():
        async for msg in up:
            await ws.send_text(msg)
    async def client_to_up():
        while True:
            await up.send(await ws.receive_text())
```

**踩坑记录**：之前 `mihomo.get("/memory")` 导致 overview 永久挂起，因为 `/memory` `/traffic` `/logs` 都是流式的。已在 `mihomo.py` 加黑名单：

```python
STREAMING_ENDPOINTS = frozenset({"/traffic", "/memory", "/logs"})
```

### 5.2 前端

- 环形缓冲 500 条
- 级别过滤 + 关键词搜索
- 自动滚动（用户上滚时暂停）

---

## 6. 一键体检（F-6.3、F-6.4）

```python
async def health_check() -> list[CheckResult]:
    return [
        check_ip_forward(),        # net.ipv4.ip_forward == 1
        check_mihomo(),            # API 可达 + 进程存活
        check_listeners(),         # 7895/1053 是否监听
        check_nft(),               # inet transp 表存在 + counter 命中
        check_policy_route(),      # ip rule fwmark 0x1
        check_nodes(),             # 至少一个节点 alive
        check_rules_freshness(),   # 规则集 updatedAt 距今 < 3 天
    ]
```

每个结果包含 `fix` 字段（可复制的命令）：

```json
{
  "name": "nft 规则",
  "ok": false,
  "detail": "宿主机未加载 inet transp 表",
  "fix": "sudo nft -f /opt/home-proxy/net/nftables-transp.conf"
}
```

---

## 7. 前端架构

### 7.1 无构建

- 原生 ES2020，`<script src="/static/app.js">`
- CSS 变量做主题
- 不引入任何 CDN（内网环境也要能打开）

### 7.2 状态管理

```js
const state = { view, readOnly, traffic, hist, rulesCache, ... }
```
单向数据流：`fetch → state → render(view)`。

### 7.3 轮询策略

| 页面 | 间隔 | 说明 |
|---|---|---|
| 概览 | 1s | 差分算速率 |
| 连接 | 1s | 活动连接变化快 |
| 其他 | 手动 | 避免无谓请求 |

`document.hidden` 时暂停。

### 7.4 主题（F-7.1）

```css
:root { --bg-0: #070b13; ... }         /* 深色默认 */
[data-theme="light"] { --bg-0: #f6f8fc; ... }
```
切换写 `localStorage`，首屏在 `<head>` 内联脚本设置，避免闪烁。

---

## 8. 错误处理规范（F-7.3）

所有 API 错误统一形状：

```json
{ "error": "简短中文描述", "detail": "技术细节/建议" }
```

前端 `toast` 显示 `error`，`banner` 显示 `error + detail`。

常见错误的中文化在 `mihomo.py::_translate()`：

| 上游情况 | 用户看到 |
|---|---|
| ConnectError | 连不上 mihomo 控制接口（含目标地址 + 排查建议）|
| Timeout | mihomo 接口超时（含秒数）|
| 401 | mihomo 鉴权失败，UI 的 MIHOMO_SECRET 与 config.yaml 不一致 |

---

## 9. 安全（F-8.x）

| 项 | 实现 |
|---|---|
| 鉴权 | HTTP Basic；`UI_USER`/`UI_PASS` 都设置才启用 |
| 只读 | `UI_READ_ONLY=1` → 所有写接口 403 |
| 脱敏 | 订阅 URL 中的 token 参数在列表接口里替换为 `***` |
| 时序安全 | `secrets.compare_digest` 比较密码 |
| 不提权 | 容器 `USER 10001`，仅 `NET_ADMIN`（读 nft）|

---

## 10. 实施顺序

| 阶段 | 内容 | 依赖 |
|---|---|---|
| S1 | 配置读写层 `config_editor.py` + 备份 | — |
| S2 | 订阅 CRUD API | S1 |
| S3 | 设置落盘 + 回滚 | S1 |
| S4 | systemd unit（含 socket）| — |
| S5 | 日志 WS 桥接 | — |
| S6 | 一键体检 | S4 |
| S7 | 前端：订阅页 / 日志页 / 体检卡 / 主题 | S2,S5,S6 |
| S8 | 文档 + E2E 测试 | 全部 |

---

## 11. 测试策略

| 层级 | 方式 |
|---|---|
| 配置读写 | 单元测试：注入含注释的 YAML，验证增删改后注释保留 |
| API | 用真实 mihomo + 本地桩（`scripts/test/*.py`），curl 全量 |
| 故障切换 | 造好/坏节点，杀后端观察 fallback 自动切换 |
| 端到端 | `scripts/e2e-test.sh`（netns 模拟 LAN）|
| 部署 | Docker 构建 + compose 起停 + 重启后自愈 |
