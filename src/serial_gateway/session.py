"""串口会话引擎(pyserial + 后台线程读,跨平台兼容)。

替代 pyserial-asyncio(后者在 Windows 上对 COM 端口支持不稳定)。
后台读线程 → run_coroutine_threadsafe → asyncio 事件循环,线程安全桥接。
"""
from __future__ import annotations

import asyncio
import base64
import collections
import re
import threading
import time
from dataclasses import dataclass

import serial

from .config import SerialConfig


@dataclass
class LogEntry:
    dir: str  # 'rx' | 'tx'
    source: str  # 'device' | 'client:<id>'
    ts: float
    data: bytes

    def to_dict(self) -> dict:
        return {
            "dir": self.dir,
            "source": self.source,
            "ts": int(self.ts * 1000),
            "data_b64": base64.b64encode(self.data).decode("ascii"),
        }


class SerialSession:
    """一个物理串口的活跃会话。用 pyserial + 后台线程驱动读写循环。"""

    def __init__(self, name: str, config: SerialConfig, ring_cap: int = 262144):
        self.name = name
        self.config = config
        self.ring_cap = ring_cap
        self._ring: collections.deque[LogEntry] = collections.deque()
        self._ring_bytes = 0
        self._subs: dict[str, asyncio.Queue] = {}
        self._op_lock = asyncio.Lock()
        self._resp_waiter: tuple | None = None
        self._urc: collections.deque = collections.deque(maxlen=500)
        self._serial: serial.Serial | None = None
        self._read_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    async def open(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._serial = serial.Serial(
            port=self.name,
            baudrate=self.config.baudrate,
            bytesize=self.config.bytesize,
            parity=self.config.parity,
            stopbits=self.config.stopbits,
            xonxoff=self.config.xonxoff,
            rtscts=self.config.rtscts,
            timeout=0.1,
        )
        # CH342 等 USB 转串口芯片需 DTR 信号线拉高才正常收发
        try:
            self._serial.dtr = True
        except Exception:
            pass
        self._closed = False
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

    @property
    def is_open(self) -> bool:
        return not self._closed and self._serial is not None

    @property
    def subscriber_ids(self) -> list[str]:
        return list(self._subs)

    # ---- 后台读线程 ----

    def _read_loop(self) -> None:
        """后台线程:持续读取串口数据,通过 run_coroutine_threadsafe 桥接到 asyncio。"""
        while not self._closed:
            try:
                n = self._serial.in_waiting
                if n:
                    data = self._serial.read(n)
                else:
                    time.sleep(0.01)
                    continue
                if data:
                    asyncio.run_coroutine_threadsafe(self._on_rx(data), self._loop)
            except Exception as e:
                if not self._closed:
                    import logging
                    logging.getLogger("serial_gateway").warning("读线程异常退出: %s", e)
                break

    async def _on_rx(self, data: bytes) -> None:
        """收到设备数据:写 ring → 响应归属 → fan-out(在事件循环线程执行)。"""
        entry = LogEntry("rx", "device", time.time(), data)
        self._push_ring(entry)

        w = self._resp_waiter
        if w is not None:
            fut, regex, buf = w
            buf.extend(data)
            text = bytes(buf).decode("utf-8", "ignore")
            if regex is not None and regex.search(text):
                self._resp_waiter = None
                if not fut.done():
                    fut.set_result(bytes(buf))
        else:
            self._urc.append(data)

        await self._fanout(entry)

    # ---- 写 ----

    async def write(self, data: bytes, source: str) -> None:
        async with self._op_lock:
            self._serial.write(data)
            entry = LogEntry("tx", f"client:{source}", time.time(), data)
            self._push_ring(entry)
            await self._fanout(entry)

    async def send_data(
        self,
        payload: bytes,
        source: str,
        wait_policy: str = "none",
        stop_pattern: str | None = None,
        timeout_ms: int = 2000,
    ) -> bytes:
        # 整个 send + wait 在锁内,避免并发 send_data 覆盖 _resp_waiter
        async with self._op_lock:
            self._serial.write(payload)
            entry = LogEntry("tx", f"client:{source}", time.time(), payload)
            self._push_ring(entry)
            await self._fanout(entry)

            if wait_policy == "none":
                return b""

            regex = None
            if wait_policy == "keyword" and stop_pattern:
                regex = re.compile(stop_pattern)
            elif wait_policy == "at_command":
                regex = re.compile(r"\b(OK|ERROR)\b")

            buf = bytearray()
            fut = self._loop.create_future()
            self._resp_waiter = (fut, regex, buf)
            try:
                return await asyncio.wait_for(fut, max(timeout_ms, 1) / 1000)
            except asyncio.TimeoutError:
                self._resp_waiter = None
                return bytes(buf)

    async def read_urc(self) -> list[bytes]:
        items = list(self._urc)
        self._urc.clear()
        return items

    # ---- 订阅 ----

    async def subscribe(self, client_id: str) -> asyncio.Queue:
        if client_id in self._subs:
            raise ValueError(f"client 已订阅: {client_id}")
        q: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self._subs[client_id] = q
        return q

    async def unsubscribe(self, client_id: str) -> None:
        self._subs.pop(client_id, None)

    async def configure(self, config: SerialConfig) -> None:
        """应用新串口参数(持锁,重开底层端口)。"""
        async with self._op_lock:
            self.config = config
            if self._serial:
                self._serial.baudrate = config.baudrate
                self._serial.bytesize = config.bytesize
                self._serial.parity = config.parity
                self._serial.stopbits = config.stopbits
                self._serial.xonxoff = config.xonxoff
                self._serial.rtscts = config.rtscts

    def recent_log(self, count: int = 100) -> list[LogEntry]:
        items = list(self._ring)
        return items[-count:] if count else items

    async def _fanout(self, entry: LogEntry) -> None:
        for q in list(self._subs.values()):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def _push_ring(self, entry: LogEntry) -> None:
        self._ring.append(entry)
        self._ring_bytes += len(entry.data)
        while self._ring_bytes > self.ring_cap and self._ring:
            old = self._ring.popleft()
            self._ring_bytes -= len(old.data)

    async def close(self) -> None:
        self._closed = True
        if self._read_thread:
            self._read_thread.join(timeout=2)
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass


class SessionManager:
    """多端口会话管理:同一物理端口单例复用。"""

    def __init__(self, ring_cap: int = 262144):
        self.ring_cap = ring_cap
        self._sessions: dict[str, SerialSession] = {}
        self._lock = asyncio.Lock()

    async def open_or_get(self, name: str, config: SerialConfig) -> SerialSession:
        async with self._lock:
            existing = self._sessions.get(name)
            if existing is not None and not existing._closed:
                if existing.config != config:
                    await existing.close()
                else:
                    return existing
            session = SerialSession(name, config, self.ring_cap)
            await session.open()
            self._sessions[name] = session
            return session

    def get(self, name: str) -> SerialSession | None:
        return self._sessions.get(name)

    def list_open(self) -> list[str]:
        return [n for n, s in self._sessions.items() if s.is_open]

    async def close(self, name: str) -> None:
        async with self._lock:
            session = self._sessions.pop(name, None)
        if session:
            await session.close()
