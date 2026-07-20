"""配置:命令行参数 + 串口参数类型。"""
from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass
class SerialConfig:
    baudrate: int = 115200
    bytesize: int = 8  # serial.EIGHTBITS
    parity: str = "N"  # serial.PARITY_NONE
    stopbits: float = 1  # serial.STOPBITS_ONE
    xonxoff: bool = False
    rtscts: bool = False

    def to_dict(self) -> dict:
        return {
            "baudrate": self.baudrate,
            "bytesize": self.bytesize,
            "parity": self.parity,
            "stopbits": self.stopbits,
            "xonxoff": self.xonxoff,
            "rtscts": self.rtscts,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "SerialConfig":
        d = d or {}
        return cls(
            baudrate=int(d.get("baudrate", 115200)),
            bytesize=int(d.get("bytesize", 8)),
            parity=str(d.get("parity", "N")),
            stopbits=float(d.get("stopbits", 1)),
            xonxoff=bool(d.get("xonxoff", False)),
            rtscts=bool(d.get("rtscts", False)),
        )


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 20000
    data_dir: str = "data"
    ring_cap: int = 262144  # 每端口 ring buffer 字节上限

    @classmethod
    def from_args(cls) -> "Config":
        p = argparse.ArgumentParser(prog="serial-gateway")
        p.add_argument("--host", default="0.0.0.0")
        p.add_argument("--port", type=int, default=20000)
        p.add_argument("--data-dir", default="data")
        p.add_argument("--ring-cap", type=int, default=262144)
        a = p.parse_args()
        return cls(
            host=a.host, port=a.port, data_dir=a.data_dir, ring_cap=a.ring_cap
        )
