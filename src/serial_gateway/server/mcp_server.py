"""MCP Streamable HTTP 服务端(挂到 FastAPI 的 /mcp)。

工具融合 serial2mcp 的接口(configure_connection / send_data+wait_policy / read_urc)
与本项目的多客户端能力(transact / get_recent_log),复用同一套串口引擎。
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from ..config import SerialConfig


def make_mcp(manager) -> FastMCP:
    mcp = FastMCP("serial-gateway")
    # 放开 DNS rebinding 保护,允许局域网 IP 直接访问 MCP(调试工具,非生产)
    mcp.settings.transport_security.enable_dns_rebinding_protection = False

    @mcp.tool()
    def list_ports() -> str:
        """列出主机可用串口(含 USB vid/pid/serial/manufacturer/product)。"""
        from .. import ports as ports_mod

        return json.dumps(ports_mod.list_available_ports(), ensure_ascii=False, indent=2)

    @mcp.tool()
    async def configure_connection(
        port: str, baudrate: int = 115200, action: str = "open"
    ) -> str:
        """打开/关闭串口并配置参数。action=open 打开(幂等),close 关闭。"""
        if action == "close":
            await manager.close(port)
            return f"closed {port}"
        await manager.open_or_get(port, SerialConfig(baudrate=baudrate))
        return json.dumps({"session_id": port})

    @mcp.tool()
    async def send_data(
        session_id: str,
        payload: str,
        encoding: str = "text",
        wait_policy: str = "none",
        stop_pattern: str = "",
        timeout_ms: int = 2000,
    ) -> str:
        """发送数据并按策略获取响应。

        encoding: text | hex
        wait_policy: none(射后不理) | timeout(等满) | keyword(等正则命中) | at_command(等 OK/ERROR)
        """
        s = manager.get(session_id)
        if not s:
            return "ERROR: session not open"
        data = bytes.fromhex(payload) if encoding == "hex" else payload.encode("utf-8")
        resp = await s.send_data(
            data, "mcp", wait_policy, stop_pattern or None, timeout_ms
        )
        return resp.decode("utf-8", "ignore") if resp else "(no wait)"

    @mcp.tool()
    async def read_urc(session_id: str) -> str:
        """读取设备主动上报(URC)缓冲。"""
        s = manager.get(session_id)
        if not s:
            return "ERROR: session not open"
        items = await s.read_urc()
        return "".join(d.decode("utf-8", "ignore") for d in items)

    @mcp.tool()
    async def get_recent_log(session_id: str, count: int = 100) -> str:
        """获取最近收发日志(方向/来源/时间戳/base64 数据)。"""
        s = manager.get(session_id)
        if not s:
            return "ERROR: session not open"
        return json.dumps([e.to_dict() for e in s.recent_log(count)], ensure_ascii=False)

    @mcp.tool()
    async def transact(
        session_id: str,
        send: str,
        expect: str = "",
        timeout_ms: int = 2500,
        settle_ms: int = 300,
    ) -> str:
        """发送并等回复(请求/响应式,尽力归属)。

        有 expect(正则)时等命中;否则等 timeout_ms。settle_ms 预留(后续用于静默判定)。
        """
        s = manager.get(session_id)
        if not s:
            return "ERROR: session not open"
        policy = "keyword" if expect else "timeout"
        resp = await s.send_data(
            send.encode("utf-8"), "mcp:transact", policy, expect or None, timeout_ms
        )
        return resp.decode("utf-8", "ignore") if resp else ""

    return mcp


def build(manager):
    """构建 MCP streamable http ASGI app + session_manager。

    返回 (asgi_app, session_manager)。session_manager 必须在 host(FastAPI)
    的 lifespan 中 run()——mount 的 sub-app lifespan 不会被自动触发。
    """
    mcp = make_mcp(manager)
    app = mcp.streamable_http_app()  # 触发 _session_manager 懒初始化
    return app, mcp.session_manager
