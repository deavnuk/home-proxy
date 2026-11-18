#!/usr/bin/env bash
# ============================================================================
#  deploy-mode-b.sh — 模式 B 一键部署（网关保持 Merlin，只让 DNS + fake-ip 走本机）
#  ---------------------------------------------------------------------------
#  为什么用模式 B（而不是把网关改成本机）：
#    网关改成本机 = 全屋所有流量都要过这台机器。这台机器一挂，家里完全断网。
#    模式 B 只把「DNS」和「fake-ip 网段」引到本机：
#      国内流量： 客户端 → Merlin → 外网          （不碰本机）
#      被墙流量： 客户端 → Merlin → 本机 → 代理
#    本机挂掉时，国内网站照常能用，只有被墙网站上不去。
#
#  本脚本做三件事：
#    ① sysctl（ip_forward 等）
#    ② 策略路由（TPROXY 必需）
#    ③ nftables（TPROXY 劫持 + LAN :53 重定向到 mihomo 1053）
#
#  用法：
#     sudo ./scripts/deploy-mode-b.sh
#     sudo ./scripts/deploy-mode-b.sh --lan-if enp0s31f6 --lan-net <LAN_NET> --lan-addr <SERVER_IP>
#
#  幂等，可反复执行。
# ============================================================================
set -euo pipefail

