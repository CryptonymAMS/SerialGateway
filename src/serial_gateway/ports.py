"""端口枚举:过滤 Linux 幽灵 8250 端口,只保留真实串口。"""
from __future__ import annotations

import serial.tools.list_ports as _lp


def _is_real_port(p) -> bool:
    """判断是否为真实可用串口(过滤 Linux 幽灵 8250 UART)。"""
    # USB 串口:一定真实
    if p.vid is not None:
        return True
    hwid = p.hwid or ""
    desc = p.description or ""
    # USB 前缀(hwid 含 USB VID:PID 但 vid 解析失败的情况)
    if hwid.startswith("USB"):
        return True
    # Linux 幽灵 8250:hwid="PYSERIAL" 或 description 等于设备名(无意义)
    if hwid in ("PYSERIAL", "n/a", ""):
        return False
    # description 与 device 相同(如 "ttyS0"),说明 sysfs 无设备信息
    if desc == p.device or desc == "n/a":
        return False
    # 有 PCI/其他 hwid 的保留(可能真实)
    return True


def list_available_ports() -> list[dict]:
    out: list[dict] = []
    for p in _lp.comports():
        if not _is_real_port(p):
            continue
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
            ptype = "pci" if (p.hwid or "").startswith("PCI") else "other"
        out.append(
            {
                "name": p.device,
                "port_type": ptype,
                "usb": usb,
                "description": p.description,
            }
        )
    return out

