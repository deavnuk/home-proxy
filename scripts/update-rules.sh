#!/usr/bin/env bash
# ============================================================================
#  update-rules.sh — 刷新规则集（由 mihomo-update.timer 每天调用）
#    · 官方 gfwlist  -> providers/gfwlist-extra.yaml   (本脚本)
#    · gfw/cncidr/direct/private/reject 由 mihomo 内部按 interval 自动拉取
#  拉取失败时保留旧文件（绝不写空，避免把分流打崩）
# ============================================================================
set -euo pipefail

CONF_DIR=${CONF_DIR:-/etc/mihomo}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=${PY:-python3}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# mihomo 必须先起来：它的 mixed-port 用作抓取代理（防污染）
PROXY_HTTP="${PROXY_HTTP:-http://127.0.0.1:7890}"

log "开始更新规则集"

# ---- 1) 官方 gfwlist -> gfw-extra ----
OK=0
for attempt in 1 2 3; do
  if https_proxy="$PROXY_HTTP" http_proxy="$PROXY_HTTP" \
     "$PY" "$HERE/scripts/gfwlist2clash.py" --out "$TMP/gfwlist-extra.yaml"; then
    OK=1; break
  fi
  log "gfwlist 第 $attempt 次失败，5s 后重试"
  sleep 5
done

if [[ $OK -eq 1 ]] && [[ -s "$TMP/gfwlist-extra.yaml" ]]; then
  CNT=$(grep -c "^  - " "$TMP/gfwlist-extra.yaml" || echo 0)
  if [[ $CNT -ge 500 ]]; then
    install -m 600 -o mihomo -g mihomo "$TMP/gfwlist-extra.yaml" \
      "$CONF_DIR/providers/gfwlist-extra.yaml"
    log "gfw-extra 更新成功 ($CNT 条)"
    CHANGED=1
  else
    log "gfw-extra 条目过少 ($CNT)，保留旧文件"
    CHANGED=0
  fi
else
  log "gfwlist 拉取失败，保留旧文件"
  CHANGED=0
fi

# ---- 2) 让 mihomo 重新加载（rule-provider 的 interval 也会自己刷，这里确保立即生效）----
if [[ "${CHANGED:-0}" -eq 1 ]]; then
  SECRET=$(awk -F'"' '/^secret:/{print $2; exit}' "$CONF_DIR/config.yaml")
  if curl -fsS -m 10 -X PUT -H "Authorization: Bearer $SECRET" \
      "http://127.0.0.1:9090/configs?force=true" \
      -H 'Content-Type: application/json' \
      -d "{\"path\":\"$CONF_DIR/config.yaml\",\"payload\":\"\"}" >/dev/null 2>&1; then
    log "已通知 mihomo 重载配置"
  else
    log "API 重载失败（mihomo 本身会按 interval 自行刷新，可忽略）"
  fi
fi

log "规则更新结束"
