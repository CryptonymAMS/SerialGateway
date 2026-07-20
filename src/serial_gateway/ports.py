"""端口枚举(借 pyserial 的 comports,自带 USB 元信息)。"""
from __future__ import annotations

import serial.tools.list_ports as _lp


def list_available_ports() -> list[dict]:
    out: list[dict] = []
    for p in _lp.comports():
        if p.vid is not None:
            usb = {
                "vid": p.vid,
                "pid": p.pid or 0,
                "serial_number": p.serial_number,
                "manufacturer": p.manufacturer,
                "product": p.product,
            }
            ptype = "usb"
        else:
            usb = None
            ptype = "unknown"
        out.append(
            {
                "name": p.device,
                "port_type": ptype,
                "usb": usb,
                "description": p.description,
            }
        )
    return out
