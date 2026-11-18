#!/usr/bin/env bash
# ============================================================================
#  e2e-test.sh — 端到端验证透明代理链路（在 network namespace 里模拟真实家庭网络）
#
#  链路: LAN 客户端(192.168.2.100)
#          --DNS--> 网关 192.168.2.2:53  [nft redirect]  -> mihomo :1053
#          <-fake-ip 198.18.x (gfwlist 命中) / 真实 IP (未命中)-
#          --TCP--> 198.18.x:443  [nft TPROXY] -> mihomo :7895
#          -> PROXY(server proxy) -> 代理桩 -> HTTP 200
#
#  需要 root（或用 `unshare -r -m -n` 在隔离环境里跑）。
#  用法:  sudo ./scripts/e2e-test.sh
#         unshare -r -m -n bash scripts/e2e-test.sh     # 无 root 的沙箱
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"

PASS=0; FAIL=0
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }
info(){ echo "    $*"; }

STATE=${E2E_STATE:-$(mktemp -d /tmp/e2e.XXXXXX)}; mkdir -p "$STATE"
MIHOMO_PID=""
STUB_PIDS=()

cleanup() {
  [[ -n $MIHOMO_PID ]] && kill -9 "$MIHOMO_PID" 2>/dev/null
  for p in "${STUB_PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null; done
  ip netns del cli 2>/dev/null
  ip link del veth-s 2>/dev/null
  nft delete table inet transp 2>/dev/null
  ip rule del fwmark 0x1 lookup 100 2>/dev/null
  ip route flush table 100 2>/dev/null
  [[ -n ${E2E_KEEP:-} ]] || rm -rf "$STATE"
}
trap cleanup EXIT

echo "=== 0. 准备测试网（namespace 模拟 LAN）==="
# 预检：端口被占会导致新旧实例抢同一条连接，结果不可信
for p in 7895 1053 17890 11081 5300; do
  if ss -tln 2>/dev/null | grep -q ":$p "; then
    echo "  ✗ 端口 $p 已被占用，请先停掉旧的 mihomo/测试桩：" >&2
    ss -tlnp 2>/dev/null | grep ":$p " >&2
    echo "    pkill -9 -f 'bin/mihomo'; pkill -9 -f 'stub.py'" >&2
    exit 2
  fi
done
ok "端口预检通过（7895/1053/17890/11081/5300 均空闲）"

# 预检：已存在的 mihomo 进程会抢走 fake-ip 映射（即使端口不复用也会污染 conntrack/日志）
if pgrep -x mihomo >/dev/null; then
  echo "  ✗ 已有 mihomo 进程在运行，请先停止：" >&2
  pgrep -a -x mihomo >&2
  echo "    pkill -9 -x mihomo" >&2
  exit 2
fi
ok "无残留 mihomo 进程"

mount -t tmpfs none /run 2>/dev/null || true
mkdir -p /run/netns
ip link set lo up
ip netns add cli 2>/dev/null || true
ip link add veth-s type veth peer name veth-c 2>/dev/null || true
ip link set veth-c netns cli
ip addr add 192.168.2.2/24 dev veth-s
ip link set veth-s up
ip netns exec cli ip addr add 192.168.2.100/24 dev veth-c
ip netns exec cli ip link set veth-c up
ip netns exec cli ip link set lo up
ip netns exec cli ip route add default via 192.168.2.2
ip netns exec cli ping -c1 -W2 192.168.2.2 >/dev/null \
  && ok "LAN 二层连通 (192.168.2.100 -> 192.168.2.2)" || bad "LAN 二层不通"

sysctl -qw net.ipv4.ip_forward=1
sysctl -qw net.ipv4.conf.all.rp_filter=0

# ---- 两个测试桩：伪 server-proxy(SOCKS5) + 伪上游 DNS
python3 "$HERE/scripts/test/socks5-stub.py" > "$STATE/socks.log" 2>&1 &
STUB_PIDS+=($!)
python3 "$HERE/scripts/test/dns-stub.py" 5300 > "$STATE/dns.log" 2>&1 &
STUB_PIDS+=($!)
sleep 2

# 桩必须真的起来了
ss -tln 2>/dev/null | grep -q ':11081' && ok "SOCKS5 代理桩 :11081" || { bad "代理桩未启动"; cat "$STATE/socks.log"; }

# ---- 生成测试用 mihomo 配置（改端口 + 指向桩，独立 state 目录）
python3 - "$STATE" <<'PY'
import pathlib, re, sys
state = sys.argv[1]
s = pathlib.Path("config/config.yaml").read_text()
s = s.replace("mixed-port: 7890", "mixed-port: 17890")
s = s.replace("port: 7891", "port: 17891")
s = s.replace("redir-port: 7892", "redir-port: 17892")
s = s.replace("external-controller: 127.0.0.1:9090", "external-controller: 127.0.0.1:19090")
# 代理指向本地 SOCKS5 桩
s = s.replace("server: <PROXY_HOST>", "server: 127.0.0.1")
s = s.replace("port: 1081", "port: 11081")
# 所有上游 DNS 指向本地 DNS 桩
for key in ("default-nameserver", "nameserver", "proxy-server-nameserver"):
    s = re.sub(r"^  %s:[^\n]*\n(    - .*\n)+" % key,
               "  %s:\n    - 127.0.0.1:5300\n" % key, s, flags=re.M)
