# 家庭透明代理：Merlin 路由器 + Ubuntu Server（按 gfwlist 分流）

一台 Ubuntu Server 做**旁路网关**，家里所有设备零配置（不改 App、不装客户端），
按 **gfwlist** 决定走不走你的 server proxy。配一个 Web 面板，日常运维不用登服务器。

## 文档导航

| 文档 | 内容 | 面向 |
|---|---|---|
| **[docs/DEPLOY.md](docs/DEPLOY.md)** | **★ 部署指南：从零装一台、验证、升级、卸载** | **部署者先看这个** |
| [router/merlin-setup.md](router/merlin-setup.md) | **★ 本家定稿方案（模式 B）**：Merlin 两处配置、断网演练、故障速查 | 部署者 |
| [README.md](README.md) | 本文：方案总览与原理 | 所有人 |
| [docs/TECH_DESIGN.md](docs/TECH_DESIGN.md) | 架构、关键约束、实现方案、踩坑记录 | 开发/排障 |
| [docs/PRD.md](docs/PRD.md) | 产品需求、用户旅程、功能清单与验收标准 | 产品/决策 |
| [docs/UX_DESIGN.md](docs/UX_DESIGN.md) | 交互规范、组件规则、a11y、文案规范 | 前端/设计 |
| [ui/README.md](ui/README.md) | 面板部署、环境变量、已知限制 | 部署者 |

```
        ┌─────────────┐
        │   Merlin    │  网关保持 Merlin（不改）
        │ <GATEWAY_IP> │  DHCP 下发：DNS1=server，DNS2=Merlin（备用）
        └──────┬──────┘
               │ LAN <LAN_NET>
   ┌───────────┴───────────────┐
   │                           │
手机/电视/PC            ┌──────┴──────────┐
   │                    │ Ubuntu Server   │ <SERVER_IP>
   │  DNS 指向 server    │  mihomo + nft   │
   │  国内流量 ──────────┤                 │
   │  （走 Merlin 直出）  └──────┬──────────┘
   │                            │ 仅 fake-ip 段（被墙域名）
   └── 198.18.x.x ──► Merlin ──►│
                                ▼
                        ┌───────────────┐
                        │  代理节点      │
                        └───────────────┘
```

**关键：网关不指向 server。** 国内流量完全不经过它；server 挂掉时家里照常上网。

## 核心设计：fake-ip 白名单

不用「全量透明代理」，而是让 DNS 结果本身携带分流信息：

| DNS 查询 | 返回 | 内核行为 |
|---|---|---|
| 命中 gfwlist | `198.18.x.x`（假 IP） | nft 命中 fake-ip 段 → TPROXY 给 mihomo → 走 server proxy |
| 未命中 | 真实 IP | 内核直接转发，**不进用户态** → 满速、零额外延迟 |

好处：
- 分流依据是**域名**（gfwlist），不是 IP，准确
- 直连流量完全不经 mihomo，4K/游戏/下载不受影响
- mihomo 通过 fake-ip 反查表还原出域名，代理端拿到的仍是域名（不破坏 CDN/SNI）
- 硬编码 DNS（8.8.8.8 / DoH）的设备也被 nft 强制劫持，无处可逃

## 目录

```
home-proxy/
├── bin/mihomo                     # mihomo 二进制 (v1.19.34, linux-amd64)
├── config/
│   ├── config.yaml                # ★ 主配置（改 server proxy 地址在这里）
│   ├── geoip.metadb               # GeoIP 库（离线，避免启动时下载）
│   └── providers/                 # 规则集（可离线预置，mihomo 会自动更新）
│       ├── gfw.yaml               # gfwlist（Loyalsoldier 版，4089 条）
│       ├── gfwlist-extra.yaml     # 官方 gfwlist 转换产物（4113 条）
│       ├── cncidr.yaml            # 国内 CIDR（直连）
│       ├── direct.yaml            # 直连白名单
│       ├── private.yaml           # 内网域名
│       └── reject.yaml            # 广告/追踪
├── net/nftables-transp.conf       # ★ nftables：TPROXY + DNS 劫持 + NAT
├── scripts/
│   ├── install-service.sh         # ★ 装 systemd 服务（开机自启）
│   ├── deploy-mode-b.sh           # ★ 部署网络规则（sysctl+路由+nft）
│   ├── set-mode.sh                # 配置指引与自检
│   ├── update-rules.sh            # 每日刷新 gfwlist（systemd timer 调用）
│   ├── gfwlist2clash.py           # 官方 gfwlist(base64) -> mihomo rule-provider
│   ├── e2e-test.sh                # ★ 端到端验证（netns 模拟真实家庭网络）
│   └── test/                      # 测试桩（SOCKS5 代理桩 + DNS 桩）
├── systemd/
│   ├── mihomo-update.service      # 规则更新
│   └── mihomo-update.timer        # 每日
└── router/merlin-setup.md         # ★ Merlin 侧配置步骤
```

