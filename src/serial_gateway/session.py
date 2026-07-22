"""串口会话引擎(pyserial + 后台线程读,跨平台兼容)。

替代 pyserial-asyncio(后者在 Windows 上对 COM 端口支持不稳定)。
后台读线程 → run_coroutine_threadsafe → asyncio 事件循环,线程安全桥接。

并发安全要点:
- pyserial 的 Serial 对象非线程安全。读线程与事件循环线程(write/configure/close)
  通过 `self._serial_lock` 互斥访问底层串口。
- 所有同步阻塞调用(serial.write / thread.join)一律下沉到 executor,
  避免钉死 asyncio 事件循环(否则一个慢写会冻结所有客户端)。
- 多客户端共享串口时遵循「先来后到」:已有订阅者则以既有配置为准,
  拒绝不同配置的后来者(ConfigConflictError),杜绝配置乒乓互相踢人。
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

# 同步写串口的兜底超时(秒)。配合 executor,确保设备不消费数据时不会永久阻塞。
_WRITE_TIMEOUT = 2.0


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


class ConfigConflictError(Exception):
    """已有客户端以不同配置打开该串口(先来后到,拒绝配置乒乓)。"""

    def __init__(self, name: str, current_config: SerialConfig):
        self.name = name
        self.current_config = current_config
        super().__init__(
            f"port {name} already open with a different config; "
            f"existing subscribers hold precedence"
        )


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
        self._serial_lock = threading.Lock()  # 保护 _serial 跨线程访问
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
            write_timeout=_WRITE_TIMEOUT,  # 设备不消费时 write 不会永久阻塞
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
        """后台线程:持续读取串口数据,通过 run_coroutine_threadsafe 桥接到 asyncio。

        访问 _serial 全程持锁,与 write/configure/close 互斥。
        """
        while not self._closed:
            try:
                with self._serial_lock:
                    if self._closed or self._serial is None:
                        break
                    n = self._serial.in_waiting
                    data = self._serial.read(n) if n else b""
                if data:
                    asyncio.run_coroutine_threadsafe(self._on_rx(data), self._loop)
                else:
                    time.sleep(0.01)
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

    async def _write_raw(self, data: bytes) -> None:
        """同步串口写入下沉到 executor,避免阻塞事件循环。

        write_timeout 兜底:设备不消费数据时 pyserial 抛 SerialTimeoutException,
        传播给调用方,而不是永久钉死写线程(进而冻结事件循环)。
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._do_write, data)

    def _do_write(self, data: bytes) -> None:
        with self._serial_lock:
            if self._serial is not None and not self._closed:
                self._serial.write(data)  # write_timeout 触发时抛 SerialTimeoutException

    async def write(self, data: bytes, source: str) -> None:
        async with self._op_lock:
            await self._write_raw(data)
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
        # 发送(锁内,保证命令原子) → 等待响应(锁外,不阻塞其他客户端)
        async with self._op_lock:
            await self._write_raw(payload)
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
        """应用新串口参数(持锁,直接设置已打开端口的参数)。"""
        async with self._op_lock:
            self.config = config
            with self._serial_lock:
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
        """关闭会话。

        - 先向所有订阅者 queue 投递哨兵 None,fwd_task 收到后可优雅退出并发
          session_closed,避免协程泄漏与「订阅错位」造成的永久挂起。
        - thread.join 下沉 executor,不阻塞事件循环。
        """
        self._closed = True
        for q in list(self._subs.values()):
            try:
                q.put_nowait(None)  # 哨兵
            except asyncio.QueueFull:
                pass
        self._subs.clear()
        if self._read_thread:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._read_thread.join, 2)
        with self._serial_lock:
            if self._serial:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None


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
                    # 有活跃订阅者:先来后到,拒绝不同配置(防止乒乓互相踢人)
                    if existing.subscriber_ids:
                        raise ConfigConflictError(name, existing.config)
                    # 无订阅者(残留 session):重开以应用新配置
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
