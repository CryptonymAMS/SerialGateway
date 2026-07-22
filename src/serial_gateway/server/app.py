"""FastAPI 应用:REST 控制面 + WebSocket 多客户端 fan-out + 内嵌 WebUI + MCP(挂载)。

MCP 的 session_manager 必须在 host(FastAPI)的 lifespan 中 run(),
否则 mount 的 sub-app lifespan 不会被触发。
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .. import ports as ports_mod
from ..config import Config, SerialConfig
from ..profile import ProfileStore
from ..session import ConfigConflictError, SessionManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(cfg: Config) -> FastAPI:
    manager = SessionManager(ring_cap=cfg.ring_cap)
    profiles = ProfileStore(os.path.join(cfg.data_dir, "profiles"))

    # MCP(可选):提前构建,拿到 session_manager 以便在 lifespan 中初始化。
    mcp_app = None
    session_manager = None
    mcp_error = None
    try:
        from . import mcp_server

        mcp_app, session_manager = mcp_server.build(manager)
    except Exception as e:  # MCP 不可用时降级
        mcp_error = str(e)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if session_manager is not None:
            async with session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="serial-gateway", lifespan=lifespan)
    app.state.manager = manager
    app.state.profiles = profiles
    app.state.cfg = cfg

    # ---- WebUI(单文件 HTML) ----
    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    # ---- REST 控制面 ----
    @app.get("/api/ports")
    async def get_ports():
        return ports_mod.list_available_ports()

    @app.get("/api/profiles")
    async def list_profiles():
        return await app.state.profiles.list()

    @app.post("/api/profiles")
    async def save_profile(profile: dict):
        await app.state.profiles.save(profile)
        return JSONResponse(status_code=204, content=None)

    @app.delete("/api/profiles/{pid}")
    async def delete_profile(pid: str):
        await app.state.profiles.delete(pid)
        return JSONResponse(status_code=204, content=None)

    @app.post("/api/sessions")
    async def open_session(req: dict):
        name = req["port"]
        sc = SerialConfig.from_dict(req.get("config"))
        try:
            await app.state.manager.open_or_get(name, sc)
        except ConfigConflictError as ce:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "config_conflict",
                    "message": f"端口 {name} 已被以其他配置打开(先来后到)",
                    "current_config": ce.current_config.to_dict(),
                },
            )
        return {"session_id": name}

    @app.delete("/api/sessions/{sid}")
    async def close_session(sid: str):
        await app.state.manager.close(sid)
        return JSONResponse(status_code=204, content=None)

    @app.post("/api/sessions/{sid}/configure")
    async def configure_session(sid: str, body: dict):
        s = app.state.manager.get(sid)
        if not s:
            raise HTTPException(404, "session not found")
        await s.configure(SerialConfig.from_dict(body))
        return JSONResponse(status_code=204, content=None)

    @app.get("/api/sessions/{sid}/log")
    async def recent_log(sid: str, count: int = 100, secs: int = 0):
        s = app.state.manager.get(sid)
        if not s:
            raise HTTPException(404, "session not found")
        entries = s.recent_log(count if not secs else 100000)
        if secs:
            now = time.time()
            entries = [e for e in entries if e.ts >= now - secs]
        return [e.to_dict() for e in entries]

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        out: asyncio.Queue = asyncio.Queue()
        sender = asyncio.create_task(_ws_sender(ws, out))
        subs: dict[str, tuple[str, asyncio.Task]] = {}
        try:
            while True:
                msg = await ws.receive_json()
                await _handle_ws_msg(app, msg, out, subs)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            try:
                await out.put({"type": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            sender.cancel()
            affected = set()
            for cid, (name, task) in list(subs.items()):
                task.cancel()
                s = app.state.manager.get(name)
                if s:
                    await s.unsubscribe(cid)
                    affected.add(name)
            # 仅当某端口的所有订阅者都离开后,才关闭 session(释放串口)
            for port in affected:
                s = app.state.manager.get(port)
                if s and len(s.subscriber_ids) == 0:
                    await app.state.manager.close(port)

    # ---- MCP 挂载(FastMCP 内部 route 为 /mcp,挂根) ----
    if mcp_app is not None:
        app.mount("/", mcp_app)
    else:
        @app.get("/mcp")
        async def mcp_unavailable():
            return JSONResponse(
                status_code=503, content={"error": f"MCP unavailable: {mcp_error}"}
            )

    return app


async def _ws_sender(ws: WebSocket, out: asyncio.Queue):
    try:
        while True:
            msg = await out.get()
            await ws.send_json(msg)
    except Exception:
        pass


async def _handle_ws_msg(app: FastAPI, msg: dict, out: asyncio.Queue, subs: dict):
    t = msg.get("type")
    if t == "subscribe":
        port = msg["port"]
        client = msg["client"]
        sc = SerialConfig.from_dict(msg.get("config"))
        try:
            s = await app.state.manager.open_or_get(port, sc)
        except ConfigConflictError as ce:
            # 先来后到:已有客户端以其他配置打开,告知前端当前配置让其沿用
            await out.put(
                {
                    "type": "config_conflict",
                    "port": port,
                    "current_config": ce.current_config.to_dict(),
                }
            )
            return
        if client in subs:
            old_name, old_task = subs.pop(client)
            old_task.cancel()
            olds = app.state.manager.get(old_name)
            if olds:
                await olds.unsubscribe(client)
        try:
            q = await s.subscribe(client)
        except ValueError:
            await s.unsubscribe(client)
            q = await s.subscribe(client)

        async def fwd():
            try:
                while True:
                    entry = await q.get()
                    if entry is None:  # 哨兵:session 已关闭
                        await out.put({"type": "session_closed", "port": port})
                        break
                    await out.put(
                        {
                            "type": "data",
                            "port": port,
                            "dir": entry.dir,
                            "source": entry.source,
                            "ts": int(entry.ts * 1000),
                            "data_b64": base64.b64encode(entry.data).decode("ascii"),
                        }
                    )
            except Exception:
                pass

        subs[client] = (port, asyncio.create_task(fwd()))
        await out.put({"type": "subscribed", "port": port})

    elif t == "write":
        port = msg["port"]
        client = msg["client"]
        data = base64.b64decode(msg.get("data_b64", ""))
        s = app.state.manager.get(port)
        if not s:
            await out.put({"type": "error", "message": "session not open"})
            return
        await s.write(data, client)
        await out.put({"type": "written", "port": port})

    elif t == "unsubscribe":
        port = msg["port"]
        client = msg["client"]
        s = app.state.manager.get(port)
        if s:
            await s.unsubscribe(client)
            # 仅当该端口无任何订阅者时关闭 session(释放串口)
            if len(s.subscriber_ids) == 0:
                await app.state.manager.close(port)
        if client in subs:
            _, task = subs.pop(client)
            task.cancel()
        await out.put({"type": "unsubscribed", "port": port})
