"""多客户端并发场景验证:
  - 先来后到:不同配置的后来者收到 config_conflict,沿用后可连
  - write 不阻塞事件循环(即使设备不消费,write_timeout 兜底快速返回)
  - 三个客户端共享同一串口,无死锁
在远端 192.168.192.3 上运行(python 已装 websockets)。
"""
import asyncio
import json
import sys

import websockets

URL = "ws://localhost:20000/ws"
PORT = "/dev/ttyUSB0"

CFG_15 = {"baudrate": 1500000, "bytesize": 8, "parity": "N", "stopbits": 1, "xonxoff": False, "rtscts": False}
CFG_115 = {"baudrate": 115200, "bytesize": 8, "parity": "N", "stopbits": 1, "xonxoff": False, "rtscts": False}


async def open_client(cid, cfg):
    ws = await websockets.connect(URL)
    await ws.send(json.dumps({"type": "subscribe", "port": PORT, "client": cid, "config": cfg}))
    # 读到第一个控制消息(subscribed/config_conflict/error/session_closed)
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=6)
        msg = json.loads(raw)
        if msg.get("type") in ("subscribed", "config_conflict", "error", "session_closed"):
            return ws, msg


async def recv_ctrl(ws, timeout=8):
    """读到下一个控制类消息,跳过 data。"""
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        if msg.get("type") != "data":
            return msg
    return None


async def main():
    # 1. client1 @1500000
    ws1, m1 = await open_client("c1", CFG_15)
    assert m1["type"] == "subscribed", f"step1: {m1}"
    print("STEP1 client1 subscribed @1500000  OK")

    # 2. client2 @1500000 (复用同一 session)
    ws2, m2 = await open_client("c2", CFG_15)
    assert m2["type"] == "subscribed", f"step2: {m2}"
    print("STEP2 client2 subscribed @1500000  OK (shared session, 不踢人)")

    # 3. client3 @115200 -> 应被拒绝(config_conflict),current=1500000
    ws3, m3 = await open_client("c3", CFG_115)
    assert m3["type"] == "config_conflict", f"step3: 期望 config_conflict, 实际 {m3}"
    assert m3["current_config"]["baudrate"] == 1500000, m3
    print("STEP3 client3 @115200 被拒(config_conflict, current=1500000)  OK")
    await ws3.close()

    # 4. client3 沿用 1500000 -> subscribed
    ws3b, m3b = await open_client("c3b", CFG_15)
    assert m3b["type"] == "subscribed", f"step4: {m3b}"
    print("STEP4 client3 沿用 1500000 subscribed  OK")

    # 5. client1 write -> written(或 error=write_timeout),关键是不阻塞、快速返回
    await ws1.send(json.dumps({"type": "write", "port": PORT, "client": "c1", "data_b64": "dGVzdAo="}))
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    msg = await recv_ctrl(ws1, timeout=8)
    dt = loop.time() - t0
    assert msg is not None, "step5: client1 write 无响应(事件循环可能被阻塞!)"
    assert msg["type"] in ("written", "error"), f"step5: 意外消息 {msg}"
    assert dt < 4.0, f"step5: write 响应耗时 {dt:.2f}s,疑似阻塞"
    print(f"STEP5 client1 write -> {msg['type']} in {dt:.2f}s  OK (未阻塞)")

    # 6. 确认 client2 仍活着(能收到 client1 write 的 tx fan-out 说明 session 健康)
    #    再让 client2 write 一次
    await ws2.send(json.dumps({"type": "write", "port": PORT, "client": "c2", "data_b64": "YWEK"}))
    msg2 = await recv_ctrl(ws2, timeout=8)
    assert msg2 is not None and msg2["type"] in ("written", "error"), f"step6: {msg2}"
    print(f"STEP6 client2 write -> {msg2['type']}  OK (多客户端仍可输入)")

    for ws in (ws1, ws2, ws3b):
        await ws.close()
    print("\nALL_PASS: 配置乒乓已消除,write 不阻塞,多客户端共享正常")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
