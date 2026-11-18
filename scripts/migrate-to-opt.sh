#!/usr/bin/env bash
# ============================================================================
#  migrate-to-opt.sh — 把配置从仓库目录迁移到标准位置 /opt/mihomo
#  ---------------------------------------------------------------------------
#  为什么要迁移：
#    配置和 socket 原先放在 ~/home-proxy/config 和 ~/run-mihomo 下。
#    systemd 服务以 root 运行时，访问家目录可能被沙箱策略拦截
#    （实测报 mkdir: cannot create directory '/home/xxx': Permission denied）。
#    标准做法是放 /opt/mihomo（配置）和 /run/mihomo（运行时 socket）。
#
#  这条脚本做：
#    ① 复制 config/ → /opt/mihomo
#    ② 把 socket 目录改到 /run/mihomo
#    ③ 更新 ui/.env 里的三个路径
#    ④ 重启容器让新路径生效
#
#  用法： sudo ./scripts/migrate-to-opt.sh
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_CONF="$HERE/config"
DST_CONF=/opt/mihomo
SOCK_DIR=/run/mihomo

log() { printf '\e[1;32m==>\e[0m %s\n' "$*"; }
warn(){ printf '\e[1;33m[!]\e[0m %s\n' "$*"; }
die() { printf '\e[1;31m!!\e[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 sudo 运行： sudo $0"

[[ -f "$SRC_CONF/config.yaml" ]] || die "找不到源配置 $SRC_CONF/config.yaml"

# ---------------------------------------------------------------- 1. 停服务
if systemctl is-active --quiet mihomo 2>/dev/null; then
  log "停止 mihomo 服务"
  systemctl stop mihomo
fi
if pgrep -x mihomo >/dev/null; then
  log "停止手工运行的 mihomo"
  pkill -x mihomo || true
  sleep 2
fi

# ---------------------------------------------------------------- 2. 复制配置
log "复制配置 $SRC_CONF → $DST_CONF"
install -d -m 755 "$DST_CONF"
cp -a "$SRC_CONF"/. "$DST_CONF"/
chown -R root:root "$DST_CONF"
log "  共 $(find "$DST_CONF" -type f | wc -l) 个文件"

# ---------------------------------------------------------------- 3. socket 目录
install -d -m 755 "$SOCK_DIR"
log "socket 目录 $SOCK_DIR 就绪"

# ---------------------------------------------------------------- 4. 更新 UI .env
ENVF="$HERE/ui/.env"
if [[ -f "$ENVF" ]]; then
  log "更新 $ENVF"
  sed -i \
    -e "s|^MIHOMO_API=.*|MIHOMO_API=unix:///run/mihomo/ctl.sock|" \
    -e "s|^MIHOMO_HOME=.*|MIHOMO_HOME=$DST_CONF|" \
    -e "s|^MIHOMO_RUN_DIR=.*|MIHOMO_RUN_DIR=$SOCK_DIR|" \
    "$ENVF"
  grep -E "^MIHOMO_(API|HOME|RUN_DIR)" "$ENVF" | sed 's/^/  /'
fi

# ---------------------------------------------------------------- 5. 重启容器
if command -v docker >/dev/null && [[ -f "$HERE/ui/docker-compose.<HOSTNAME>.yml" ]]; then
  log "重启面板容器"
  ( cd "$HERE/ui" && docker compose -f docker-compose.<HOSTNAME>.yml --env-file .env up -d --force-recreate ) || \
    warn "容器重启失败，请手动检查"
fi

# ---------------------------------------------------------------- 6. 收尾提示
cat <<EOF

$(printf '\e[1;32m迁移完成\e[0m')

  配置已移到 : $DST_CONF
  socket 将在: $SOCK_DIR/ctl.sock

  原目录 $SRC_CONF 仍然保留（未删除），确认一切正常后可自行删除：
      sudo rm -rf $SRC_CONF

  下一步请运行安装脚本，它会用新的路径写 systemd unit：
      sudo $HERE/scripts/install-service.sh
EOF
