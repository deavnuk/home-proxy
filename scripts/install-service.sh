#!/usr/bin/env bash
# ============================================================================
#  install-service.sh — 把 mihomo 装成 systemd 服务（开机自启 + 崩溃自愈）
#  ---------------------------------------------------------------------------
#  为什么必须这么做（实测教训）：
#    手工 nohup 起的 mihomo，机器重启后进程消失，
#    且 /run/mihomo/ctl.sock 是不存在了还是残留旧 inode，
#    都会导致 UI 面板 "连不上 mihomo"。systemd 能保证：
#      ① 开机自动启动
#      ② 崩溃 3 秒后自动重启
#      ③ RuntimeDirectory 保证 socket 目录存在且权限正确
#
#  用法（路径都可省略，脚本会自己推断）：
#     sudo ./scripts/install-service.sh
#     sudo ./scripts/install-service.sh --home /home/you/home-proxy/config --sock /run/mihomo/ctl.sock
#
#  ★ 注意：用 sudo 时不要写 ~ 或 $HOME —— 那是 root 的家目录。
#    省略参数最安全，脚本会用仓库所在位置推断。
#
#  幂等：可重复执行。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 配置目录：默认标准位置 /opt/mihomo
# 旧部署可能把配置放在仓库内（$HERE/config），检测到就自动迁移过来
MIHOMO_HOME=${MIHOMO_HOME:-/opt/mihomo}

# socket：标准位置 /run/mihomo（tmpfs，重启自动清空，由 RuntimeDirectory 创建）
SOCK=${SOCK:-/run/mihomo/ctl.sock}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) MIHOMO_HOME=$2; shift 2;;
    --sock) SOCK=$2; shift 2;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "未知参数: $1"; exit 1;;
  esac
done

