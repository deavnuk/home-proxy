"""FastAPI 应用入口。

鉴权用 HTTP Basic（浏览器原生弹窗，零 JS 依赖）。
实时日志走 WebSocket（mihomo 的 /logs 是流式端点，普通 GET 会永久挂起）。
"""
from __future__ import annotations

import asyncio
import base64
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from . import api, health, mihomo, nft
from .settings import settings

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
    title="Home Proxy UI",
    version="2.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


# ---------------------------------------------------------------- 鉴权
def _check_basic(auth_header: str | None) -> bool:
    if not settings.auth_enabled:
        return True
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
    except Exception:  # noqa: BLE001
        return False
    user, _, pwd = raw.partition(":")
    return (secrets.compare_digest(user, settings.ui_user)
            and secrets.compare_digest(pwd, settings.ui_pass))


async def require_auth(request: Request) -> None:
    if not settings.auth_enabled:
        return
    if not _check_basic(request.headers.get("authorization")):
        raise HTTPException(
            status_code=401,
            detail="需要登录",
            headers={"WWW-Authenticate": 'Basic realm="Home Proxy UI"'},
        )


# ---------------------------------------------------------------- 错误处理
@app.exception_handler(mihomo.MihomoError)
async def mihomo_error_handler(_: Request, exc: mihomo.MihomoError) -> JSONResponse:
    return JSONResponse(status_code=exc.status,
                        content={"error": exc.message, "detail": exc.detail})


# ---------------------------------------------------------------- 路由
app.include_router(api.router, dependencies=[Depends(require_auth)])
app.include_router(nft.router, dependencies=[Depends(require_auth)])
app.include_router(health.router, dependencies=[Depends(require_auth)])


@app.get("/api/health")
async def health_probe() -> dict:
    """容器健康检查用（不走鉴权，Dockerfile HEALTHCHECK 依赖它）。"""
    return {"ok": True, "auth": settings.auth_enabled, "read_only": settings.read_only}


# ---------------------------------------------------------------- 实时日志
_WS_BLACKLIST = {"/traffic", "/memory"}


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket, level: str = "info") -> None:
    """把浏览器的 WS 桥接到 mihomo 的 /logs 流。"""
    await ws.accept()

    # WS 不能自定义 header，用 ?token=<base64(user:pass)> 代替
    if settings.auth_enabled:
        token = ws.query_params.get("token", "")
        ok = False
        try:
            u, _, p = base64.b64decode(token).decode("utf-8").partition(":")
            ok = (secrets.compare_digest(u, settings.ui_user)
                  and secrets.compare_digest(p, settings.ui_pass))
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            await ws.close(code=4401)
            return

    try:
        import websockets  # type: ignore
    except ImportError:
        await ws.send_text('{"type":"error","payload":"服务端缺 websockets 依赖"}')
        await ws.close(code=1011)
        return

    if settings.is_unix_socket:
        # websockets 库支持 unix socket
        target = f"ws://localhost/logs?level={level}"
        connect_kw = {"uds": settings.unix_socket_path}
    else:
        base = settings.mihomo_api.replace("http://", "ws://").replace("https://", "wss://")
        target = f"{base.rstrip('/')}/logs?level={level}"
        connect_kw = {}

    headers = {}
    if settings.mihomo_secret:
        headers["Authorization"] = f"Bearer {settings.mihomo_secret}"

    try:
        async with websockets.connect(
            target, additional_headers=headers, open_timeout=6, **connect_kw
        ) as upstream:
            async def up_to_client() -> None:
                async for msg in upstream:
                    if ws.client_state != WebSocketState.CONNECTED:
                        return
                    await ws.send_text(msg if isinstance(msg, str) else msg.decode("utf-8", "replace"))

            async def client_to_up() -> None:
                # 前端只发心跳/过滤指令，转发给 mihomo（它不认也无妨）
                while True:
                    await ws.receive_text()

            tasks = [asyncio.create_task(up_to_client()),
                     asyncio.create_task(client_to_up())]
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_text(f'{{"type":"error","payload":"{type(e).__name__}: {e}"}}')
            except Exception:  # noqa: BLE001
                pass
            await ws.close(code=1011)


# ---------------------------------------------------------------- 静态前端
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
