# Server 协议版本握手部署指南 / Enabling the Protocol Version Handshake

> 适用范围：**部署 A2C-SMCP Server** 的工程师。本指南说明如何在你的部署形态中启用「连接版本握手」
> （`a2c_version` 协商 / HTTP 400 + Socket.IO `4008`），覆盖 **裸 Socket.IO Server（ASGI / WSGI）**
> 与 **FastAPI + Socket.IO 集成** 两类场景。
>
> 协议依据：[a2c-smcp-protocol `versioning.md`](https://github.com/A2C-SMCP/a2c-smcp-protocol)。

---

## 1. 背景：握手不是自动生效的

SDK 把协议版本握手实现为一层**传输层中间件**（`a2c_smcp/server/middleware.py`），它在请求进入
Socket.IO `connect` handler **之前**于 HTTP 传输层校验 URL query 中的 `a2c_version`，不兼容直接
返回 `HTTP 400 + 4008`。这是协议唯一规范的校验时机——即使业务 handler 有 bug 也无法绕过。

**关键事实：这层中间件不会自动套上。** `socketio.ASGIApp(...)` / `socketio.WSGIApp(...)` 本身
不做版本校验。如果你**只**创建了 socketio app 而没有用 A2C 中间件包裹它，那么：

- 任意 `a2c_version`（甚至 `99.0.0` 或完全缺失）都会被放行，连接照常建立（HTTP 200）；
- 协议 MUST 要求的「不兼容连接被拒、绝不建立」**被静默跳过**，且不会有任何报错——
  这正是 [python-sdk #93](https://github.com/A2C-SMCP/python-sdk/issues/93) 暴露的问题。

> 一句话：**创建 socketio app 后，必须再用 A2C 中间件包一层，握手才真正生效。**

---

## 2. 中间件总览

`a2c_smcp.server` 导出两个对称的中间件类：

| 类 | 运行栈 | 包裹对象 |
|----|--------|---------|
| `A2CProtocolVersionASGIMiddleware` | ASGI（uvicorn / hypercorn，FastAPI/Starlette/Sanic 等） | `socketio.ASGIApp(...)` |
| `A2CProtocolVersionWSGIMiddleware` | WSGI（gunicorn / werkzeug，Flask 等） | `socketio.WSGIApp(...)` |

构造参数（两者一致）：

```python
A2CProtocolVersionASGIMiddleware(
    app,                              # 下游 socketio app（必填，位置参数）
    *,
    socketio_path="/socket.io",       # MUST 与 socketio app 的 socketio_path 完全一致
    server_version=PROTOCOL_VERSION,  # Server 实现的协议版本，默认 SDK 内置常量
)
```

设计要点：

- **路径作用域**：中间件**只**校验命中 `socketio_path` 前缀的请求；其它路由（你的 REST API、
  健康检查等）原样透传，互不影响。
- **`server_version` 默认即 `a2c_smcp.PROTOCOL_VERSION`**，绝大多数部署无需显式传入；仅在你需要
  对外宣称一个不同的协议版本（例如灰度 / 测试不兼容）时才覆盖。
- v0.x 阶段严格匹配 `MAJOR.MINOR`，`PATCH` 任意。即客户端 `X.Y.Z` 仅与 Server 区间
  `[X.Y.0, X.Y.999]` 兼容。

导入方式：

```python
from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.server import (
    A2CProtocolVersionASGIMiddleware,
    A2CProtocolVersionWSGIMiddleware,
)
```

---

## 3. 场景 A：裸 Socket.IO ASGI Server（uvicorn）

最常见的异步部署：用 uvicorn 直接跑一个 socketio ASGI app。

```python
import socketio
from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.server import A2CProtocolVersionASGIMiddleware, SMCPNamespace, DefaultAuthenticationProvider

SOCKETIO_PATH = "/socket.io"

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
sio.register_namespace(SMCPNamespace(DefaultAuthenticationProvider(admin_secret="secret")))

# 1) 先建 socketio ASGI app
asgi_app = socketio.ASGIApp(sio, socketio_path=SOCKETIO_PATH)

# 2) 再用 A2C 中间件包裹 —— 握手在此生效
app = A2CProtocolVersionASGIMiddleware(asgi_app, socketio_path=SOCKETIO_PATH, server_version=PROTOCOL_VERSION)

# 运行: uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## 4. 场景 B：裸 Socket.IO WSGI Server（Flask / gunicorn）

同步部署完全对称，换用 WSGI 中间件即可。

```python
from socketio import Server, WSGIApp
from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.server import A2CProtocolVersionWSGIMiddleware, SyncSMCPNamespace, DefaultSyncAuthenticationProvider

SOCKETIO_PATH = "/socket.io"

sio = Server(cors_allowed_origins="*")
sio.register_namespace(SyncSMCPNamespace(DefaultSyncAuthenticationProvider(admin_secret="secret")))

# 1) 先建 socketio WSGI app
wsgi_app = WSGIApp(sio, socketio_path=SOCKETIO_PATH)

# 2) 再用 A2C 中间件包裹
app = A2CProtocolVersionWSGIMiddleware(wsgi_app, socketio_path=SOCKETIO_PATH, server_version=PROTOCOL_VERSION)

# 运行: gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker main:app
```

---

## 5. 场景 C：FastAPI + Socket.IO 集成

FastAPI（Starlette）本身是 ASGI 应用。和 Socket.IO 集成时，**推荐用 `socketio.ASGIApp` 的
`other_asgi_app` 把 FastAPI 作为「非 socketio 路由」的回落**，然后把整体再交给 A2C 中间件包裹。
因为中间件按路径作用域工作，FastAPI 的业务路由会原样透传，只有 `/socket.io/*` 走版本校验。

```python
import socketio
from fastapi import FastAPI
from a2c_smcp import PROTOCOL_VERSION
from a2c_smcp.server import A2CProtocolVersionASGIMiddleware, SMCPNamespace, DefaultAuthenticationProvider

SOCKETIO_PATH = "/socket.io"

fastapi_app = FastAPI()

@fastapi_app.get("/health")  # 你的普通 REST 路由：不受握手影响，正常透传
async def health() -> dict:
    return {"ok": True}

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
sio.register_namespace(SMCPNamespace(DefaultAuthenticationProvider(admin_secret="secret")))

# 1) socketio app 以 FastAPI 作为非 socketio 请求的回落（other_asgi_app）
asgi_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app, socketio_path=SOCKETIO_PATH)

# 2) A2C 中间件包在最外层：只拦 /socket.io 握手，/health 等 FastAPI 路由透传
app = A2CProtocolVersionASGIMiddleware(asgi_app, socketio_path=SOCKETIO_PATH, server_version=PROTOCOL_VERSION)

# 运行: uvicorn main:app --host 0.0.0.0 --port 8000
```

> **通用原则（适用于任何挂载方式）**：A2C 中间件必须位于**最外层**，即所有 `/socket.io` 握手
> 请求进入 engine.io 之前都会先经过它；且其 `socketio_path` 必须等于 engine.io 实际接收请求的
> 完整路径。若你改用 `fastapi_app.mount("/socket.io", socketio.ASGIApp(sio))` 的挂载方式，则把
> A2C 中间件包在 `fastapi_app` 外层、并保持 `socketio_path="/socket.io"` 一致即可。

---

## 6. 强约束：`socketio_path` 必须三处一致

握手能否命中，取决于路径匹配。下面三者**必须完全一致**，否则中间件要么拦不到握手（形同未启用），
要么误伤其它路由：

1. `socketio.ASGIApp(sio, socketio_path=...)` / `socketio.WSGIApp(sio, socketio_path=...)`
2. `A2CProtocolVersionASGIMiddleware(app, socketio_path=...)` / WSGI 同理
3. 客户端连接时的 `socketio_path`（SDK 客户端默认 `/socket.io`）

默认值均为 `/socket.io`，保持默认即可；一旦自定义，请三处同步修改。

---

## 7. WebSocket-only 与 transports 注意

- ASGI 中间件同时校验 `http`（polling 握手）与 `websocket`（直连 WS 握手）两类 scope。对 WS-only
  不兼容客户端：运行栈支持 ASGI WebSocket Denial Response（uvicorn ≥ 0.21 / hypercorn / daphne）时，
  回与 polling 路径**字节一致**的 `HTTP 400 + 4008`；否则以 WebSocket close code `4900` 关闭握手。
- WSGI 不区分 http / websocket scope（WS 握手起始即普通 HTTP GET），天然走同一 `400/4008` 路径。
- **客户端侧**：A2C SDK 客户端强制 polling-first 握手（显式 `transports=["websocket"]` 会被护栏
  纠正），以确保 `4008` 拒因可被还原归一为 `ProtocolVersionError`。**部署 Server 时无需为此做额外
  配置**，但请勿在反向代理层屏蔽 polling 握手路径。

---

## 8. 验证握手是否生效

部署后用裸 HTTP 探测 polling 握手端点即可验证（无需真实客户端）：

```bash
# 不兼容版本 → 期望 HTTP 400 + 4008
curl -i "http://127.0.0.1:8000/socket.io/?EIO=4&transport=polling&a2c_version=99.0.0"
# 预期：HTTP/1.1 400 Bad Request
#       X-A2C-Error-Code: 4008
#       {"code":4008,"message":"Protocol version mismatch","server_version":"0.2.0",
#        "client_version":"99.0.0","min_supported":"0.2.0","max_supported":"0.2.999"}

# 缺失 a2c_version → 期望 HTTP 400（无 4008 header）
curl -i "http://127.0.0.1:8000/socket.io/?EIO=4&transport=polling"

# 兼容版本 → 期望 HTTP 200（透传给 engine.io，返回 open 包）
curl -i "http://127.0.0.1:8000/socket.io/?EIO=4&transport=polling&a2c_version=0.2.0"

# 非 socketio 路由不受影响 → 期望正常响应
curl -i "http://127.0.0.1:8000/health"
```

若不兼容版本仍返回 `200`，说明中间件没有套上（参见 §1）。

---

## 9. 检查清单

- [ ] 创建 `socketio.ASGIApp` / `WSGIApp` 后，**用 A2C 中间件包裹**再交给 uvicorn / gunicorn
- [ ] 中间件、socketio app、客户端三处 `socketio_path` 一致（默认 `/socket.io`）
- [ ] `server_version` 一般保持默认 `PROTOCOL_VERSION`，仅在灰度/测试时覆盖
- [ ] 反向代理未屏蔽 polling 握手路径
- [ ] 用 §8 的 `curl` 探测确认 `99.0.0 → 400/4008`、`0.2.0 → 200`

---

## 相关文档

- [Server 使用指南](server-guide.md) — 命名空间、认证、事件处理
- [快速开始](getting-started.md) — 三方协作最小示例
- 协议规范：a2c-smcp-protocol `docs/specification/versioning.md`、`error-handling.md`