log() { printf '\e[1;32m==>\e[0m %s\n' "$*"; }
warn(){ printf '\e[1;33m[!]\e[0m %s\n' "$*"; }
die() { printf '\e[1;31m!!\e[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 sudo 运行： sudo $0"
# ── 旧布局迁移：仓库内的 config/ → /opt/mihomo ─────────────────────────────
LEGACY_CONF="$HERE/config"
if [[ ! -f "$MIHOMO_HOME/config.yaml" && -f "$LEGACY_CONF/config.yaml" ]]; then
  log "检测到旧布局 $LEGACY_CONF，迁移到 $MIHOMO_HOME"
  install -d -m 755 "$MIHOMO_HOME"
  cp -a "$LEGACY_CONF"/. "$MIHOMO_HOME"/
  log "迁移完成（原目录保留，可自行删除）"
fi

[[ -f "$MIHOMO_HOME/config.yaml" ]] || die "找不到 $MIHOMO_HOME/config.yaml
  当前推断的目录: $MIHOMO_HOME
  请显式指定: sudo $0 --home /绝对路径/config"

# 二进制：优先用 MIHOMO_HOME 下的，其次仓库 bin/
BIN=""
for c in "$MIHOMO_HOME/mihomo" "$HERE/bin/mihomo" "$HERE/mihomo"; do
  [[ -x $c ]] && { BIN=$c; break; }
done
[[ -n $BIN ]] || die "找不到 mihomo 二进制"

# 二进制放在配置目录内：<MIHOMO_HOME>/mihomo
# 注意：不能写成 dirname(MIHOMO_HOME)/mihomo —— 当 MIHOMO_HOME=/opt/mihomo 时
# dirname 得到 /opt，拼出来正好是 /opt/mihomo（与目录同名），会变成「目录当程序执行」
BIN_DEST="$MIHOMO_HOME/mihomo"
# 源和目标是同一文件时跳过（仓库就在 MIHOMO_HOME 的上级时必然如此）
if [[ "$(readlink -f "$BIN")" == "$(readlink -f "$BIN_DEST" 2>/dev/null || echo "")" ]]; then
  log "二进制已在 $BIN_DEST，跳过复制"
elif [[ -f "$BIN_DEST" ]] && cmp -s "$BIN" "$BIN_DEST"; then
  log "二进制已是同一文件内容，跳过复制"
else
  log "安装二进制到 $BIN_DEST"
  install -m 755 "$BIN" "$BIN_DEST"
fi

# socket 要给 UI 容器（以 PUID 运行）读写，且家目录 750 时容器需能进入
CONF_OWNER=$(stat -c '%U:%G' "$MIHOMO_HOME/config.yaml" 2>/dev/null || echo root:root)
SOCK_DIR=$(dirname "$SOCK")
install -d -m 755 "$SOCK_DIR"

log "写入 /etc/systemd/system/mihomo.service"
# 生成 unit：注意替换顺序
#   1) 先替换 /opt/mihomo/mihomo -> 二进制真实路径（必须在替换目录之前）
#   2) 再替换 /opt/mihomo        -> 配置目录
#   3) socket 路径
sed -e "s|/opt/mihomo/mihomo|$BIN_DEST|g" \
    -e "s|/opt/mihomo|$MIHOMO_HOME|g" \
    -e "s|/run/mihomo/ctl.sock|$SOCK|g" \
    -e "s|\$CTL_SOCK_PATH|$SOCK|g" \
    "$HERE/systemd/mihomo.service" > /etc/systemd/system/mihomo.service

# socket 不在 /run 下时：RuntimeDirectory 不适用，改用 ExecStartPre 建目录
if [[ $SOCK != /run/* ]]; then
  sed -i "/^RuntimeDirectory/d" /etc/systemd/system/mihomo.service
  sed -i "s|^ExecStartPre=.*|ExecStartPre=/bin/mkdir -p $(dirname "$SOCK")|" /etc/systemd/system/mihomo.service
  grep -q "^ExecStartPre=" /etc/systemd/system/mihomo.service || \
    sed -i "s|^ExecStart=|ExecStartPre=/bin/mkdir -p $(dirname "$SOCK")\nExecStart=|" /etc/systemd/system/mihomo.service
fi


# 停掉手工进程，避免抢 7895 端口
if pgrep -x mihomo >/dev/null && ! systemctl is-active --quiet mihomo; then
  log "停止手工运行的 mihomo"
  pkill -x mihomo || true
  sleep 2
fi

systemctl daemon-reload
systemctl enable mihomo.service
systemctl restart mihomo.service
sleep 4

# ---------------------------------------------------------------- 自检
echo
log "自检"
fail=0
systemctl is-active --quiet mihomo && echo "  ✓ 服务运行中" || { echo "  ✗ 服务未运行"; fail=1; }
[[ -S $SOCK ]] && echo "  ✓ 控制 socket $SOCK" || { echo "  ✗ socket 未生成"; fail=1; }

SECRET=$(grep -oP '(?<=secret: ")["^"]+' "$MIHOMO_HOME/config.yaml" 2>/dev/null \
         || awk -F'"' '/^secret:/{print $2;exit}' "$MIHOMO_HOME/config.yaml")
if [[ -S $SOCK ]]; then
  code=$(curl -s -m 5 --unix-socket "$SOCK" \
         -H "Authorization: Bearer $SECRET" -o /dev/null -w '%{http_code}' \
         http://localhost/version || echo 000)
  [[ $code == 200 ]] && echo "  ✓ socket 可访问（/version = 200）" \
                     || { echo "  ✗ socket 访问异常（$code）"; fail=1; }
fi

systemctl is-enabled --quiet mihomo && echo "  ✓ 已设为开机自启" || { echo "  ✗ 未设置开机自启"; fail=1; }

echo
if [[ $fail -eq 0 ]]; then
  cat <<EOF
\e[1;32m服务安装完成\e[0m

  管理命令：
    sudo systemctl status mihomo      查看状态
    sudo systemctl restart mihomo     重启
    journalctl -u mihomo -f           看实时日志
    journalctl -u mihomo -n 50        看最近 50 行

  UI 面板会自动重连到 $SOCK，无需重启容器。
EOF
else
  echo "有检查项失败，请查看： journalctl -u mihomo -n 40 --no-pager"
  exit 1
fi