## 部署

### 1. 改两处配置

`config/config.yaml`：

```yaml
proxies:
  - name: "node-manual"
    type: socks5
    server: <PROXY_HOST>     # ← 你的代理节点
    port: 1081
    udp: true
secret: "change-me-please"      # ← 改成随机字符串，面板登录用
```

订阅也可以，面板里加更方便（见下文「Web 面板」）。

> mihomo 的 DNS 监听 `1053` 而非 `53`：53 是特权端口（非 root 绑不了），
> 且被 systemd-resolved 占用。nft 会把 LAN 的 `:53` 重定向到 `1053`。

### 2. 装到 Ubuntu Server

```sh
# ① 装成 systemd 服务（开机自启 + 崩溃自愈 + unix socket 控制接口）
sudo ./scripts/install-service.sh

# ② 部署网络规则（sysctl + 策略路由 + nftables TPROXY/DNS劫持）
sudo ./scripts/deploy-mode-b.sh
```

`deploy-mode-b.sh` 会自动推导网卡/本机 IP/网段，也可显式指定：

```sh
sudo ./scripts/deploy-mode-b.sh --lan-if eth0 --lan-net <LAN_NET> --lan-addr <SERVER_IP>
```

### 3. 配 Merlin

见 [`router/merlin-setup.md`](router/merlin-setup.md)。核心两件事：

- DHCP：**网关保持 Merlin 不变**；DNS1 填 server，DNS2 填 Merlin（备用）
- 静态路由：`198.18.0.0/16 → server`

⚠️ 不要把网关改成 server。那样它一挂全家断网。

### 4. 验证

```sh
# 端到端（在 server 上跑，会自己建 netns，不影响现有网络）
sudo ./scripts/e2e-test.sh
```

实测结果（沙箱环境）：

```
✓ LAN 二层连通                     ✓ nft 规则加载
✓ TPROXY 监听 :7895                ✓ 策略路由 (fwmark 0x1 -> table 100)
✓ google/youtube/twitter -> 198.18.x（假 IP）
✓ baidu/bilibili/qq -> 真实 IP（直连）
✓ TPROXY 链路通（LAN -> 代理桩 -> HTTP 200）
✓ fake-ip 已还原为域名 twitter.com
✓ gfwlist 域名 -> PROXY
✓ 非 gfwlist 域名 -> DIRECT
✓ baidu 未经过代理（分流正确）
结果: 20 通过 / 0 失败
```

线上检查：

```sh
journalctl -u mihomo -f | grep -E 'google|baidu'
# [TCP] ... --> www.google.com:443 match RuleSet(gfw) using PROXY[server-proxy]
# [TCP] ... --> www.baidu.com:443  match RuleSet(direct) using DIRECT

nft list chain inet transp prerouting_tproxy    # 看命中计数
dig @192.168.2.2 www.google.com                 # 应返回 198.18.x.x
dig @192.168.2.2 www.baidu.com                  # 应返回真实 IP
```

## 分流规则

`config/config.yaml` 的 `rules`（自上而下首个匹配生效）：

```yaml
- RULE-SET,private,DIRECT            # 内网域名
- RULE-SET,reject,REJECT             # 广告/追踪
- RULE-SET,direct,DIRECT             # 银行、国内服务
- RULE-SET,cncidr,DIRECT,no-resolve  # 国内 IP 段
- RULE-SET,gfw,PROXY                 # ★ gfwlist
- RULE-SET,gfw-extra,PROXY           # ★ 官方 gfwlist
- MATCH,DIRECT                       # 兜底直连
```

想全走代理：最后一行改 `MATCH,PROXY`。
想加自己的域名：在 `config/providers/` 里建个 yaml，加个 `rule-providers` 条目和一条 `RULE-SET`。

## 端口

| 端口 | 用途 |
|---|---|
| 7895 | TPROXY（LAN 透明代理，TCP+UDP） |
| 7892 | REDIRECT（本机进程 TCP 透明代理） |
| 7890 | mixed HTTP/SOCKS5（调试/临时设备） |
| 7891 | SOCKS5（含 UDP） |
| 1053 | DNS（nft 把 LAN 的 :53 重定向到这里） |
| 9090 | 控制面板 API（仅 127.0.0.1，需 secret） |

## 常见坑（都已在配置里处理）

