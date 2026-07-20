"""入口:python -m serial_gateway [--host 0.0.0.0 --port 20000]。

单例运行:同一设备只允许一个实例。重复启动时报错并提示已运行进程 PID。
"""
from __future__ import annotations

import atexit
import os
import sys
import tempfile

import uvicorn

from serial_gateway.config import Config

PID_FILE = os.path.join(tempfile.gettempdir(), "serial-gateway.pid")


def _pid_alive(pid: int) -> bool:
    """检查给定 PID 的进程是否仍在运行。"""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _check_single_instance() -> int | None:
    """检查是否有另一个实例在运行。返回已运行 PID 或 None。"""
    try:
        with open(PID_FILE) as f:
            old_pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None
    if _pid_alive(old_pid):
        return old_pid
    return None


def _write_pid_file() -> None:
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid_file() -> None:
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def main() -> None:
    # 单例检查
    existing = _check_single_instance()
    if existing is not None:
        print(
            f"ERROR: serial-gateway 已在运行 (PID {existing})。\n"
            f"  同一设备只允许一个实例。如需重启请先停止旧进程:\n"
            f"  kill {existing}  (Linux/macOS)  或  taskkill /f /pid {existing}  (Windows)",
            file=sys.stderr,
        )
        sys.exit(1)

    _write_pid_file()
    atexit.register(_remove_pid_file)

    cfg = Config.from_args()
    from serial_gateway.server.app import create_app

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
