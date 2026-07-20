"""serial-gateway: 跨平台串口网关(Python)。

单进程同时承载 HTTP(WebUI) / WebSocket(实时流) / MCP-over-HTTP(AI)。
串口引擎借鉴 serial2mcp 的「响应 vs URC 分流」,并增加多客户端共享、
ring buffer、设备 profile 等能力。
"""

__version__ = "0.1.0"
