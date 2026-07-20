"""设备 profile:持久化 + USB 指纹匹配。"""
from __future__ import annotations

import json
import os
import uuid


class ProfileStore:
    """每个 profile 一个 JSON 文件。"""

    def __init__(self, dirpath: str):
        self.dir = dirpath

    def _path(self, pid: str) -> str:
        # 防 path traversal:只允许字母数字/下划线/连字符
        if not pid or not all(c.isalnum() or c in "-_" for c in pid):
            raise ValueError(f"invalid profile id: {pid}")
        return os.path.join(self.dir, f"{pid}.json")

    async def list(self) -> list[dict]:
        if not os.path.isdir(self.dir):
            return []
        out = []
        for f in os.listdir(self.dir):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, f), encoding="utf-8") as fp:
                    out.append(json.load(fp))
            except Exception:
                pass
        return out

    async def save(self, profile: dict) -> dict:
        os.makedirs(self.dir, exist_ok=True)
        if not profile.get("id"):
            profile["id"] = str(uuid.uuid4())
        with open(self._path(profile["id"]), "w", encoding="utf-8") as fp:
            json.dump(profile, fp, ensure_ascii=False, indent=2)
        return profile

    async def delete(self, pid: str) -> None:
        try:
            os.remove(self._path(pid))
        except FileNotFoundError:
            pass


def match_port(profile: dict, ports: list[dict]) -> str | None:
    """指纹匹配:vid+pid+serial > vid+pid > last_port_name。"""
    fp = profile.get("fingerprint", {})
    vid = fp.get("vid")
    pid = fp.get("pid")
    serial = fp.get("serial_number")

    if vid is not None and serial:
        for p in ports:
            u = p.get("usb") or {}
            if u.get("vid") == vid and u.get("pid") == pid and u.get("serial_number") == serial:
                return p["name"]
    if vid is not None:
        for p in ports:
            u = p.get("usb") or {}
            if u.get("vid") == vid and u.get("pid") == pid:
                return p["name"]
    last = profile.get("last_port_name")
    if last and any(p["name"] == last for p in ports):
        return last
    return None
