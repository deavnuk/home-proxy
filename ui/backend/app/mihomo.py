"""mihomo RESTful API 的薄封装。

只做三件事：拼 URL、带 secret、把上游错误翻译成可读中文。
所有 API 版本差异都收敛在这里，UI 层不用关心。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .settings import settings


class MihomoError(Exception):
    """上游 mihomo API 返回了错误，或连不上。"""

    def __init__(self, message: str, status: int = 502, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if settings.mihomo_secret:
        h["Authorization"] = f"Bearer {settings.mihomo_secret}"
    return h


# mihomo 的这些端点是「无限流」，普通 GET 会永久挂起（每帧都会重置读超时）。
# 需要时请用 WebSocket 或改用 /connections 里的内嵌字段。
STREAMING_ENDPOINTS = frozenset({"/traffic", "/memory", "/logs"})


def _guard_streaming(path: str) -> None:
    base = path.split("?", 1)[0].rstrip("/")
    if base in STREAMING_ENDPOINTS:
        raise MihomoError(
            f"{base} 是流式端点，不能用普通 GET",
            500,
            "请改用 WebSocket，或读取 /connections 响应里的内嵌字段。",
        )


def _translate(exc: Exception) -> MihomoError:
    if isinstance(exc, httpx.ConnectError):
        target = (
            f"unix socket {settings.unix_socket_path}"
            if settings.is_unix_socket
            else settings.mihomo_api
        )
        return MihomoError(
            "连不上 mihomo 控制接口",
            502,
            f"目标 {target}。请确认 mihomo 已启动；"
            + (
                "unix socket 模式需要把 socket 文件挂进容器，"
                "且容器用户对 socket 有读写权限。"
                if settings.is_unix_socket
                else "external-controller 未绑定到 127.0.0.1（容器内需 0.0.0.0）。"
            ),
        )
    if isinstance(exc, httpx.TimeoutException):
        return MihomoError(f"mihomo 接口超时（{settings.timeout}s）", 504, str(exc))
    return MihomoError("mihomo 接口异常", 502, str(exc))


async def request(method: str, path: str, **kwargs: Any) -> Any:
    if method.upper() == "GET":
        _guard_streaming(path)

    kwargs.setdefault("timeout", settings.timeout)

    # unix socket 模式：base_url 用一个假域名，httpx 走 uds 传输
    if settings.is_unix_socket:
        url = f"http://localhost{path}"
        transport = httpx.AsyncHTTPTransport(uds=settings.unix_socket_path)
    else:
        url = f"{settings.mihomo_api.rstrip('/')}{path}"
        transport = None

    try:
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.request(method, url, headers=_headers(), **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc

    if resp.status_code == 401:
        raise MihomoError("mihomo 鉴权失败", 401, "UI 的 MIHOMO_SECRET 与 config.yaml 的 secret 不一致。")
    if resp.status_code >= 400:
        raise MihomoError(f"mihomo 返回 {resp.status_code}", 502, resp.text[:500])

    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        return resp.text


async def get(path: str, **kw: Any) -> Any:
    return await request("GET", path, **kw)


async def put(path: str, payload: Any | None = None, **kw: Any) -> Any:
    return await request("PUT", path, json=payload, **kw)


async def patch(path: str, payload: Any, **kw: Any) -> Any:
    return await request("PATCH", path, json=payload, **kw)


async def delete(path: str, **kw: Any) -> Any:
    return await request("DELETE", path, **kw)
