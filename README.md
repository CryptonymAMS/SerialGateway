# Serial Gateway

跨平台串口网关。单进程承载 HTTP(WebUI)、WebSocket(实时流)、MCP(AI 接入)。
人与 AI 可同时操作同一串口,专为嵌入式调试设计。

## 快速开始

```bash
git clone <repo> && cd serial-gateway
pip install -r requirements.txt
pip install -e .
python -m serial_gateway
# → http://localhost:20000
```

> 需 Python 3.10+。Linux 串口权限:`sudo usermod -aG dialout $USER`(重新登录生效)。

## 部署方式

### 方式 A:pip(开发/调试)

```bash
python3.10 -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt -e .
python -m serial_gateway
```

无 Python 3.10?用 [uv](https://astral.sh/uv):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.10 && uv venv --python 3.10 .venv
uv pip install -r requirements.txt -e .
.venv/bin/python -m serial_gateway
```

### 方式 B:单文件二进制(分发/无 Python 环境)

在**目标机**上构建(glibc 自洽):
```bash
pip install -r requirements.txt -e . pyinstaller
bash scripts/build_pyinstaller.sh
# → dist/serial-gateway (单文件,含 Python+依赖+WebUI)
./dist/serial-gateway
```

## WebUI

浏览器打开 `http://<主机IP>:20000`。

- **终端直连**(默认):键盘直接发往设备,Tab 补全、Ctrl+C 透传(像 minicom)
- **输入框**:回车发送,可切文本/十六进制/CRLF
- 底部状态栏切换模式;侧栏配置波特率/数据位/校验/停止位;支持保存常用设备

URL 参数控制默认模式:`?mode=terminal`(默认)或 `?mode=inputbar`。

## MCP(AI 接入)

### Claude Code

```bash
claude mcp add --transport http serial http://<主机IP>:20000/mcp
```

新开会话后,AI 自动获得串口工具,按需调用。

### 其他 MCP 客户端(Cursor / VS Code 等)

JSON 配置:
```json
{
  "mcpServers": {
    "serial": {
      "url": "http://<主机IP>:20000/mcp"
    }
  }
}
```

### 工具列表

| 工具 | 说明 |
|---|---|
| `list_ports` | 枚举串口(含 USB vid/pid/serial) |
| `configure_connection` | 打开/关闭串口(open 幂等) |
| `send_data` | 发送 + 等响应(`wait_policy`: keyword/timeout/none/at_command) |
| `read_urc` | 读设备主动上报(URC) |
| `transact` | 发命令 + 等回复(`expect` 正则 + `timeout_ms`) |
| `get_recent_log` | 收发日志 |

### 单例运行

同一设备只允许一个实例。重复启动报错并提示已运行 PID:
```
ERROR: serial-gateway 已在运行 (PID 12345)。
  kill 12345  (Linux/macOS)  或  taskkill /f /pid 12345  (Windows)
```

## 命令行选项

| 选项 | 默认 | 说明 |
|---|---|---|
| `--host` | `0.0.0.0` | 监听地址(局域网可访问) |
| `--port` | `20000` | HTTP/WS/MCP 共用端口 |
| `--data-dir` | `data` | 设备 profile 存储目录 |
| `--ring-cap` | `262144` | 每端口 ring buffer 字节上限 |

## 跨设备访问

默认监听 `0.0.0.0:20000`,局域网内任意设备浏览器/MCP 直接用 `http://<主机IP>:20000`。

## 芯片兼容

CH340 / CH342 / CH343 / PL2303 / CP210x / CDC-ACM — 全部已验证(Windows/macOS/Linux)。