s = re.sub(r"^    'rule-set:gfw,gfw-extra':[^\n]*\n(      - .*\n)+",
           "    'rule-set:gfw,gfw-extra':\n      - 127.0.0.1:5300\n", s, flags=re.M)
# 规则集用本地已下载副本，测试时不联网
s = s.replace("type: http\n    behavior: domain\n    format: yaml\n    interval: 86400\n    proxy: PROXY\n", "type: file\n    behavior: domain\n    format: yaml\n")
s = s.replace("type: http\n    behavior: ipcidr\n    format: yaml\n    interval: 86400\n    proxy: PROXY\n", "type: file\n    behavior: ipcidr\n    format: yaml\n")
s = re.sub(r"^    url: .*\n", "", s, flags=re.M)
# 单节点场景：PROXY 组默认直接选 server-proxy，避免 url-test 抢占 fake-ip
s = s.replace("    proxies: [auto, server-proxy, DIRECT]", "    proxies: [server-proxy, DIRECT]")
p = pathlib.Path(state) / "config.yaml"
p.write_text(s)
print(p)
PY

# 规则集放进独立 state 目录
cp -r config/providers "$STATE/providers"

sed -i 's/^log-level: info/log-level: debug/' "$STATE/config.yaml"
./bin/mihomo -d "$STATE" -f "$STATE/config.yaml" > "$STATE/mihomo.log" 2>&1 &
MIHOMO_PID=$!
for _ in $(seq 30); do
  ss -tln 2>/dev/null | grep -q ':7895' && break
  sleep 1
done

echo "=== 1. mihomo 入站监听 ==="
ss -tln 2>/dev/null | grep -q ':7895'  && ok "TPROXY 监听 :7895" || bad "TPROXY :7895 未监听"
ss -tln 2>/dev/null | grep -q ':1053'  && ok "DNS 监听 :1053"    || bad "DNS :1053 未监听"
grep -q "TProxy.*listening" "$STATE/mihomo.log" && ok "日志确认 TProxy listener 启动" || { bad "TProxy 日志缺失"; tail -20 "$STATE/mihomo.log"; }
grep -qE "bind: address already in use" "$STATE/mihomo.log" && bad "端口冲突（有旧实例在跑）" || true

echo "=== 2. 加载 nft 规则 + 策略路由 ==="
nft -f net/nftables-transp.conf && ok "nft 规则加载" || bad "nft 加载失败"
ip rule add fwmark 0x1 lookup 100 2>/dev/null
ip route add local 0.0.0.0/0 dev lo table 100 2>/dev/null \
  && ok "策略路由 (fwmark 0x1 -> table 100)" || bad "策略路由失败"

if [[ -n ${E2E_DEBUG:-} ]]; then
  echo "---- sanity: mixed-port 17890 直接访问代理桩 ----"
  curl -sS -m 8 -x http://127.0.0.1:17890 -o /dev/null -w 'http_code=%{http_code}\n' http://probe.test/ 2>&1 || echo "curl failed"
  ss -tnp 2>/dev/null | grep 17890 | head -3
fi

if [[ -n ${E2E_EARLY:-} ]]; then
  echo "---- 早期冒烟：DNS 查询前先连 fake-ip ----"
  ip netns exec cli timeout 6 python3 -c "
import socket
s=socket.socket();s.settimeout(5)
try:
  s.connect(('198.18.0.99',443));print('early-connect-ok')
except Exception as e:print('early-fail',repr(e))
"
  echo "---- mihomo log ----"; tail -5 "$STATE/mihomo.log" | grep -v '^time=.*provider'
fi

echo "=== 3. DNS 分流（LAN 客户端查 192.168.2.2:53，验证 nft 劫持）==="
# q <domain> -> 输出 "<rcode> <ips,逗号分隔>"
q() {
  ip netns exec cli python3 - "$1" <<'PY'
import socket, struct, random, sys
def skipname(d, i):
    while True:
        l = d[i]
        if l == 0: return i + 1
        if l & 0xC0 == 0xC0: return i + 2
        i += 1 + l
name = sys.argv[1]
tid = random.randint(0, 65535)
pkt = (struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
       + b"".join(bytes([len(x)]) + x.encode() for x in name.split("."))
       + b"\0" + struct.pack(">HH", 1, 1))
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(8)
s.sendto(pkt, ("192.168.2.2", 53))   # 查"网关"的 53，实际被 nft 重定向到 mihomo:1053
d = s.recvfrom(4096)[0]; s.close()
an = struct.unpack(">H", d[6:8])[0]
i = skipname(d, 12) + 4
out = []
for _ in range(an):
    i = skipname(d, i)
    t = struct.unpack(">H", d[i:i+2])[0]; i += 8
    rl = struct.unpack(">H", d[i:i+2])[0]; i += 2
    rd = d[i:i+rl]; i += rl
    if t == 1 and rl == 4:
        out.append(".".join(map(str, rd)))
print(d[3] & 0xf, ",".join(out) or "-")
PY
}

