# Home Proxy UI

mihomo 透明代理的 Web 管理面板。**只提供 UI**，代理与网络规则仍在宿主机（或 host 网络容器）里跑。

```
浏览器 ──► UI 容器 (FastAPI) ──► mihomo REST API ──► 代理节点
                 │
                 └──► nft / ip（只读探测透明代理是否生效）
```

## 功能

| 页面 | 内容 |
|---|---|
| **概览** | 实时上下行速率曲线（canvas）、累计流量、活动连接数、内存；**透明代理链路健康检查**（nft 规则 / 策略路由 / TPROXY 端口） |
| **代理** | 策略组切换、节点列表、单节点/整组测速（延迟分色） |
| **规则** | 6 个规则集（gfw/gfw-extra/cncidr/direct/private/reject）条目数与更新时间、一键刷新；分流规则表（含命中计数、可过滤） |
| **连接** | 活动连接表（域名/规则/链路/流量/时长）、单条或全部断开、实时刷新 |
| **设置** | 运行模式（rule/global/direct）、IPv6、TCP 并发、局域网、日志级别；端口一览 |
| **配置文件** | 在线编辑 `config.yaml`，保存自动备份 + 热重载，带 YAML 结构校验 |

设计上刻意做的取舍：

- **无前端框架**（纯 JS + CSS，约 1100 行），无 CDN 依赖，断网也能打开
- **无 WebSocket**：轮询 `/api/overview`，前端对累计流量做差分算速率。少一类鉴权/重连的坑，家用 1s 轮询足够
- **HTTP Basic 鉴权**：浏览器原生弹窗，零前端代码
- **后端收敛版本差异**：前端只吃 `/api/*` 的稳定形状，不碰 mihomo 原始 JSON

## 快速开始

### 方式一：只跑 UI（mihomo 已在宿主机 systemd 跑）

最轻量，推荐先用这个验证。

```bash
cd ui
cp .env.example .env
vi .env       # 至少改 MIHOMO_SECRET、UI_PASS、MIHOMO_HOME

# mihomo 的 external-controller 必须能被容器访问到
sudo sed -i 's|external-controller: 127.0.0.1:9090|external-controller: 0.0.0.0:9090|' \
  /opt/mihomo/config.yaml
sudo systemctl restart mihomo

docker compose -f docker-compose.ui-only.yml up -d
```

浏览器打开 `http://<server-ip>:8080`，用 `.env` 里的 `UI_USER`/`UI_PASS` 登录。

### 方式二：全容器化（mihomo + UI 都由 compose 管）

```bash
cd ui
cp .env.example .env && vi .env
docker compose up -d          # mihomo 用 network_mode: host
```

⚠️ 这个方式下 **nft 规则仍要装在宿主机**（容器内的 nft 只影响自己 netns，除非 host 模式；
而 host 模式下容器改 nft 就等于改宿主机，容易误伤，所以交由宿主 systemd 管理）。
先按主 README 装好 `net/nftables-transp.conf`，再起 compose。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MIHOMO_API` | `http://host.docker.internal:9090` | mihomo 控制接口 |
| `MIHOMO_SECRET` | **必填** | 必须等于 `config.yaml` 的 `secret` |
| `MIHOMO_CONFIG` | `${MIHOMO_HOME}/config.yaml` | 配置文件路径，**必须与 mihomo 看到的绝对路径一致** |
| `MIHOMO_HOME` | `/opt/mihomo` | mihomo 的 `-d` 目录，会**同路径**挂进容器 |
| `MIHOMO_BACKUP_DIR` | `/backup` | 改配置时的备份目录 |
| `UI_USER` / `UI_PASS` | — | 都设置才启用 Basic 鉴权；留空则**完全不鉴权** |
| `UI_READ_ONLY` | `0` | `1` = 禁止改配置/切代理/断连接 |
| `UI_PORT` | `8080` | 宿主机端口 |
| `PUID` / `PGID` | `1000` | 容器运行身份，必须与 `config.yaml` 属主一致 |
| `NFT_TABLE` | `inet transp` | 透明代理状态面板读的 nft 表 |

## 三个必须知道的坑

这三点是实际踩出来的，部署时最容易卡住：

### 1. `MIHOMO_CONFIG` 必须与 mihomo 看到的路径字符串完全一致

mihomo 只允许热重载**自己 home 目录（`-d`）下**的文件，且按**绝对路径**校验：

