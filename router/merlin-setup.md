# Merlin 路由器配置

## 一、最终拓扑

```
                公网
                 │
          ┌──────┴──────┐
          │   Merlin    │ <GATEWAY_IP>
          └──────┬──────┘
                 │ LAN <LAN_NET>
        ┌────────┴────────┐
        │                 │
    家里设备            <HOSTNAME> (.178)
   DHCP 下发：          Ubuntu Server
     网关 = Merlin        mihomo + nft
     DNS1 = .178
     DNS2 = .254  ←备用
                          │
                          └──► 代理节点（公网）
```

**关键点：网关保持 Merlin，只把 DNS 指向 <HOSTNAME>。**

- 国内流量：`设备 → Merlin → 外网`（完全不经过 <HOSTNAME>）
- 被墙流量：`设备 → Merlin → <HOSTNAME> → 代理节点`（<HOSTNAME> 只处理 fake-ip 段）

## 二、为什么不把网关也指向 <HOSTNAME>

如果把 DHCP 网关也改成 <HOSTNAME>，全屋所有流量都要经过它。**它一挂，家里完全断网**
（连百度都打不开）。

本方案刻意保持网关为 Merlin，只让 DNS 和 fake-ip 段走 <HOSTNAME>：

| 场景 | 国内网站 | 被墙网站 |
|---|---|---|
| 正常 | ✓ | ✓ |
| <HOSTNAME> 挂了 | ✓ | ✗（DNS2 兜底解析，但代理在 <HOSTNAME> 上） |

## 三、Merlin 侧配置（一次性，共两处）

### 1) DHCP 服务器

`高级设置 → 局域网 → DHCP 服务器`

| 项目 | 值 | 说明 |
|---|---|---|
| 启用 DHCP | 是 | |
| 默认网关 | **保持现状不动**（如 `<GATEWAY_IP>`） | ⚠️ 千万别改成 <HOSTNAME> |
| DNS Server 1 | `<SERVER_IP>` | <HOSTNAME>，负责分流判断 |
| DNS Server 2 | `<GATEWAY_IP>` | Merlin 自己，**<HOSTNAME> 挂了时的备用 DNS** |
| 通告路由器为 DNS | No | 否则客户端可能只问路由器 |

点击「应用本页设置」。

### 2) 静态路由（把 fake-ip 段交给 <HOSTNAME>）

`高级设置 → 局域网 → 路由（Static Route）`

| 网络/主机 IP | 网络掩码 | 网关 | 接口 |
|---|---|---|---|
| `198.18.0.0` | `255.255.0.0` | `<SERVER_IP>` | `LAN` |

点击「应用本页设置」。

> 这条路由是模式 B 的核心：客户端拿到的 fake-ip（`198.18.x.x`）会经 Merlin 转发给 <HOSTNAME>，其余真实 IP 走默认网关直出。

## 四、<HOSTNAME> 侧配置（一条命令）

```sh
sudo ~/home-proxy/scripts/deploy-mode-b.sh
```

脚本会做：
- sysctl（`ip_forward`、`rp_filter=0` 等）
- 策略路由（TPROXY 必需的 `fwmark 0x1 → table 100`）
- nftables（TPROXY 劫持 + **LAN :53 重定向到 mihomo 的 1053**）
- 六项自检（含「向本机 53 查 DNS 是否有响应」）

## 五、验证

### 在任意局域网设备上

```sh
# 1) DNS 分流是否正确
nslookup www.google.com <SERVER_IP>    # 应返回 198.18.x.x
nslookup www.baidu.com  <SERVER_IP>    # 应返回真实 IP

# 2) fake-ip 是否路由到 <HOSTNAME>
ip route get 198.18.0.4                   # 应显示 via <SERVER_IP>
# Windows: route print | findstr 198.18
# macOS:   route -n get 198.18.0.4

# 3) 实际访问
curl -so /dev/null -w '%{http_code}
' https://www.google.com   # 200/204
curl -so /dev/null -w '%{http_code}
' https://www.baidu.com    # 200
```

### 在 <HOSTNAME> 上看分流决策

```sh
journalctl -u mihomo -f | grep -E 'google|baidu'
# [TCP] ... --> www.google.com:443 match RuleSet(gfw) using PROXY[balance]
# [TCP] ... --> www.baidu.com:443  match RuleSet(direct) using DIRECT
```

### 断网演练（验证 <HOSTNAME> 挂了不影响家里）

```sh
sudo systemctl stop mihomo
```

然后在手机上：
- 微信 / 淘宝 / 百度 → **应该正常**（走 Merlin 直连，DNS2 兜底）
- google.com → 打不开（预期，代理在 <HOSTNAME> 上）

如果国内网站也打不开，说明网关被误改成了 <HOSTNAME>，改回 Merlin 即可。

## 六、常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 全屋上不了网 | DHCP 网关被改成了 <HOSTNAME> | 改回 Merlin 的 IP |
| 所有网站解析失败 | nft 的 DNS 劫持没生效 | `sudo ~/home-proxy/scripts/deploy-mode-b.sh` 重跑，看自检项 |
| google 解析出 198.18 但打不开 | Merlin 静态路由没配 | 在设备上 `ip route get 198.18.0.4` 确认 |
| 只有部分设备能用 | 设备缓存了旧 DNS/路由 | 重启 Wi-Fi 或等 DHCP 续租 |
| 国内网站变慢 | 静态路由误写成 `0.0.0.0/0` | 删掉，只保留 `198.18.0.0/16` |
| <HOSTNAME> 重启后失效 | 没装 systemd 服务 | `sudo ~/home-proxy/scripts/install-service.sh` |

