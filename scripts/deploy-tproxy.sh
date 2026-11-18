#!/usr/bin/env bash
# ============================================================================
#  deploy-tproxy.sh — 在 <HOSTNAME> (<SERVER_IP>) 上启用透明代理
#
#  这条脚本**必须用 root 跑**（TPROXY 需要 CAP_NET_ADMIN）：
#      sudo ~/home-proxy/scripts/deploy-tproxy.sh
#
#  它会做（幂等，可反复跑）：
#    1. 把 mihomo 装成 systemd 服务（root 身份，才能 bind TPROXY）
      并额外开 unix socket 控制接口（$CTL_SOCK），供 Web UI 容器访问
      —— 因为宿主 ufw 默认 DROP，容器的 TCP 连接到宿主机一律被拦，
         unix socket 不受防火墙影响。
#    2. 写 sysctl（ip_forward / rp_filter / conntrack）
#    3. 写策略路由（fwmark 0x1 -> table 100）
#    4. 装 nftables 规则（TPROXY + DNS 劫持 + NAT）
#    5. 启动并自检
#
#  不做：apt 装包（nft/ip 已具备）。如需装包会明确提示。
# ============================================================================
set -euo pipefail

LAN_IF=${LAN_IF:-enp0s31f6}
LAN_NET=${LAN_NET:-192.168.1.0/24}
LAN_ADDR=${LAN_ADDR:-$(ip -4 -o addr show "${LAN_IF:-eth0}" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -vE '^(169\.254\.|127\.)' | head -1)}
CTL_SOCK=${CTL_SOCK:-/run/mihomo/ctl.sock}
HOME_DIR=${HOME_DIR:-~/home-proxy}
SVC_USER=${SVC_USER:-$(id -un)}

[[ $EUID -eq 0 ]] || { echo "请用 sudo 运行： sudo $0"; exit 1; }

log() { printf '\e[1;32m==>\e[0m %s\n' "$*"; }
die() { printf '\e[1;31m!!\e[0m %s\n' "$*" >&2; exit 1; }

[[ -x $HOME_DIR/mihomo ]] || die "找不到 $HOME_DIR/mihomo"
[[ -f $HOME_DIR/config/config.yaml ]] || die "找不到 $HOME_DIR/config/config.yaml"
command -v nft >/dev/null || die "缺 nftables：sudo apt install nftables"

# ---------------------------------------------------------------- 1. systemd
log "安装 systemd 服务 mihomo"
install -d -m 755 "$(dirname "$CTL_SOCK")"
cat > /etc/systemd/system/mihomo.service <<EOF
[Unit]
Description=mihomo transparent proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$HOME_DIR
ExecStart=$HOME_DIR/mihomo -d $HOME_DIR/config -f $HOME_DIR/config/config.yaml -ext-ctl-unix $CTL_SOCK
Restart=always
RestartSec=3
LimitNOFILE=1048576
# TPROXY / REDIRECT 必需
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 2. sysctl
log "写入 sysctl"
cat > /etc/sysctl.d/99-mihomo-transp.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.route_localnet = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.conf.all.accept_local = 1
net.netfilter.nf_conntrack_max = 262144
net.netfilter.nf_conntrack_tcp_timeout_established = 7200
net.netfilter.nf_conntrack_udp_timeout = 60
EOF
sysctl -q -p /etc/sysctl.d/99-mihomo-transp.conf

# ---------------------------------------------------------------- 3. 策略路由
log "写入策略路由（TPROXY 必需）"
cat > /etc/systemd/system/mihomo-route.service <<'EOF'
[Unit]
Description=mihomo policy routing for TPROXY
Before=mihomo.service
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip rule add fwmark 0x1 lookup 100
ExecStart=/sbin/ip route add local 0.0.0.0/0 dev lo table 100
ExecStop=/sbin/ip rule del fwmark 0x1 lookup 100
ExecStop=/sbin/ip route flush table 100

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 4. nftables
log "写入 nftables 规则 (if=$LAN_IF net=$LAN_NET addr=$LAN_ADDR)"
sed -e "s|^define LAN_NET.*|define LAN_NET     = $LAN_NET|" \
    -e "s|^define LAN_ADDR.*|define LAN_ADDR    = $LAN_ADDR|" \
    -e "s|^define LAN_IF.*|define LAN_IF      = \"$LAN_IF\"|" \
    "$HOME_DIR/net/nftables-transp.conf" > /etc/nftables-mihomo.conf

cat > /etc/systemd/system/mihomo-nftables.service <<'EOF'
[Unit]
Description=nftables rules for mihomo transparent proxy
After=mihomo-route.service
Wants=mihomo-route.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables-mihomo.conf
ExecStop=/usr/sbin/nft delete table inet transp

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 5. 启动
log "启动服务"
systemctl daemon-reload
# 先停掉手工跑的 mihomo，避免抢 7895
pkill -x mihomo 2>/dev/null || true
sleep 1
systemctl enable --now mihomo-route.service
systemctl enable --now mihomo.service
sleep 4
systemctl enable --now mihomo-nftables.service

# ---------------------------------------------------------------- 6. 自检
echo
log "自检"
fail=0
systemctl is-active --quiet mihomo && echo "  ✓ mihomo 运行中" || { echo "  ✗ mihomo 未运行"; fail=1; }
ss -tln | grep -q ':7895' && echo "  ✓ TPROXY 监听 :7895" || { echo "  ✗ TPROXY :7895 未监听"; fail=1; }
ss -tln | grep -q ':1053' && echo "  ✓ DNS 监听 :1053" || { echo "  ✗ DNS :1053 未监听"; fail=1; }
nft list table inet transp >/dev/null 2>&1 && echo "  ✓ nft 规则已加载" || { echo "  ✗ nft 规则缺失"; fail=1; }
ip rule show | grep -q 'fwmark 0x1 lookup 100' && echo "  ✓ 策略路由已配" || { echo "  ✗ 策略路由缺失"; fail=1; }
sysctl -n net.ipv4.ip_forward | grep -q 1 && echo "  ✓ ip_forward=1" || { echo "  ✗ ip_forward 未开"; fail=1; }
[[ -S "$CTL_SOCK" ]] && echo "  ✓ 控制 socket $CTL_SOCK" || echo "  ! 控制 socket 未生成（Web UI 会连不上，但不影响代理）"
nft list table inet transp 2>/dev/null | grep -q masquerade && echo "  ✓ NAT (masquerade) 已配" || echo "  ! 缺少 masquerade（模式B 回程可能不通）"

echo
if [[ $fail -eq 0 ]]; then
  cat <<EOF
\e[1;32m透明代理已启用\e[0m

  LAN 客户端请把「网关」和「DNS」都设为： $LAN_ADDR
  UI 面板： http://$LAN_ADDR:8080

  Web UI： http://$LAN_ADDR:8081   （compose 部署，走 docker 端口发布）
  参考： sudo $HOME_DIR/scripts/set-mode.sh b    # 模式 B 的额外配置

  验证（在任意局域网设备上）：
    nslookup www.google.com $LAN_ADDR   # 应返回 198.18.x.x
    nslookup www.baidu.com  $LAN_ADDR   # 应返回真实 IP

  观察分流：
    journalctl -u mihomo -f | grep -E 'google|baidu'
EOF
else
  echo "有检查项失败，请查看： journalctl -u mihomo -n 40 --no-pager"
  exit 1
fi
