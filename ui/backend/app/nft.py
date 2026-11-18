"""透明代理状态探测。

UI 需要回答一个关键问题：**分流到底有没有在生效？**
光看 mihomo 活着不够——nft 规则没装、策略路由没配，流量根本到不了它。

这里只读，不改规则。所有失败都降级为 available=False，不阻塞其它面板。
"""
from __future__ import annotations

import asyncio
import re
import shutil
from typing import Any

from fastapi import APIRouter

from .settings import settings

router = APIRouter(prefix="/api")


async def _run(cmd: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except (asyncio.TimeoutError, FileNotFoundError, PermissionError, OSError):
        return 127, "", "unavailable"


_COUNTER = re.compile(r"counter packets (\d+) bytes (\d+)")
_TPROXY = re.compile(r"tproxy ip to :(\d+)")


async def status(cfg_mode: str | None = None) -> dict[str, Any]:
    """探测 nft 规则 / 策略路由 / 端口监听。"""
    if not shutil.which("nft"):
        return {
            "available": False,
            "reason": "UI 容器内没有 nft（这是常态：网络规则装在宿主机上）",
            "hints": [
                "透明代理规则需要装在宿主机，而不是 UI 容器里。",
                "在宿主机执行：nft list table inet transp",
                "或查看 README 的『验证』一节。",
            ],
        }

    table = settings.nft_table
    rc, out, err = await _run(["nft", "list", "table", *table.split()])
    if rc != 0 or not out.strip():
        return {
            "available": False,
            "reason": "宿主机上没有加载 nft 规则表",
            "table": table,
            "hints": [
                "在宿主机上运行：sudo nft -f net/nftables-transp.conf",
                "或 sudo systemctl start mihomo-nftables",
                f"排查：{err.strip()[:200]}" if err.strip() else "",
            ],
        }

    tproxy_hits = _initial_tproxy_hits(out)
    rule_count = out.count("chain ")
    chains = re.findall(r"chain (\S+) \{", out)

    # 策略路由：TPROXY 必需 fwmark -> local 表
    prc, pout, _ = await _run(["ip", "rule", "show"])
    policy_ok = "fwmark 0x1 lookup 100" in pout

    rrc, rout, _ = await _run(["ip", "route", "show", "table", "100"])
    local_ok = "local" in rout and "default" in rout

    return {
        "available": True,
        "table": table,
        "chains": chains,
        "rule_count": rule_count,
        "tproxy_port": tproxy_hits,
        "policy_route": {"rule": policy_ok, "local_route": local_ok},
        "healthy": bool(tproxy_hits) or (policy_ok and local_ok),
        "hints": [] if (policy_ok and local_ok) else [
            "策略路由缺失，TPROXY 收不到包：",
            "sudo ip rule add fwmark 0x1 lookup 100",
            "sudo ip route add local 0.0.0.0/0 dev lo table 100",
        ],
    }


def _initial_tproxy_hits(text: str) -> int | None:
    m = _TPROXY.search(text)
    return int(m.group(1)) if m else None


@router.get("/transparent")
async def transparent() -> dict[str, Any]:
    return await status()
