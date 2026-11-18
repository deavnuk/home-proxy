"""把 mihomo 的原始 API 数据整理成 UI 友好的结构。

设计原则：
  · 前端只吃这里的形状，不直接碰 mihomo 原始 JSON（版本差异隔离在这层）
  · 每个端点都尽力降级：单个上游失败不影响其它区块渲染
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from . import mihomo
from .settings import settings
from . import nft
from . import config_editor
from . import health

router = APIRouter(prefix="/api")

# 默认测速 URL（与 config.yaml 里 url-test 一致）
TEST_URL = "https://www.gstatic.com/generate_204"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_bytes(n: float | int | None) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}" if i else f"{int(v)} B"


def _human_duration(sec: float | int | None) -> str:
    if not sec:
        return "-"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


# ---------------------------------------------------------------- 概览
@router.get("/overview")
async def overview() -> dict[str, Any]:
    """首页仪表盘：版本、模式、流量、内存、延迟、透明代理状态。"""
    out: dict[str, Any] = {"server_time": _now()}

    ver, cfg, conn = await asyncio.gather(
        mihomo.get("/version"),
        mihomo.get("/configs"),
        mihomo.get("/connections"),
        return_exceptions=True,
    )

    def pick(v: Any, default: Any = None) -> Any:
        return default if isinstance(v, Exception) else v

    v = pick(ver, {})
    out["version"] = v.get("version", "unknown")
    out["meta"] = v.get("meta", False)

    c = pick(cfg, {})
    out["mode"] = c.get("mode", "rule")
    out["log_level"] = c.get("log-level", "info")
    out["ipv6"] = c.get("ipv6", False)
    out["tcp_concurrent"] = c.get("tcp-concurrent", False)
    out["allow_lan"] = c.get("allow-lan", False)
    out["ports"] = {
        "mixed": c.get("mixed-port", 0),
        "socks": c.get("socks-port", 0),
        "redir": c.get("redir-port", 0),
        "tproxy": c.get("tproxy-port", 0),
    }
    out["tun"] = bool((c.get("tun") or {}).get("enable"))

    k = pick(conn, {})
    up_total, down_total = k.get("uploadTotal", 0), k.get("downloadTotal", 0)
    out["traffic"] = {
        "up": up_total,
        "down": down_total,
        "up_h": _fmt_bytes(up_total),
        "down_h": _fmt_bytes(down_total),
        "total_h": _fmt_bytes((up_total or 0) + (down_total or 0)),
    }
    out["connections"] = len(k.get("connections") or [])

    # 注意：mihomo 的 /memory 是流式端点（永不返回），不能在这里同步拉取。
    # /connections 的响应本身就带 memory 字段，直接用它。
    out["memory"] = {
        "bytes": k.get("memory", 0) or 0,
        "human": _fmt_bytes(k.get("memory", 0) or 0),
    }

    # 透明代理是否真的在生效（nft 是否装了规则）
    out["transparent"] = await nft.status(cfg_mode=out["mode"])
    out["read_only"] = settings.read_only
    return out


# ---------------------------------------------------------------- 代理
_GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}


@router.get("/proxies")
async def proxies() -> dict[str, Any]:
    """代理列表，分为「策略组」和「节点」两部分。

    注意：订阅（proxy-providers）里的节点不会出现在顶层 /proxies 中，
    只存在于 provider 内部。所以这里额外合并 /providers/proxies 的节点，
    否则 UI 上看不到订阅节点。
    """
    raw = await mihomo.get("/proxies")
    items: dict[str, Any] = raw.get("proxies") or {}

    groups, nodes = [], []
    seen: set[str] = set()

    def make_node(name: str, p: dict, **extra) -> dict:
        hist = p.get("history") or []
        delay = hist[-1]["delay"] if hist else None
        return {
            "name": name,
            "type": p.get("type", ""),
            "alive": p.get("alive", True),
            "udp": p.get("udp", False),
            "delay": delay,
            "order": _order_hint(name),
            **extra,
        }

    for name, p in items.items():
        typ = p.get("type", "")
        seen.add(name)
        if typ in _GROUP_TYPES:
            g = make_node(name, p)
            g.update({"now": p.get("now", ""), "all": p.get("all") or [],
                      "testUrl": p.get("testUrl", "")})
            groups.append(g)
        else:
            nodes.append(make_node(name, p))

    # 合并订阅里的节点（顶层 /proxies 不含它们）
    try:
        prov = await mihomo.get("/providers/proxies")
        for pname, prov_data in (prov.get("providers") or {}).items():
            if prov_data.get("vehicleType") == "Compatible":
                continue
            for p in prov_data.get("proxies") or []:
                n = p.get("name")
                if not n or n in seen:
                    continue
                seen.add(n)
                nodes.append(make_node(n, p, provider=pname))
    except mihomo.MihomoError:
        pass  # provider 接口不可用不影响主列表

    groups.sort(key=lambda g: (g["order"], g["name"]))
    nodes.sort(key=lambda n: (n["order"], n["name"]))
    return {"groups": groups, "nodes": nodes, "total": len(seen)}


def _order_hint(name: str) -> int:
    """让 GLOBAL / PROXY / auto 这类关键项排在前面。"""
    priority = {"GLOBAL": 0, "PROXY": 1, "auto": 2, "REJECT": 90, "DIRECT": 91, "COMPATIBLE": 99}
    return priority.get(name, 50)


@router.put("/proxies/{group}")
async def select_proxy(group: str, payload: dict = Body(...)) -> dict[str, Any]:
    _guard_write()
    name = payload.get("name")
    if not name:
        raise HTTPException(400, "缺少 name")
    await mihomo.put(f"/proxies/{group}", {"name": name})
    return {"ok": True, "group": group, "now": name}


@router.get("/delay/group/{group}")
async def group_delay(group: str, url: str = TEST_URL, timeout: int = 5000) -> dict[str, Any]:
    """对一个策略组内所有节点测速。"""
    try:
        res = await mihomo.get(
            f"/group/{group}/delay",
            params={"url": url, "timeout": timeout},
            timeout=(timeout / 1000) + 10,
        )
        return {"delays": res or {}}
    except mihomo.MihomoError as e:
        raise HTTPException(e.status, f"{e.message}: {e.detail}") from e


@router.get("/delay/node/{name}")
async def node_delay(name: str, url: str = TEST_URL, timeout: int = 5000) -> dict[str, Any]:
    """对单个节点测速。

    路径故意用 /delay/node/{name} 而不是 /proxies/{name}/delay：
    后者与 /proxies/{group}/delay 模式相同，会被后注册的路由遮蔽。
    """
    try:
        res = await mihomo.get(
            f"/proxies/{name}/delay",
            params={"url": url, "timeout": timeout},
            timeout=(timeout / 1000) + 10,
        )
        return {"delay": (res or {}).get("delay")}
    except mihomo.MihomoError as e:
        raise HTTPException(e.status, f"{e.message}: {e.detail}") from e


# ---------------------------------------------------------------- 规则
@router.get("/rules")
async def rules() -> dict[str, Any]:
    raw = await mihomo.get("/rules")
    out = []
    for r in raw.get("rules") or []:
        extra = r.get("extra") or {}
        out.append({
            "index": r.get("index"),
            "type": r.get("type"),
            "payload": r.get("payload"),
            "proxy": r.get("proxy"),
            "size": r.get("size", -1),
            "hits": extra.get("hitCount", 0),
            "last_hit": extra.get("hitAt"),
            "disabled": extra.get("disabled", False),
        })
    return {"rules": out, "total": len(out)}


@router.get("/rule-providers")
async def rule_providers() -> dict[str, Any]:
    raw = await mihomo.get("/providers/rules")
    rows = []
    for name, p in (raw.get("providers") or {}).items():
        rows.append({
            "name": name,
            "behavior": p.get("behavior"),
            "format": p.get("format"),
            "count": p.get("ruleCount", 0),
            "vehicle": p.get("vehicleType"),
            "updated_at": p.get("updatedAt"),
        })
    rows.sort(key=lambda r: r["name"])
    return {"providers": rows}


@router.put("/rule-providers/{name}")
async def update_rule_provider(name: str) -> dict[str, Any]:
    """手动刷新一个规则集（立即去远端拉一次）。"""
    _guard_write()
    try:
        await mihomo.put(f"/providers/rules/{name}", timeout=120)
    except mihomo.MihomoError as e:
        raise HTTPException(e.status, f"{e.message}: {e.detail}") from e
    return {"ok": True, "name": name}


# ---------------------------------------------------------------- 连接
@router.get("/connections")
async def connections() -> dict[str, Any]:
    raw = await mihomo.get("/connections")
    rows = []
    for c in raw.get("connections") or []:
        meta = c.get("metadata") or {}
        up, down = c.get("upload", 0), c.get("download", 0)
        rows.append({
            "id": c.get("id"),
            "host": meta.get("host") or meta.get("destinationIP", ""),
            "dest": f"{meta.get('destinationIP', '')}:{meta.get('destinationPort', '')}",
            "source": f"{meta.get('sourceIP', '')}:{meta.get('sourcePort', '')}",
            "network": meta.get("network", ""),
            "type": meta.get("type", ""),
            "process": meta.get("processPath") or meta.get("process", ""),
            "rule": c.get("rule", ""),
            "rule_payload": c.get("rulePayload", ""),
            "chains": c.get("chains") or [],
            "up": up,
            "down": down,
            "up_h": _fmt_bytes(up),
            "down_h": _fmt_bytes(down),
            "start": c.get("start"),
            "duration": _human_duration(_elapsed(c.get("start"))),
        })
    rows.sort(key=lambda r: (r["start"] or ""), reverse=True)
    return {
        "connections": rows,
        "total": len(rows),
        "up_h": _fmt_bytes(raw.get("uploadTotal", 0)),
        "down_h": _fmt_bytes(raw.get("downloadTotal", 0)),
    }


def _elapsed(start: str | None) -> float:
    if not start:
        return 0
    try:
        t = datetime.fromisoformat(start.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds()
    except (ValueError, AttributeError):
        return 0


@router.delete("/connections")
async def close_all_connections() -> dict[str, Any]:
    _guard_write()
    await mihomo.delete("/connections")
    return {"ok": True}


@router.delete("/connections/{cid}")
async def close_connection(cid: str) -> dict[str, Any]:
    _guard_write()
    await mihomo.delete(f"/connections/{cid}")
    return {"ok": True}


# ---------------------------------------------------------------- 配置
@router.get("/settings")
async def get_settings_api() -> dict[str, Any]:
    cfg = await mihomo.get("/configs")
    return {
        "mode": cfg.get("mode"),
        "log_level": cfg.get("log-level"),
        "ipv6": cfg.get("ipv6"),
        "tcp_concurrent": cfg.get("tcp-concurrent"),
        "allow_lan": cfg.get("allow-lan"),
        "unified_delay": cfg.get("unified-delay"),
        "find_process_mode": cfg.get("find-process-mode"),
        "interface_name": cfg.get("interface-name"),
        "ports": {
            "mixed": cfg.get("mixed-port", 0),
            "socks": cfg.get("socks-port", 0),
            "redir": cfg.get("redir-port", 0),
            "tproxy": cfg.get("tproxy-port", 0),
        },
        "tun": cfg.get("tun") or {},
        "read_only": settings.read_only,
    }


_ALLOWED_PATCH = {
    "mode", "log-level", "ipv6", "tcp-concurrent", "allow-lan",
    "unified-delay", "find-process-mode", "interface-name",
    "mixed-port", "socks-port", "redir-port", "tproxy-port",
}


@router.patch("/settings")
async def patch_settings(body: dict = Body(...)) -> dict[str, Any]:
    """运行时改配置（不落盘，重启失效；改文件请用 /config/file）。"""
    _guard_write()
    bad = set(body) - _ALLOWED_PATCH
    if bad:
        raise HTTPException(400, f"不支持的字段: {', '.join(sorted(bad))}")
    if not body:
        raise HTTPException(400, "空请求")
    await mihomo.patch("/configs", body)
    return {"ok": True, "applied": body}


@router.get("/config/file")
async def get_config_file() -> dict[str, Any]:
    """读取 config.yaml 原文（用于 UI 里编辑）。"""
    path = settings.mihomo_config
    if not path.exists():
        return {"path": str(path), "exists": False, "content": "", "writable": False}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"读取失败: {e}") from e
    return {
        "path": str(path),
        "exists": True,
        "content": content,
        "size": len(content),
        "writable": not settings.read_only and _is_writable(path),
    }


def _is_writable(path) -> bool:
    import os

    return os.access(path, os.W_OK)


@router.put("/config/file")
async def put_config_file(body: dict = Body(...)) -> dict[str, Any]:
    """保存 config.yaml 并热重载。会先备份。"""
    _guard_write()
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(400, "content 不能为空")

    # 基本校验：必须是能解析的 YAML 且含关键字段，避免把代理写死
    err = _validate_yaml_shape(content)
    if err:
        raise HTTPException(400, f"配置校验失败：{err}")

    path = settings.mihomo_config
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # 备份必须写到「确实可写」的地方：
    #   容器里常以 bind mount 挂单个文件，同目录往往属 root 不可写，
    #   所以优先尝试同目录，失败则退到 MIHOMO_BACKUP_DIR / 系统临时目录。
    backup_dir = _pick_backup_dir(path)
    backup = backup_dir / f"{path.name}.bak.{ts}" if backup_dir else None
    try:
        if path.exists() and backup:
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(content, encoding="utf-8")
    except PermissionError as e:
        raise HTTPException(403, f"没有写入权限：{e}。请在 compose 里用 `user: \"$(id -u):$(id -g)\"` "
                                f"或把配置目录 chown 给容器用户。") from e
    except OSError as e:
        raise HTTPException(500, f"写入失败: {e}") from e

    # 热重载（mihomo 的 PUT /configs 需要 path 参数指向配置文件）
    reload_ok, reload_msg = await _reload(str(path))
    return {
        "ok": reload_ok,
        "reload": reload_msg,
        "backup": str(backup) if backup and backup.exists() else None,
    }


def _pick_backup_dir(path) -> "Path | None":
    """找一个能写的目录放备份。"""
    import os
    import tempfile

    candidates = []
    if os.getenv("MIHOMO_BACKUP_DIR"):
        candidates.append(Path(os.environ["MIHOMO_BACKUP_DIR"]))
    candidates.append(path.parent)
    candidates.append(Path(tempfile.gettempdir()))

    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".ui_write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return d
        except OSError:
            continue
    return None


@router.post("/config/reload")
async def reload_config() -> dict[str, Any]:
    _guard_write()
    ok, msg = await _reload(str(settings.mihomo_config))
    if not ok:
        raise HTTPException(502, msg)
    return {"ok": True, "message": msg}


async def _reload(path: str) -> tuple[bool, str]:
    try:
        await mihomo.put(
            "/configs",
            {"path": path},
            params={"force": "true"},
            timeout=30,
        )
        return True, "已重载"
    except mihomo.MihomoError as e:
        return False, f"{e.message}: {e.detail}"


def _validate_yaml_shape(text: str) -> str | None:
    """极简结构校验（不引入 pyyaml 依赖）。"""
    try:
        import yaml  # type: ignore
    except ImportError:
        # 没有 pyyaml 就只做粗检
        for need in ("proxies:", "proxy-groups:", "rules:"):
            if need not in text:
                return f"缺少 `{need}` 段"
        return None
    try:
        doc = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        return f"YAML 语法错误: {e}"
    if not isinstance(doc, dict):
        return "根节点必须是映射"
    for need in ("proxies", "proxy-groups", "rules"):
        if need not in doc:
            return f"缺少 `{need}` 段"
    for need in ("proxies", "proxy-groups", "rules"):
        if not isinstance(doc.get(need), list):
            return f"`{need}` 必须是列表"
    return None


# ---------------------------------------------------------------- 订阅管理（写入 config.yaml）


@router.get("/subscriptions")
async def list_subscriptions() -> dict[str, Any]:
    """列出配置里的订阅（合并磁盘配置和 mihomo 运行状态）。

    磁盘配置给「填的是什么」（URL、间隔），
    mihomo 状态给「跑得怎么样」（节点数、存活数、更新时间）。
    """
    path = settings.mihomo_config
    if not path.exists():
        raise HTTPException(404, f"配置文件不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"读取失败：{e}") from e

    configured = {s["name"]: s for s in config_editor.parse_subscriptions(text)}

    # mihomo 运行状态
    runtime: dict[str, dict] = {}
    try:
        raw = await mihomo.get("/providers/proxies")
        for name, p in (raw.get("providers") or {}).items():
            if p.get("vehicleType") == "Compatible":
                continue
            proxies = p.get("proxies") or []
            runtime[name] = {
                "count": len(proxies),
                "alive": sum(1 for x in proxies if x.get("alive")),
                "updated_at": p.get("updatedAt"),
                "info": p.get("subscriptionInfo") or {},
            }
    except mihomo.MihomoError:
        pass

    rows = []
    for name, c in configured.items():
        r = runtime.get(name)
        rows.append({
            **c,
            "url": config_editor.mask_url(c["url"]),
            "url_set": bool(c["url"]),
            "count": r["count"] if r else 0,
            "alive": r["alive"] if r else 0,
            "updated_at": r["updated_at"] if r else None,
            "info": r["info"] if r else {},
            "loaded": r is not None,
        })
    rows.sort(key=lambda x: x["name"])
    return {"subscriptions": rows, "total": len(rows), "read_only": settings.read_only}


@router.post("/subscriptions")
async def create_subscription(body: dict = Body(...)) -> dict[str, Any]:
    """新增订阅。body: {name, url, interval?, proxy?, health_check_url?, ...}"""
    _guard_write()
    name = (body.get("name") or "").strip()
    url = (body.get("url") or "").strip()
    if not name:
        raise HTTPException(400, "缺少 name")
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise HTTPException(400, "name 只能包含字母数字、下划线、点、连字符")
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "url 必须以 http:// 或 https:// 开头")

    text = _read_config()
    if any(s["name"] == name for s in config_editor.parse_subscriptions(text)):
        raise HTTPException(409, f"订阅 {name} 已存在")

    sub = _sub_from_body(name, url, body)
    new_text = config_editor.sync_group_use(config_editor.upsert_subscription(text, sub))
    return await _commit(new_text)


@router.put("/subscriptions/{name}")
async def update_subscription(name: str, body: dict = Body(...)) -> dict[str, Any]:
    """修改订阅（只传要改的字段）。"""
    _guard_write()
    text = _read_config()
    existing = {s["name"]: s for s in config_editor.parse_subscriptions(text)}
    if name not in existing:
        raise HTTPException(404, f"订阅 {name} 不存在")

    cur = existing[name]
    merged = {**cur}
    for k in ("url", "interval", "proxy", "filter", "exclude_filter", "size_limit", "enabled"):
        if k in body:
            merged[k] = body[k]
    if "health_check_url" in body:
        merged["health_check"] = {**cur.get("health_check", {}), "url": body["health_check_url"]}
    if "health_check_interval" in body:
        merged["health_check"] = {**cur.get("health_check", {}),
                                "interval": int(body["health_check_interval"])}
    if not re.match(r"^https?://", merged.get("url") or ""):
        raise HTTPException(400, "url 必须以 http:// 或 https:// 开头")

    new_text = config_editor.sync_group_use(config_editor.upsert_subscription(text, merged))
    return await _commit(new_text)


@router.delete("/subscriptions/{name}")
async def delete_subscription(name: str) -> dict[str, Any]:
    _guard_write()
    text = _read_config()
    new_text, ok = config_editor.remove_subscription(text, name)
    if not ok:
        raise HTTPException(404, f"订阅 {name} 不存在")
    return await _commit(config_editor.sync_group_use(new_text))


@router.post("/subscriptions/{name}/reload")
async def reload_subscription(name: str) -> dict[str, Any]:
    """立即拉取订阅内容（不重载整个配置）。"""
    _guard_write()
    try:
        await mihomo.put(f"/providers/proxies/{name}", timeout=120)
    except mihomo.MihomoError as e:
        raise HTTPException(e.status, f"{e.message}: {e.detail}") from e
    return {"ok": True, "name": name}


def _sub_from_body(name: str, url: str, body: dict) -> dict[str, Any]:
    return {
        "name": name,
        "url": url,
        "type": "http",
        "interval": int(body.get("interval", 21600)),
        "enabled": bool(body.get("enabled", True)),
        "path": body.get("path") or f"./providers/{name}.yaml",
        "proxy": body.get("proxy", "PROXY"),
        "filter": body.get("filter", ""),
        "exclude_filter": body.get("exclude_filter", ""),
        "size_limit": int(body.get("size_limit", 0) or 0),
        "health_check": {
            "enable": True,
            "interval": int(body.get("health_check_interval", 300)),
            "url": body.get("health_check_url") or "https://www.gstatic.com/generate_204",
        },
    }


def _read_config() -> str:
    path = settings.mihomo_config
    if not path.exists():
        raise HTTPException(404, f"配置文件不存在：{path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"读取失败：{e}") from e


async def _commit(new_text: str) -> dict[str, Any]:
    """校验 -> 备份 -> 写入 -> 热重载。"""
    err = config_editor.validate(new_text)
    if err:
        raise HTTPException(400, f"配置校验失败：{err}")

    path = settings.mihomo_config
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = _pick_backup_dir(path)
    backup = backup_dir / f"{path.name}.bak.{ts}" if backup_dir else None
    try:
        if path.exists() and backup:
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(new_text, encoding="utf-8")
    except PermissionError as e:
        raise HTTPException(403, f"没有写入权限：{e}") from e
    except OSError as e:
        raise HTTPException(500, f"写入失败：{e}") from e

    ok, msg = await _reload(str(path))
    return {"ok": ok, "reload": msg,
            "backup": str(backup) if backup and backup.exists() else None}


# ---------------------------------------------------------------- 节点订阅
@router.get("/proxy-providers")
async def proxy_providers() -> dict[str, Any]:
    """列出节点订阅（proxy-providers）及其状态。

    mihomo 的 /providers/proxies 返回两类：
      · vehicleType=Compatible -> 内置策略组（PROXY/auto/...）
      · vehicleType=HTTP/File  -> 真正的订阅来源
    这里只挑出后者。
    """
    try:
        raw = await mihomo.get("/providers/proxies")
    except mihomo.MihomoError as e:
        raise HTTPException(e.status, f"{e.message}: {e.detail}") from e

    rows = []
    for name, p in (raw.get("providers") or {}).items():
        vehicle = p.get("vehicleType", "")
        if vehicle == "Compatible":
            continue  # 内置组，不是订阅
        proxies = p.get("proxies") or []
        alive = sum(1 for x in proxies if x.get("alive"))
        rows.append({
            "name": name,
            "vehicle": vehicle,
            "count": len(proxies),
            "alive": alive,
            "updated_at": p.get("updatedAt"),
            "subscription_info": p.get("subscriptionInfo") or {},
        })
    rows.sort(key=lambda r: r["name"])
    return {"providers": rows, "total": len(rows)}


@router.put("/proxy-providers/{name}")
async def update_proxy_provider(name: str) -> dict[str, Any]:
    """手动刷新订阅（立即重新拉取 URL）。"""
    _guard_write()
    try:
        await mihomo.put(f"/providers/proxies/{name}", timeout=120)
    except mihomo.MihomoError as e:
        raise HTTPException(e.status, f"{e.message}: {e.detail}") from e
    return {"ok": True, "name": name}


@router.get("/proxy-providers/{name}/healthcheck")
async def proxy_provider_healthcheck(name: str) -> dict[str, Any]:
    """触发订阅内所有节点的健康检查。"""
    try:
        await mihomo.get(f"/providers/proxies/{name}/healthcheck", timeout=90)
    except mihomo.MihomoError as e:
        raise HTTPException(e.status, f"{e.message}: {e.detail}") from e
    return {"ok": True}


# ---------------------------------------------------------------- 高级配置表单
@router.get("/advanced")
async def get_advanced() -> dict[str, Any]:
    """读取高级表单字段 + 字段定义（含中文标签与说明）。"""
    path = settings.mihomo_config
    if not path.exists():
        raise HTTPException(404, f"找不到配置文件：{path}")
    text = path.read_text(encoding="utf-8")
    return {
        "fields": config_editor.advanced_fields(),
        "values": config_editor.parse_advanced(text),
        "read_only": settings.read_only,
    }


@router.put("/advanced")
async def put_advanced(body: dict = Body(...)) -> dict[str, Any]:
    """保存高级表单。只改表单覆盖的字段，其余原样保留。"""
    _guard_write()
    path = settings.mihomo_config
    text = path.read_text(encoding="utf-8")
    new_text, err = config_editor.write_advanced(text, body)
    if err:
        raise HTTPException(400, err)
    ok, msg = await _save_and_reload(path, new_text)
    return {"ok": ok, "reload": msg}


# ---------------------------------------------------------------- 网站分流探测
@router.get("/probe")
async def probe(domain: str) -> dict[str, Any]:
    """查一个域名会怎么走：走代理 / 直连 / 拦截。

    实现思路（三个方案都试过，这是唯一在容器里可靠的）：
      ✗ mihomo /dns/query API —— 走真实解析，不过 fake-ip，google 也返回真实 IP
      ✗ 直接查 mihomo DNS 端口 —— 容器被宿主 ufw 拦住，连不上
      ✓ 直接读规则集文件做匹配 —— 规则集就挂在容器里，且这正是分流的真实依据

    匹配顺序与 config.yaml 的 rules 段一致：
      我的规则 > direct(直连清单) > gfw(被墙清单) > 其余直连
    """
    d, _rtype, err = config_editor.normalize_domain(domain)
    if err:
        raise HTTPException(400, err)

    # 1) 用户自定义规则优先
    cfg_text = ""
    path = settings.mihomo_config
    if path.exists():
        cfg_text = path.read_text(encoding="utf-8")
        for r in config_editor.parse_my_rules(cfg_text):
            if _domain_match(d, r["domain"]):
                return {"domain": d, "verdict": r["action"], "ip": None,
                        "reason": f"你为 {r['domain']} 单独指定了「{_ACTION_CN.get(r['action'], r['action'])}」"}

    prov_dir = path.parent / "providers"

    # 2) direct 清单 -> 直连
    if _in_ruleset(prov_dir / "direct.yaml", d):
        return {"domain": d, "verdict": "DIRECT", "ip": None,
                "reason": "在「已知可直连」清单里，直接连接"}

    # 3) gfw / gfw-extra -> 代理
    for f in ("gfw.yaml", "gfw-extra.txt", "gfwlist-extra.yaml"):
        if _in_ruleset(prov_dir / f, d):
            return {"domain": d, "verdict": "PROXY", "ip": None,
                    "reason": "在 gfwlist（被墙网站清单）里，会走代理"}

    # 4) 兜底直连
    return {"domain": d, "verdict": "DIRECT", "ip": None,
            "reason": "不在任何清单里，按默认策略直接连接"}


_ACTION_CN = {"DIRECT": "直连", "PROXY": "走代理", "REJECT": "拦截"}


def _domain_match(domain: str, rule_domain: str) -> bool:
    """规则域名是否匹配目标（含子域）。"""
    return domain == rule_domain or domain.endswith("." + rule_domain)


def _in_ruleset(file, domain: str) -> bool:
    """在规则集文件里查域名。支持 yaml(payload: - '+.x.com') 与纯文本两种格式。"""
    from pathlib import Path as _P
    f = _P(file)
    if not f.exists():
        return False
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "payload:")):
            continue
        if line.startswith("- "):
            line = line[2:].strip().strip("'\"")
        if not line:
            continue
        line = line.lstrip("+*.").lower()
        if line and (domain == line or domain.endswith("." + line)):
            return True
    return False


# ---------------------------------------------------------------- 我的规则
@router.get("/my-rules")
async def my_rules() -> dict[str, Any]:
    path = settings.mihomo_config
    if not path.exists():
        raise HTTPException(404, "找不到配置文件")
    text = path.read_text(encoding="utf-8")
    return {"rules": config_editor.parse_my_rules(text)}


@router.post("/my-rules")
async def add_my_rule(body: dict = Body(...)) -> dict[str, Any]:
    _guard_write()
    raw = (body.get("domain") or "").strip()
    action = (body.get("action") or "PROXY").upper()
    d, rtype, err = config_editor.normalize_domain(raw)
    if err:
        raise HTTPException(400, err)
    if action not in ("DIRECT", "PROXY", "REJECT"):
        raise HTTPException(400, "action 只能是 DIRECT / PROXY / REJECT")

    path = settings.mihomo_config
    text = path.read_text(encoding="utf-8")
    new_text, err = config_editor.add_my_rule(text, rtype, d, action)
    if err:
        raise HTTPException(400, err)

    ok, msg = await _save_and_reload(path, new_text)
    return {"ok": ok, "reload": msg, "domain": d, "action": action}


@router.delete("/my-rules/{domain}")
async def del_my_rule(domain: str) -> dict[str, Any]:
    _guard_write()
    path = settings.mihomo_config
    text = path.read_text(encoding="utf-8")
    new_text, removed = config_editor.remove_my_rule(text, domain)
    if not removed:
        raise HTTPException(404, f"没有这条规则：{domain}")
    ok, msg = await _save_and_reload(path, new_text)
    return {"ok": ok, "reload": msg}


async def _save_and_reload(path, new_text: str) -> tuple[bool, str]:
    """写盘 + 热重载，带备份。"""
    err = config_editor.validate(new_text)
    if err:
        raise HTTPException(400, f"配置校验失败：{err}")

    backup_dir = _pick_backup_dir(path)
    if backup_dir:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            (backup_dir / f"{path.name}.bak.{ts}").write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    try:
        path.write_text(new_text, encoding="utf-8")
    except PermissionError as e:
        raise HTTPException(403, f"没有写入权限：{e}") from e
    return await _reload(str(path))


# ---------------------------------------------------------------- 工具
def _guard_write() -> None:
    if settings.read_only:
        raise HTTPException(403, "UI 处于只读模式（UI_READ_ONLY=1）")


@router.get("/ping")
async def ping() -> dict[str, Any]:
    """给前端做连通性/鉴权自检。"""
    try:
        v = await mihomo.get("/version", timeout=3)
        return {"ok": True, "mihomo": v.get("version"), "auth": settings.auth_enabled,
                "read_only": settings.read_only}
    except mihomo.MihomoError as e:
        return {"ok": False, "error": e.message, "detail": e.detail}