for d in www.google.com www.youtube.com twitter.com; do
  r=$(q "$d"); info "$d -> $r"
  [[ "$r" == "0 198.18."* ]] && ok "$d 拿到 fake-ip（gfwlist 命中）" || bad "$d 未拿到 fake-ip"
done

for d in www.baidu.com www.bilibili.com www.qq.com; do
  r=$(q "$d"); info "$d -> $r"
  [[ "$r" == "0 "* && "$r" != *"198.18."* ]] && ok "$d 拿到真实 IP（直连）" || bad "$d 分流错误"
done

echo "=== 4. TPROXY 实际转发（LAN 客户端 -> fake-ip -> mihomo -> 代理桩）==="
FAKEIP=$(q twitter.com | awk '{print $2}' | cut -d, -f1)
info "twitter fake-ip = $FAKEIP"
[[ "$FAKEIP" == 198.18.* ]] || bad "拿不到 fake-ip，后续测试无意义"

# url-test 健康检查会在启动头几秒占用 fake-ip 表；等它结束再测才稳定
# （真实家庭场景里设备总是在 mihomo 稳定运行后才发请求）
for _ in $(seq 20); do
  grep -qa "Finish A Health Checking" "$STATE/mihomo.log" && break
  sleep 1
done

out=""
for attempt in 1 2 3; do
  out=$(ip netns exec cli timeout 12 python3 - "$FAKEIP" <<'PY'
import socket, sys
ip = sys.argv[1]
s = socket.socket(); s.settimeout(10)
try:
    s.connect((ip, 443))
    s.sendall(b"GET / HTTP/1.0\r\nHost: twitter.com\r\n\r\n")
    print(s.recv(200).decode("utf-8", "replace").split("\r\n")[0])
except Exception as e:
    print("FAIL", e)
PY
)
  [[ "$out" == *"200"* ]] && break
  info "第 $attempt 次失败（$out），重试"
  sleep 2
done
info "响应: $out"
[[ "$out" == *"200"* ]] && ok "TPROXY 链路通（LAN -> 代理桩 -> HTTP 200）" || bad "TPROXY 链路不通"

if [[ -n ${E2E_DEBUG:-} ]]; then
  echo "---- nft counters ----"; nft -a list table inet transp 2>&1 | grep -E "counter .*(tproxy|redirect)" || true
  echo "---- mihomo tcp ----"; grep -aE "\[TCP\]" "$STATE/mihomo.log" | tail -5
fi

grep -qa "CONNECT twitter.com" "$STATE/socks.log" \
  && ok "fake-ip 已还原为域名 twitter.com（代理收到域名而非 IP）" || bad "fake-ip 未还原为域名"

echo "=== 5. 出口判定（mihomo 日志）==="
grep -aE "using PROXY|using DIRECT" "$STATE/mihomo.log" | tail -4 | sed 's/^/    /'
grep -qa "twitter.com.*match RuleSet(gfw) using PROXY" "$STATE/mihomo.log" \
  && ok "gfwlist 域名 -> PROXY" || bad "gfwlist 域名未走 PROXY"

# 未命中的域名走 DIRECT：经 mixed-port 显式验证（透明链路里直连不进 mihomo）
# 沙箱里没有真实外网，用 DIRECT 必定失败也足以证明「走的是 DIRECT 而非 PROXY」
curl -sS -m 10 -x http://127.0.0.1:17890 -o /dev/null http://www.baidu.com/ 2>/dev/null
if grep -qa "www.baidu.com.*match RuleSet(direct) using DIRECT" "$STATE/mihomo.log" \
   || grep -qa "dial DIRECT (match RuleSet/direct).*www.baidu.com" "$STATE/mihomo.log"; then
  ok "非 gfwlist 域名 -> DIRECT"
else
  bad "非 gfwlist 域名未走 DIRECT"
fi
# 反向确认：baidu 绝不应该出现在代理桩日志里
grep -qa "CONNECT www.baidu.com" "$STATE/socks.log" \
  && bad "baidu 竟然走了代理（分流错误）" || ok "baidu 未经过代理（分流正确）"

echo "=== 6. 代理桩收到的 CONNECT 目标 ==="
grep -a CONNECT "$STATE/socks.log" | tail -4 | sed 's/^/    /'

echo
echo "================ 结果: $PASS 通过 / $FAIL 失败 ================"
[[ $FAIL -eq 0 ]]
