#!/usr/bin/env bash
# ============================================================================
#  set-mode.sh — 模式 B 的配置指引与自检
#  ---------------------------------------------------------------------------
#  本方案只采用模式 B：
#    客户端网关仍是 Merlin，只把 fake-ip 段（198.18.0.0/16）路由到本机。
#    · 国内流量完全不经过本机
#    · 本机挂掉时国内网站照常可用
#
#  为什么不用「网关指向本机」的方式：
#    那样全屋所有流量都要过本机，本机一挂等于全家断网。
#
#  用法：
#     ./scripts/set-mode.sh b          # 打印 Merlin 配置指引 + 自检
#     sudo ./scripts/set-mode.sh status # 查看当前状态（需要 root 读 nft）
# ============================================================================
set -uo pipefail

LAN_IF=${LAN_IF:-$(ip -4 route show default | awk '{print $5; exit}')}
LAN_ADDR=${LAN_ADDR:-$(ip -4 -o addr show "$LAN_IF" | awk '{print $4}' | cut -d/ -f1)}
LAN_NET=${LAN_NET:-$(ip -4 -o addr show "$LAN_IF" | awk '{print $4}')}
GW=${REAL_GW:-$(ip -4 route show default | awk '{print $3; exit}')}
FAKEIP=${FAKEIP:-198.18.0.0/16}

c() { printf '\e[1;36m%s\e[0m\n' "$*"; }
g() { printf '\e[1;32m%s\e[0m\n' "$*"; }
y() { printf '\e[1;33m%s\e[0m\n' "$*"; }
r() { printf '\e[1;31m%s\e[0m\n' "$*"; }

need_root() {
  [[ $EUID -eq 0 ]] || { r "该子命令需要 root： sudo $0 $*"; exit 1; }
}

status() {
  need_root
  c "── 网络 ──"
  echo "  网卡     : $LAN_IF"
  echo "  本机地址 : $LAN_ADDR"
  echo "  网段     : $LAN_NET"
  echo "  默认网关 : $GW"
  echo
  c "── 透明代理 ──"
  systemctl is-active --quiet mihomo && g "  mihomo        : 运行中" || r "  mihomo        : 未运行"
  ss -tln 2>/dev/null | grep -q ':7895' && g "  TPROXY :7895  : 监听中" || r "  TPROXY :7895  : 未监听"
  ss -tln 2>/dev/null | grep -q ':1053' && g "  DNS :1053     : 监听中" || r "  DNS :1053     : 未监听"
  nft list table inet transp >/dev/null 2>&1 && g "  nft 规则      : 已加载" || r "  nft 规则      : 未加载"
  ip rule show | grep -q 'fwmark 0x1 lookup 100' && g "  策略路由      : 已配置" || r "  策略路由      : 缺失"
  echo
  c "── 分流统计（关键：只有 fake-ip 流量被劫持）──"
  nft -a list table inet transp 2>/dev/null | grep -E "counter.*tproxy" | sed 's/^/  /' \
    || echo "  （看不到计数器）"
  echo
  c "── 客户端路由检查 ──"
  echo "  在任意局域网设备上执行，确认 fake-ip 会走本机："
  echo "    ip route get 198.18.0.4      # 应显示 via $LAN_ADDR"
}


mode_b() {
  c "══ 模式 B：网关仍是 Merlin，只路由 fake-ip 到本机 ══"
  cat <<EOF

第 1 步 —— Merlin 的 DHCP 只改 DNS，网关保持不变：

    高级设置 → 局域网 → DHCP 服务器
      ┌──────────────────────────────┬─────────────────────┐
      │ 默认网关                     │ $GW        ← 保持不变
      │ DNS Server 1                 │ $LAN_ADDR
      │ DNS Server 2                 │ （留空！）
      └──────────────────────────────┴─────────────────────┘

第 2 步 —— Merlin 上加一条静态路由（把 fake-ip 交给本机）：

    高级设置 → 局域网 → 路由（Static Route）
      ┌──────────────┬──────────────┬──────────────────┬──────┐
      │ 网络/主机 IP │ 网络掩码     │ 网关             │ 接口 │
      ├──────────────┼──────────────┼──────────────────┼──────┤
      │ 198.18.0.0   │ 255.255.0.0  │ $LAN_ADDR        │ LAN  │
      └──────────────┴──────────────┴──────────────────┴──────┘

    （若 Merlin 是 Asuswrt/Merlin，也可 SSH 执行：
       nvram set lan_ifnames_route=... 不推荐，优先用 Web UI）

效果：
  · 国内流量：client → Merlin → 外网        （完全不经过本机）✓
  · 被墙流量：client → Merlin → 本机 → mihomo → 代理 → 外网
  · 只有被墙的流量占用本机带宽与 CPU

⚠️ 前提：本机必须能「自己出网」（有默认路由），
   否则 fake-ip 流量到了本机后 mihomo 无法连代理节点。
EOF
  echo
  c "── 本机出网自检 ──"
  if ip route show default | grep -q .; then
    g "  ✓ 默认路由存在：$(ip route show default | head -1)"
  else
    r "  ✗ 没有默认路由！模式 B 下本机会无法连接代理节点"
    echo "     请先给本机配置网关： sudo ip route add default via <上游网关>"
  fi
  if [[ $EUID -eq 0 ]]; then
    sysctl -qw net.ipv4.ip_forward=1 && g "  ✓ ip_forward 已开启"
  else
    y "  （非 root：未自动改 sysctl，请确认 net.ipv4.ip_forward=1）"
  fi

  echo
  c "── 验证 fake-ip 是否真的会路由到本机 ──"
  if command -v ipset >/dev/null 2>&1; then :; fi
  echo "  在任意客户端执行（需先配好 Merlin 静态路由）："
  echo "    ip route get 198.18.0.4        # 应显示 via <本机IP>"
  echo "    nslookup www.google.com $LAN_ADDR   # 得到 198.18.x.x"
  echo "    curl -so /dev/null -w '%{http_code}' https://www.google.com"
  echo
  y "提示：nft 规则由 deploy-mode-b.sh 部署，无需单独修改。"

  echo
  c "── 本机作为路由：确认 NAT ──"
  if nft list table inet transp >/dev/null 2>&1 && nft list table inet transp 2>/dev/null | grep -q masquerade; then
    g "  ✓ 已包含 masquerade（NAT）"
  else
    y "  ! 未检测到 masquerade，模式 B 下客户端回包可能不通"
  fi
}

case "${1:-b}" in
  b|B|route|"")  mode_b ;;
  status)        status ;;
  *) r "用法: $0 {b|status}   （本方案只支持模式 B）"; exit 1 ;;
esac