```
mihomo 返回 400: path is not subpath of home directory or SAFE_PATHS:
  /etc/mihomo/config.yaml   ← 你传给它的
  allowed paths: [/opt/mihomo]   ← 它认的
```

所以 compose 里是**同路径挂载**（宿主机 `/opt/mihomo` → 容器 `/opt/mihomo`），
**不能**映射成 `/etc/mihomo`。容器内路径 = 宿主机路径，校验才会通过。

### 2. `user:` 必须与配置文件属主一致

否则 UI 里保存配置会 `Permission denied`（容器是 uid 10001，文件是 1000）：

```bash
stat -c '%u:%g' /opt/mihomo/config.yaml   # 把这个值填进 PUID/PGID
```

### 3. 备份目录要可写

只挂单个文件时，同目录往往属 root 不可写。UI 会依次尝试
`MIHOMO_BACKUP_DIR` → 配置文件同目录 → 系统临时目录，取第一个可写的。
建议显式设置 `MIHOMO_BACKUP_DIR` 并挂一个可写卷。

## 透明代理状态面板

概览页的「透明代理链路」会读宿主机 nft 状态，回答一个关键问题：**分流到底生效没有？**

| 提示 | 含义 | 处理 |
|---|---|---|
| `宿主机上没有加载 nft 规则表` | nft 未装或表名不对 | 宿主执行 `nft -f net/nftables-transp.conf` |
| `策略路由缺失` | TPROXY 收不到包 | `ip rule add fwmark 0x1 lookup 100` + `ip route add local 0.0.0.0/0 dev lo table 100` |
| `TPROXY :7895` 徽章 | 从规则里解析到的 TPROXY 端口 | 与 `config.yaml` 的 `tproxy-port` 对比 |
| `未检测`（容器内无 nft） | UI 容器没装 nft 或没 `NET_ADMIN` | 属正常降级；需要就加 `cap_add: [NET_ADMIN]` |

> 注意：UI 容器默认**看不到宿主机的 nft 规则**（不同 netns）。
> 该面板在「只跑 UI」模式下会显示「未检测」，这是预期行为，不影响其它功能。
> 想看真实状态就在宿主机执行 `nft list table inet transp`。

## 开发

```bash
cd ui/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
MIHOMO_API=http://127.0.0.1:9090 MIHOMO_SECRET=xxx \
  uvicorn app.main:app --reload --port 8080
```

前端无构建步骤：直接编辑 `backend/static/{index.html,app.css,app.js}`，刷新即可。

## API

自带 OpenAPI 文档：`/api/docs`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ping` | 连通性/鉴权自检 |
| GET | `/api/overview` | 仪表盘聚合数据 |
| GET | `/api/proxies` | 策略组 + 节点 |
| PUT | `/api/proxies/{group}` | 切换策略组 |
| GET | `/api/delay/node/{name}` | 单节点测速 |
| GET | `/api/delay/group/{group}` | 整组测速 |
| GET | `/api/rules` | 分流规则（含命中数） |
| GET | `/api/rule-providers` | 规则集列表 |
| PUT | `/api/rule-providers/{name}` | 刷新规则集 |
| GET/DELETE | `/api/connections` | 连接列表 / 全部断开 |
| DELETE | `/api/connections/{id}` | 断开单条 |
| GET/PATCH | `/api/settings` | 运行时配置 |
| GET/PUT | `/api/config/file` | 读 / 存配置文件（含备份+重载） |
| POST | `/api/config/reload` | 热重载 |
| GET | `/api/transparent` | 透明代理健康检查 |

> `/api/delay/node/...` 与 `/api/delay/group/...` 分开是必要的：
> 若写成 `/proxies/{name}/delay` 和 `/proxies/{group}/delay`，FastAPI 会把它们视为
> 同一模式，后注册的会遮蔽先注册的（实测踩过，节点测速变成 404）。

## 已知限制

- **不支持 WebSocket 实时推送**：概览 1s 轮询、连接页 1s 轮询（切到后台会暂停）
- **配置编辑器无语法高亮**：纯 textarea（只做了 Tab 缩进），避免引入 CodeMirror 的体积
- **单实例**：不做多用户/多租户，Basic 鉴权只有一个账号
- **透明代理状态**只读探测，不改规则（改规则请在宿主机操作）
