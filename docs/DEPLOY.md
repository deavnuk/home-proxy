# 部署手册（模式 B）

本机实测环境：Ubuntu 24.04 / <HOSTNAME> / `<SERVER_IP>`

> 为什么是模式 B：把网关改成这台机器的话，机器一挂全家断网。
> 模式 B 只让「DNS」和「fake-ip 网段」走本机，国内流量完全不经过它。

## 一、整体链路

```
        设备（手机/电脑/电视）
              │
              ├─ DNS 查询 ──────────► <SERVER_IP>:53
              │                          │ nft 重定向到 mihomo:1053
              │                          ▼
              │                     gfwlist 命中？
              │                       ├─ 是 → 返回 198.18.x.x（假 IP）
              │                       └─ 否 → 返回真实 IP
              │
              └─ 访问 198.18.x.x ───► Merlin ──静态路由──► <SERVER_IP>
                                                              │ TPROXY
                                                              ▼
                                                           mihomo → 代理节点

        访问真实 IP ────────────► Merlin ──默认网关──► 外网（不碰本机）
```

## 二、服务端部署（本机）

### 1. 装成 systemd 服务

```sh
sudo ~/home-proxy/scripts/install-service.sh --home ~/home-proxy/config
```

做三件事：
- `mihomo` 以 root 运行（TPROXY 需要 `CAP_NET_ADMIN`）
- 开机自启 + 崩溃 3 秒后重启
- 开 unix socket 控制接口 `/run/mihomo/ctl.sock`（UI 容器靠它访问）

> 必须用 root：实测非 root 启动会报 `Start TProxy server error: operation not permitted`。

### 2. 配置透明代理规则

```sh
# 必须指定实际网段（脚本默认 192.168.1.0/24 只是占位）
sudo ~/home-proxy/scripts/deploy-mode-b.sh --lan-net 192.168.1.0/24
```

做四件事：
- sysctl：`ip_forward=1`、`rp_filter=0`、`route_localnet=1`
- 策略路由：`fwmark 0x1 → table 100`（TPROXY 收包必需）
- nftables：TPROXY 劫持 + **LAN `:53` 重定向到 mihomo `:1053`**
- 6 项自检

脚本会自动推导网卡/本机 IP/网段，也可显式指定：

```sh
sudo ~/home-proxy/scripts/deploy-mode-b.sh \
  --lan-if enp0s31f6 --lan-net <LAN_NET> --lan-addr <SERVER_IP>
```

### 3. 启动面板

```sh
cd ~/home-proxy/ui
docker compose -f docker-compose.<HOSTNAME>.yml --env-file .env up -d
```

访问 `http://<SERVER_IP>:8081`

## 三、Merlin 侧配置（一次性）

### 1) DHCP 服务器

```
高级设置 → 局域网 → DHCP 服务器

  默认网关           : 保持现状（<GATEWAY_IP>）
                       ★ 不要改成 178，否则本机一挂全家断网
  DNS Server 1       : <SERVER_IP>
  DNS Server 2       : <GATEWAY_IP>     ← 填 Merlin 自己
                       本机挂掉时 DNS 仍能解析，家里不断网
  通告路由器为 DNS   : No
```

### 2) 静态路由

```
高级设置 → 局域网 → 路由（Static Route）

  网络/主机 IP : 198.18.0.0
  网络掩码     : 255.255.0.0
  网关         : <SERVER_IP>
  接口         : LAN
```

这一步是模式 B 的核心：让 Merlin 把所有 `198.18.x.x` 的包交给本机。
客户端拿到的假 IP 全在这个段，所以只有被墙域名会被送过来。

## 四、验证

### 服务端

```sh
sudo nft list table inet transp | head -20     # nft 规则已加载
ip rule show | grep fwmark                     # 策略路由已配
ss -tln | grep -E ':7895|:1053'                # 两个端口在监听
curl -s --unix-socket /run/mihomo/ctl.sock \
  -H "Authorization: Bearer $(awk -F'"' '/^secret:/{print $2}' ~/home-proxy/config/config.yaml)" \
  http://localhost/version                     # 控制接口可访问
```

### 客户端（任意局域网设备）

