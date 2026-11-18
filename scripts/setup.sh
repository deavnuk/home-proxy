#!/usr/bin/env bash
# ============================================================================
#  setup.sh — 【旧版】整包部署。已被下面两个脚本取代，仅保留用于「全新机器装依赖」
#  ---------------------------------------------------------------------------
#  ⚠️ 不要用它做正式部署。它会把 nft 规则按旧的 192.168.2.x 网段写死。
#
#  现在请用：
#     sudo ./scripts/install-service.sh     # 装 systemd 服务（开机自启）
#     sudo ./scripts/deploy-mode-b.sh       # 部署网络规则（自动推导网段）
#
#  本脚本唯一还有用的部分：在全新机器上装 apt 依赖 + 下载 mihomo 二进制。
#  可以这样只用这一步：
#     ./scripts/setup.sh --deps-only
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=/opt/mihomo
CONF_DIR=/etc/mihomo
SVC_USER=mihomo
SVC=mihomo

LAN_IF=eth0
LAN_NET=192.168.2.0/24
LAN_ADDR=192.168.2.2
DO_NET=1
DEPS_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lan-if)   LAN_IF=$2; shift 2;;
    --lan-net)  LAN_NET=$2; shift 2;;
    --lan-addr) LAN_ADDR=$2; shift 2;;
    --no-net)   DO_NET=0; shift;;
    --deps-only) DEPS_ONLY=1; shift;;
    -h|--help)  sed -n '2,15p' "$0"; exit 0;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "需要 root 运行"; exit 1; }

log() { printf '\e[1;32m==>\e[0m %s\n' "$*"; }
die() { printf '\e[1;31m!!\e[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. 依赖
log "安装依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates nftables iproute2 tcpdump less jq \
  >/dev/null

if [[ $DEPS_ONLY -eq 1 ]]; then
  log "依赖已安装，按 --deps-only 退出。"
  log "接下来请执行： sudo ./scripts/install-service.sh && sudo ./scripts/deploy-mode-b.sh"
  exit 0
fi

# ---------------------------------------------------------------- 2. 二进制
log "安装 mihomo 到 $PREFIX"
install -d -m 755 "$PREFIX" "$CONF_DIR" "$CONF_DIR/providers"

if [[ -x "$HERE/bin/mihomo" ]]; then
  install -m 755 "$HERE/bin/mihomo" "$PREFIX/mihomo"
else
  # 没带二进制就现场下载（走系统代理或直连）
  ARCH=$(uname -m); case "$ARCH" in x86_64) A=amd64;; aarch64) A=arm64;; *) die "unsupported $ARCH";; esac
  VER=$(curl -fsSL --max-time 20 https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
        | jq -r .tag_name)
  TMP=$(mktemp -d)
  log "下载 mihomo $VER ($A)"
  curl -fsSL --max-time 180 -o "$TMP/m.gz" \
    "https://github.com/MetaCubeX/mihomo/releases/download/$VER/mihomo-linux-$A-$VER.gz" || \
  curl -fsSL --max-time 180 -o "$TMP/m.gz" \
    "https://github.com/MetaCubeX/mihomo/releases/download/$VER/mihomo-linux-$A-compatible-$VER.gz"
  gunzip -c "$TMP/m.gz" > "$TMP/mihomo"
  install -m 755 "$TMP/mihomo" "$PREFIX/mihomo"
  rm -rf "$TMP"
fi
"$PREFIX/mihomo" -v

# ---------------------------------------------------------------- 3. 用户
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$PREFIX" "$SVC_USER"

