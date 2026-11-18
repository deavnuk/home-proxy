"""一键体检 —— 回答「到底哪一环坏了」。

设计原则：
  · 每项检查互相独立，单项失败不影响其他项
  · 不通过时一定给出可复制的修复命令（fix 字段）
  · 只读，绝不修改系统状态
"""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from . import mihomo, nft
from .settings import settings

router = APIRouter(prefix="/api")


async def _run(cmd: list[str], timeout: float = 4.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode("utf-8", "replace").strip()
    except (asyncio.TimeoutError, FileNotFoundError, PermissionError, OSError):
        return 127, ""


def _r(name: str, ok: bool, detail: str, fix: str = "", level: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "fix": fix if not ok else "",
            "level": level}


# ---------------------------------------------------------------- 单项检查
async def check_mihomo() -> dict[str, Any]:
    try:
        v = await mihomo.get("/version", timeout=4)
        return _r("mihomo 进程", True, f"运行中，版本 {v.get('version', '?')}")
    except mihomo.MihomoError as e:
        return _r("mihomo 进程", False, e.message,
                  "sudo systemctl status mihomo\nsudo systemctl start mihomo")


async def check_listeners() -> dict[str, Any]:
    """检查 TPROXY / DNS 端口是否在监听（通过 mihomo 配置 + 探测）。"""
    try:
        cfg = await mihomo.get("/configs", timeout=4)
    except mihomo.MihomoError:
        return _r("监听端口", False, "读不到配置，无法判断", "sudo systemctl restart mihomo")

    tproxy = cfg.get("tproxy-port", 0)
    if not tproxy:
        return _r("TPROXY 端口", False,
                  "config.yaml 里没配 tproxy-port",
                  "在 config.yaml 加上 tproxy-port: 7895，然后热重载")

    rc, out = await _run(["ss", "-tln"])
    if rc != 0:
        return _r("TPROXY 端口", False, "容器内无法查看端口（正常）",
                  "在宿主机执行： ss -tln | grep 7895", level="warn")
    if f":{tproxy} " in out or f":{tproxy}\t" in out:
        return _r("TPROXY 端口", True, f":{tproxy} 监听中")
    return _r("TPROXY 端口", False, f":{tproxy} 未监听",
              "sudo systemctl restart mihomo\njournalctl -u mihomo -n 30")


async def check_nft() -> dict[str, Any]:
    st = await nft.status()
    if not st.get("available"):
        return _r("nft 规则", False, st.get("reason", "不可用"),
                  "\n".join(x for x in (st.get("hints") or []) if x), level="warn")
    pr = st.get("policy_route") or {}
    if pr.get("rule") and pr.get("local_route"):
        return _r("nft 规则", True, f"{st.get('rule_count', 0)} 条链，策略路由就绪")
    return _r("nft 规则", False, "策略路由缺失，TPROXY 收不到包",
              "\n".join(x for x in (st.get("hints") or []) if x))


async def check_forwarding() -> dict[str, Any]:
    rc, out = await _run(["sysctl", "-n", "net.ipv4.ip_forward"])
    if rc != 0:
        return _r("IP 转发", False, "容器内读不到（正常，应在宿主机检查）",
                  "在宿主机执行： sysctl net.ipv4.ip_forward   # 应为 1", level="warn")
    if out.strip() == "1":
        return _r("IP 转发", True, "net.ipv4.ip_forward = 1")
    return _r("IP 转发", False, f"net.ipv4.ip_forward = {out.strip()}",
              "sudo sysctl -w net.ipv4.ip_forward=1\n"
              "echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-mihomo.conf")


async def check_nodes() -> dict[str, Any]:
    """至少一个节点可用。"""
    try:
        raw = await mihomo.get("/providers/proxies", timeout=6)
    except mihomo.MihomoError as e:
        return _r("可用节点", False, e.message, "检查 mihomo 是否正常")

    all_nodes: list[tuple[str, bool]] = []
    for pname, p in (raw.get("providers") or {}).items():
        if p.get("vehicleType") == "Compatible":
            continue
        for x in p.get("proxies") or []:
            all_nodes.append((x.get("name", "?"), bool(x.get("alive"))))

    if not all_nodes:
        return _r("可用节点", False, "没有任何订阅节点",
                  "在「订阅」页添加订阅链接，或检查订阅是否过期")
    alive = [n for n, a in all_nodes if a]
    if alive:
        return _r("可用节点", True, f"{len(alive)}/{len(all_nodes)} 个节点可用")
    return _r("可用节点", False, f"全部 {len(all_nodes)} 个节点不可用",
              "在「订阅」页点「刷新」，或检查节点是否过期\n"
              "也可在「代理」页手动测速确认")


async def check_rules() -> dict[str, Any]:
    """规则集是否新鲜（>3 天未更新视为陈旧）。"""
    try:
        raw = await mihomo.get("/providers/rules", timeout=6)
    except mihomo.MihomoError as e:
        return _r("规则集", False, e.message, "检查 mihomo 是否正常")

    now = datetime.now(timezone.utc)
    stale, total = [], 0
    for name, p in (raw.get("providers") or {}).items():
        total += 1
        ts = p.get("updatedAt")
        if not ts or ts.startswith("0001"):
            stale.append(f"{name}(从未更新)")
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if (now - dt).total_seconds() > 3 * 86400:
                stale.append(f"{name}({(now - dt).days}天前)")
        except ValueError:
            pass

    if not stale:
        return _r("规则集", True, f"{total} 个规则集均为最新")
    return _r("规则集", False, f"{len(stale)} 个陈旧：{', '.join(stale[:3])}",
              "在「规则」页点各规则集的「刷新」\n"
              "或检查网络是否能访问 raw.githubusercontent.com")


async def check_socket() -> dict[str, Any]:
    if not settings.is_unix_socket:
        return _r("控制接口", True, f"TCP 模式：{settings.mihomo_api}", level="info")
    import os
    p = settings.unix_socket_path
    if os.path.exists(p):
        st = os.stat(p)
        return _r("控制接口", True, f"unix socket {p}（{st.st_mode & 0o777:o}）")
    return _r("控制接口", False, f"socket 文件不存在：{p}",
              "sudo systemctl restart mihomo\n"
              "确认 systemd unit 里有 -ext-ctl-unix 参数")


# ---------------------------------------------------------------- 聚合
@router.get("/health/check")
async def health_check() -> dict[str, Any]:
    results = await asyncio.gather(
        check_mihomo(), check_socket(), check_listeners(),
        check_nft(), check_forwarding(), check_nodes(), check_rules(),
        return_exceptions=True,
    )
    items = []
    for r in results:
        if isinstance(r, Exception):
            items.append(_r("检查项异常", False, repr(r)[:120], ""))
        else:
            items.append(r)

    failed = [i for i in items if not i["ok"] and i["level"] == "error"]
    warned = [i for i in items if not i["ok"] and i["level"] == "warn"]
    if failed:
        verdict, status = f"{len(failed)} 项异常需要处理", "error"
    elif warned:
        verdict, status = f"核心正常，{len(warned)} 项无法在容器内检测", "warn"
    else:
        verdict, status = "全部正常", "ok"

    return {
        "status": status,
        "verdict": verdict,
        "checks": items,
        "passed": sum(1 for i in items if i["ok"]),
        "total": len(items),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health/summary")
async def health_summary() -> dict[str, Any]:
    """轻量版：只返回红/黄/绿，给首页用（不阻塞）。"""
    try:
        checks = await asyncio.gather(
            check_mihomo(), check_nodes(), return_exceptions=True)
    except Exception:  # noqa: BLE001
        return {"status": "error", "verdict": "体检失败"}
    failed = [c for c in checks if isinstance(c, dict) and not c["ok"] and c["level"] == "error"]
    return {
        "status": "error" if failed else "ok",
        "verdict": failed[0]["detail"] if failed else "运行正常",
    }