```sh
ip route get 198.18.0.4      # 应显示 via <SERVER_IP>
nslookup www.google.com      # 应返回 198.18.x.x
nslookup www.baidu.com       # 应返回真实 IP
curl -so /dev/null -w '%{http_code}\n' https://www.google.com   # 200/204
```

### 面板「分流」页

输入 `www.google.com` → 应显示「走代理」
输入 `www.baidu.com` → 应显示「直接连接」

## 五、本机挂掉会怎样

按上面配置后：

| 场景 | 国内网站 | 被墙网站 |
|---|---|---|
| 正常运行 | ✓ | ✓ |
| **本机 mihomo 停了** | **✓（走 Merlin 直连）** | ✗ |
| **本机彻底关机** | **✓** | ✗（DNS2 兜底解析出真实 IP，直接连必失败） |

验证方法：

```sh
sudo systemctl stop mihomo
# 手机上测：微信/淘宝/百度 应正常，google 打不开
sudo systemctl start mihomo
```

如果国内网站也打不开，说明 DHCP 的**网关**被误改成了本机，改回 Merlin 的 IP。

## 六、日常运维

```sh
sudo systemctl status mihomo        # 状态
sudo systemctl restart mihomo       # 重启
journalctl -u mihomo -f             # 实时日志
journalctl -u mihomo -n 100         # 最近 100 行

# 改配置后热重载（面板「设置」页也能点）
curl -s --unix-socket /run/mihomo/ctl.sock \
  -X PUT "http://localhost/configs?force=true" \
  -H "Authorization: Bearer <secret>" \
  -H 'Content-Type: application/json' -d '{"path":"~/home-proxy/config/config.yaml"}'
```

## 七、常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 全屋 DNS 失败 | nft 规则没装（`prerouting_dns` 链缺失） | `sudo deploy-mode-b.sh` |
| google 解析出真实 IP | 客户端没用本机 DNS | 检查 DHCP 的 DNS1，清客户端 DNS 缓存 |
| google 解析出 198.18 但打不开 | Merlin 静态路由没生效 | 客户端 `ip route get 198.18.0.4` |
| 面板打不开 | 容器挂了 / 端口没发布 | `docker ps`、`docker logs home-proxy-ui` |
| 面板连不上 mihomo | socket 不存在（mihomo 未以 systemd 跑） | `sudo install-service.sh` |
| 国内网站也慢 | 网关被改成了本机 | 改回 Merlin |
| 重启后面板失效 | mihomo 是手工进程，socket 丢了 | `sudo install-service.sh` |

## 八、目录结构

本方案分两处（容器与项目分离，符合 `~/stacks/` 约定）：

```
~/stacks/home-proxy/            容器（与 caddy/jellyfin 等一致）
├── backend/                    代码（app/ static/ tests/ Dockerfile）
├── docker-compose.yml          接入 proxy 网络
├── .env                        密码与路径
└── backups/                    配置备份

/opt/mihomo/                    mihomo 运行目录（systemd 用）
├── mihomo                      二进制
├── config.yaml                 配置（含 secret）
├── providers/                  规则集
└── geoip.metadb

/run/mihomo/ctl.sock           控制接口（tmpfs，重启重建）

~/home-proxy/                  项目（文档、脚本、模板）
├── README.md
├── docs/  router/  systemd/  net/
└── scripts/                    部署与运维脚本
```

### 为什么这样分

| 位置 | 放什么 | 原因 |
|---|---|---|
| `~/stacks/home-proxy/` | 容器 | 与你其他服务的约定一致，Caddy 统一反代 |
| `/opt/mihomo/` | 内核配置与二进制 | systemd 服务需要稳定路径，不受家目录权限影响 |
| `~/home-proxy/` | 源码与文档 | 可随时 git pull 更新，不影响运行 |

### 与 Caddy 的集成

本容器必须加入 `proxy` 外部网络，否则 `proxy.lan` 反代不到：

```yaml
networks:
  - proxy       # Caddy 所在网络
  - default

networks:
  proxy:
    external: true
```

对应 Caddyfile：

```
proxy.lan {
    reverse_proxy home-proxy-ui:8080
}
```

⚠️ healthcheck 必须用容器内端口 `8080`，不能用 `${UI_PORT}`（那是宿主机映射端口）。