# ---------------------------------------------------------------- 4. 配置
log "部署配置"
[[ -f $CONF_DIR/config.yaml ]] && cp -a "$CONF_DIR/config.yaml" "$CONF_DIR/config.yaml.bak.$(date +%s)"
install -m 600 "$HERE/config/config.yaml" "$CONF_DIR/config.yaml"
for f in "$HERE"/config/providers/*.yaml; do
  [[ -e "$f" ]] && install -m 600 "$f" "$CONF_DIR/providers/$(basename "$f")"
done
# GeoIP 数据库（规则里没用到 GEOIP，但 mihomo 启动会尝试下载，预置可避免等待）
if [[ ! -f $CONF_DIR/geoip.metadb ]]; then
  curl -fsSL --max-time 120 -o "$CONF_DIR/geoip.metadb" \
    "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/release/geoip/metadb/geoip.metadb" || \
    log "geoip 下载失败（可忽略）"
fi
[[ -f $CONF_DIR/geoip.metadb ]] && chown "$SVC_USER:$SVC_USER" "$CONF_DIR"/geoip.metadb || true
chown -R "$SVC_USER:$SVC_USER" "$CONF_DIR"

# 配置自检
log "校验配置"
sudo -u "$SVC_USER" "$PREFIX/mihomo" -d "$CONF_DIR" -t || die "配置校验失败"

# ---------------------------------------------------------------- 5. systemd
log "写入 systemd 服务"
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=mihomo transparent proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
ExecStart=$PREFIX/mihomo -d $CONF_DIR
Restart=always
RestartSec=3
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF

install -d "/etc/systemd/system/$SVC.service.d"

systemctl daemon-reload
systemctl enable --now "$SVC"
sleep 3
systemctl is-active --quiet "$SVC" || { journalctl -u "$SVC" -n 40 --no-pager; die "服务启动失败"; }
log "mihomo 运行中"

# ---------------------------------------------------------------- 6. sysctl
log "调整内核参数"
cat > /etc/sysctl.d/99-mihomo-transp.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.route_localnet = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
# TPROXY 需要：允许本地投递 & 关闭源地址校验
net.ipv4.conf.all.accept_local = 1
# conntrack 容量（家用 65536 够，上 262144 更宽松）
net.netfilter.nf_conntrack_max = 262144
net.netfilter.nf_conntrack_tcp_timeout_established = 7200
net.netfilter.nf_conntrack_udp_timeout = 60
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
EOF
sysctl -q -p /etc/sysctl.d/99-mihomo-transp.conf
modprobe nf_conntrack 2>/dev/null || true

if [[ $DO_NET -eq 0 ]]; then
  log "--no-net：跳过 nft/路由"
  log "完成"
  exit 0
fi

# ---------------------------------------------------------------- 7. 策略路由
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
# TPROXY 把 mark=0x1 的包交给本地 lo 表，才能被本地监听接收
ExecStart=/sbin/ip rule add fwmark 0x1 lookup 100
ExecStart=/sbin/ip route add local 0.0.0.0/0 dev lo table 100
ExecStop=/sbin/ip rule del fwmark 0x1 lookup 100
ExecStop=/sbin/ip route flush table 100

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now mihomo-route.service

# ---------------------------------------------------------------- 8. nftables
log "写入 nftables 规则 ($LAN_IF / $LAN_NET / $LAN_ADDR)"
sed -e "s|^define LAN_NET.*|define LAN_NET     = $LAN_NET     # 家庭内网段|" \
    -e "s|^define LAN_ADDR.*|define LAN_ADDR    = $LAN_ADDR     # 本机内网地址|" \
    -e "s|^define LAN_IF.*|define LAN_IF      = \"$LAN_IF\"|" \
    "$HERE/net/nftables-transp.conf" > /etc/nftables-mihomo.conf

# 保留 distro 默认结论：只加载我们的表，不 flush 整个 ruleset
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
systemctl daemon-reload
systemctl enable --now mihomo-nftables.service
nft list table inet transp >/dev/null && log "nft 规则已加载"

# ---------------------------------------------------------------- 9. 收尾
log "检测系统 systemd-resolved 是否占用 53"
if systemctl is-active --quiet systemd-resolved; then
  log "systemd-resolved 运行中；mihomo DNS 监听 1053，nft 会把 LAN 的 :53 重定向过去，无需停用"
fi

cat <<EOF

\e[1;32m部署完成\e[0m

  代理内核   : $PREFIX/mihomo   ($("$PREFIX/mihomo" -v 2>&1 | head -1))
  配置目录   : $CONF_DIR
  日志       : journalctl -u mihomo -f
  面板       : http://$LAN_ADDR:9090/ui  （secret 见 config.yaml）

接下来在路由器（merlin）上：
  1) DHCP 下发的「网关」和「DNS」都指向 $LAN_ADDR
  2) 或在 merlin 的 LAN 设置里把默认网关改成 $LAN_ADDR
  详见 router/merlin-setup.md

本机自检：
  curl -x http://127.0.0.1:7890 -o /dev/null -w '%{http_code}\n' https://www.google.com/generate_204
  dig @127.0.0.1 -p 1053 www.google.com      # 应返回 198.18.x.x
  dig @127.0.0.1 -p 1053 www.baidu.com       # 应返回真实 IP
EOF