LAN_IF=${LAN_IF:-$(ip -4 route show default | awk '{print $5; exit}')}
# 家庭网段（Merlin AC68U 的 LAN）
LAN_NET=${LAN_NET:-192.168.1.0/24}   # 默认值，建议用 --lan-net 指定实际网段
# 本机在家庭网段中可被访问的地址（Merlin 静态路由指向它）
LAN_ADDR=${LAN_ADDR:-$(ip -4 -o addr show "$LAN_IF" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -vE '^(169\.254\.|127\.)' | head -1)}
SOCK=${SOCK:-/run/mihomo/ctl.sock}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lan-if)   LAN_IF=$2; shift 2;;
    --lan-net)  LAN_NET=$2; shift 2;;
    --lan-addr) LAN_ADDR=$2; shift 2;;
    -h|--help)  sed -n '2,26p' "$0"; exit 0;;
    *) echo "未知参数: $1"; exit 1;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\e[1;32m==>\e[0m %s\n' "$*"; }
warn(){ printf '\e[1;33m[!]\e[0m %s\n' "$*"; }
die() { printf '\e[1;31m!!\e[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 sudo 运行： sudo $0"

log "网卡=$LAN_IF  本机=$LAN_ADDR  网段=$LAN_NET"

# 一致性检查：本机地址必须在这个网段内，否则 nft 规则匹配不到任何流量
case "$LAN_ADDR" in
  ${LAN_NET%.0/24}.*) : ;;
  *)
    warn "本机地址 $LAN_ADDR 不在网段 $LAN_NET 内"
    warn "如果你的客户端在 $LAN_NET，请先让这台机器在该网段有一个地址"
    warn "或用 --lan-addr 指定一个 $LAN_NET 网段内的地址"
    ;;
esac

# ---------------------------------------------------------------- 1. sysctl
log "配置内核参数"
cat > /etc/sysctl.d/99-mihomo-transp.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.route_localnet = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.conf.all.accept_local = 1
net.netfilter.nf_conntrack_max = 262144
EOF
sysctl -q -p /etc/sysctl.d/99-mihomo-transp.conf

# ---------------------------------------------------------------- 2. 策略路由
log "配置策略路由（TPROXY 必需）"
ip rule del fwmark 0x1 lookup 100 2>/dev/null || true
ip rule add fwmark 0x1 lookup 100
ip route flush table 100 2>/dev/null || true
ip route add local 0.0.0.0/0 dev lo table 100

cat > /etc/systemd/system/mihomo-route.service <<'EOF'
[Unit]
Description=mihomo policy routing (TPROXY)
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

# ---------------------------------------------------------------- 3. nftables
log "生成并加载 nftables 规则"
sed -e "s|^define LAN_NET.*|define LAN_NET     = $LAN_NET|" \
    -e "s|^define LAN_ADDR.*|define LAN_ADDR    = $LAN_ADDR|" \
    -e "s|^define LAN_IF.*|define LAN_IF      = \"$LAN_IF\"|" \
    "$HERE/net/nftables-transp.conf" > /etc/nftables-mihomo.conf

nft -f /etc/nftables-mihomo.conf

cat > /etc/systemd/system/mihomo-nftables.service <<'EOF'
[Unit]
Description=nftables rules for mihomo (TPROXY + DNS hijack)
After=mihomo-route.service network-online.target
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
systemctl enable --now mihomo-route.service 2>/dev/null || true
systemctl enable --now mihomo-nftables.service 2>/dev/null || true

# ---------------------------------------------------------------- 4. 自检
echo
log "自检"
fail=0

ss -tln 2>/dev/null | grep -q ':1053' && echo "  ✓ mihomo DNS 监听 :1053" \
  || { echo "  ✗ mihomo DNS 未监听（先启动 mihomo）"; fail=1; }

ss -tln 2>/dev/null | grep -q ':7895' && echo "  ✓ TPROXY 监听 :7895" \
  || { echo "  ✗ TPROXY 未监听（mihomo 需以 root 运行）"; fail=1; }

nft list table inet transp >/dev/null 2>&1 && echo "  ✓ nft 规则已加载" \
  || { echo "  ✗ nft 规则加载失败"; fail=1; }

ip rule show | grep -q 'fwmark 0x1 lookup 100' && echo "  ✓ 策略路由已配" \
  || { echo "  ✗ 策略路由缺失"; fail=1; }

[[ "$(sysctl -n net.ipv4.ip_forward)" == "1" ]] && echo "  ✓ ip_forward=1" \
  || { echo "  ✗ ip_forward 未开"; fail=1; }

# DNS 劫持是否生效：从本机向自己 LAN IP 的 53 发查询，应被重定向到 1053
if command -v python3 >/dev/null; then
  r=$(python3 - "$LAN_ADDR" <<'PY' 2>/dev/null || echo FAIL
import socket,struct,random,sys
addr=sys.argv[1]; tid=random.randint(0,65535)
pkt=struct.pack(">HHHHHH",tid,0x0100,1,0,0,0)+b"\x03www\x06google\x03com\x00"+struct.pack(">HH",1,1)
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(5)
try:
    s.sendto(pkt,(addr,53)); d=s.recvfrom(4096)[0]
    print("OK" if (d[3]&0xf)==0 else "FAIL")
except Exception:
    print("FAIL")
PY
)
  [[ "$r" == "OK" ]] && echo "  ✓ DNS 劫持生效（$LAN_ADDR:53 有响应）" \
    || { echo "  ✗ DNS 劫持未生效（$LAN_ADDR:53 无响应）"; fail=1; }
fi

echo
if [[ $fail -eq 0 ]]; then
  cat <<EOF
\e[1;32m模式 B 部署完成\e[0m

接下来在 Merlin 上配置（一次性）：

  1) DHCP 服务器
       默认网关          : 保持现状（不要改成 $LAN_ADDR）
       DNS Server 1      : $LAN_ADDR
       DNS Server 2      : <GATEWAY_IP>     ← 填 Merlin 自己，作为备用
                           （这个机器挂掉时，DNS 仍能解析，家里不断网）
       通告路由器为 DNS  : No

  2) 静态路由（高级设置 → 局域网 → 路由）
       网络/主机 IP : 198.18.0.0
       网络掩码     : 255.255.0.0
       网关         : $LAN_ADDR
       接口         : LAN

验证：
  在任意局域网设备上：
    nslookup www.google.com   → 应返回 198.18.x.x
    nslookup www.baidu.com    → 应返回真实 IP
    curl -so /dev/null -w '%{http_code}' https://www.google.com   → 200/204

  本机挂掉时的预期表现：
    国内网站照常访问 ✓
    被墙网站上不去   （因为代理在本机）
EOF
else
  echo "有检查项失败，请排查后重跑。"
  echo "常见原因：mihomo 未以 root 运行（TPROXY 需要 CAP_NET_ADMIN）"
  exit 1
fi