| 坑 | 处理 |
|---|---|
| `tproxy` 语法要求协议匹配 | `meta l4proto { tcp, udp } tproxy ...` |
| `tproxy-port` 与 listeners 重复 → bind 冲突 | 只用 `tproxy-port` |
| `global-client-fingerprint` 已废弃 | 写在 proxy 上的 `client-fingerprint` |
| GEOIP 规则触发 MMDB 下载失败 | 改用 cncidr 规则集，保留离线 `geoip.metadb` |
| fake-ip 全量劫持导致国内站变慢 | `fake-ip-filter-mode: whitelist` + `rule-set:gfw` |
| 重启后老连接找不到域名 | `store-fake-ip: true` |
| 本机 QUIC 无法透明代理 | output 链对 `udp dport 443` 直接 reject，促其回退 TCP |
| 规则集从 GitHub 拉不下来 | `rule-providers` 全部带 `proxy: PROXY` |
| url-test 健康检查抢占 fake-ip | 已从 PROXY 组默认成员移除（单节点无需 urltest） |

更多排障见 [`router/merlin-setup.md#常见故障`](router/merlin-setup.md)。

## 运维

```sh
# 改完 config.yaml 热重载
curl -X PUT -H "Authorization: Bearer $(awk -F'"' '/^secret:/{print $2}' /etc/mihomo/config.yaml)" \
     http://127.0.0.1:9090/configs?force=true -d '{"path":"/etc/mihomo/config.yaml"}'

# 手动刷 gfwlist
sudo /usr/local/bin/mihomo-update-rules

# 看规则命中
nft -a list table inet transp
```

---

## Web 面板（Docker 部署）

```sh
# 1) 装 mihomo 为 systemd 服务（开机自启 + 崩溃自愈 + unix socket）
sudo ./scripts/install-service.sh --home /opt/mihomo

# 2) 起面板
cd ui
cp .env.example .env && vi .env          # 填 MIHOMO_SECRET / UI_PASS / MIHOMO_HOME
docker compose -f docker-compose.<HOSTNAME>.yml --env-file .env up -d

# 3) 打开 http://<server-ip>:8081
```

### 面板能做什么

| 页面 | 功能 |
|---|---|
| **概览** | 实时速率曲线、累计流量、**一键体检**（每项红灯都给出可复制的修复命令）|
| **代理** | 策略组切换、节点测速、延迟排序 |
| **订阅** | **在线增删改订阅**（自动写 config.yaml + 热重载，无需 SSH）|
| **规则** | 6 个规则集状态、手动刷新、分流规则与命中计数 |
| **连接** | 活动连接、单条/全部断开 |
| **日志** | 实时日志流（WebSocket）、级别过滤、关键词搜索 |
| **设置** | 运行模式、IPv6、日志级别 |
| **配置** | 在线编辑 config.yaml（语法校验 + 备份 + 回滚）|

### 订阅怎么填

**方式一：面板**（推荐）
「订阅」页 → 「+ 添加订阅」→ 填名称和链接 → 保存。自动生效。

**方式二：配置文件**
```yaml
proxy-providers:
  sub-main:
    type: http
    url: "https://你的订阅链接"
    interval: 21600          # 6 小时自动更新
    proxy: PROXY             # 用代理拉订阅
    health-check:
      enable: true
      interval: 300
```

### 节点容灾

```yaml
proxy-groups:
  - name: PROXY
    type: select
    proxies: [fallback, auto, node-manual, DIRECT]
    use: [sub-main]

  - name: fallback        # 按顺序取第一个可用的（主节点挂了自动切）
    type: fallback
    interval: 60
    lazy: false           # 未被选中也检查

  - name: auto            # 选延迟最低的
    type: url-test
    tolerance: 50
```

**实测验证**：杀掉主节点后端后，健康检查自动标记 `alive=False`，`fallback` 在下一轮检查时切到备用节点，无需人工干预。

> ⚠️ 故障发现延迟 ≈ `health-check.interval`。想让切换更快就调小它（如 30s），代价是更多探测流量。

### 部署必踩的三个坑

1. **配置路径必须一致** —— mihomo 只允许热重载自己 `-d` 目录下的文件，且按绝对路径字符串校验。容器必须**同路径挂载**（宿主 `/opt/mihomo` → 容器 `/opt/mihomo`），不能映射成 `/etc/mihomo`。
2. **文件属主必须匹配** —— 容器用户与 `config.yaml` 属主不一致会写不进去。`stat -c '%u:%g' config.yaml` 填进 `PUID/PGID`。
3. **控制接口必须用 unix socket** —— 宿主 `ufw` 默认 `DROP`，容器访问宿主机**任何** TCP 端口都会被拦（实测 `host.docker.internal:9090` = 000）。只有 `-ext-ctl-unix` 可靠。

详见 [`ui/README.md`](ui/README.md)。
