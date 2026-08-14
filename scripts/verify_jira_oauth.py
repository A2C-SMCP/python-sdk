#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性手动验收脚本（不提交 CI）：Jira 官方 MCP（Atlassian Remote MCP）OAuth 授权全流程。

背景：mcp 升级 1.29.0 + 准入守卫放开（裸 Bearer challenge 不再拒绝，AS 发现交由
mcp well-known 回退链：PRM path 插入/根 → 服务器 origin 的 RFC 8414 AS metadata）。
本脚本以**真实上游**（Atlassian 生产 MCP 端点 + 真实 DCR + 浏览器授权）做一次端到端
验收。只运行一次，需要人来完成浏览器授权。

关键事实（2026-08-14 实测）：
- ``https://mcp.atlassian.com/v1/mcp`` 的 401 是**裸 Bearer**（无 resource_metadata，
  CC 同款配置）——升级前会被 SDK 准入拒绝；升级后经 well-known 回退链拿到服务器
  origin 的 AS metadata（issuer ``cf.mcp.atlassian.com``，authorize 端点
  ``mcp.atlassian.com/v1/authorize``，DCR ``cf.mcp.atlassian.com/v1/register``）；
- DCR 动态注册 → 无需在 developer.atlassian.com 手工创建 OAuth 应用；
- PKCE S256 由 mcp SDK 自动完成；回调由**宿主**（本脚本）的 loopback 监听承接。

运行：
    uv run --no-sync python scripts/verify_jira_oauth.py        # 完整授权流程（需浏览器）
    uv run --no-sync python scripts/verify_jira_oauth.py --smoke  # 只验证到授权 URL 生成
    uv run --no-sync python scripts/verify_jira_oauth.py --redirect-host 127.0.0.1  # 回调主机回退

流程：
    1. boot Computer（embed 声明 Jira server，DCR 模式）
    2. 宿主 loopback 回调监听（ephemeral 端口，默认 localhost）
    3. create_oauth_flow → launch() → 打印授权 URL（浏览器完成 Atlassian 登录授权）
    4. 浏览器回调 → complete() → 打印 outcome
    5. 验证：oauth_status == authorized + aget_available_tools() 列出 Jira 工具
"""
from __future__ import annotations

import asyncio
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlparse

from pydantic import TypeAdapter

from a2c_smcp.computer import Computer
from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthBeginRequest,
    OAuthCallback,
    OAuthCancellation,
    OAuthCancellationReason,
)
from a2c_smcp.utils.bundle_id import resolve_bundle_id

SMOKE = "--smoke" in sys.argv
# 回调主机：默认 localhost（主流客户端惯例；Atlassian org 白名单可能只放行 localhost 而非
# 127.0.0.1）。--redirect-host 127.0.0.1 可回退。
REDIRECT_HOST = "localhost"
for _i, _arg in enumerate(sys.argv):
    if _arg == "--redirect-host" and _i + 1 < len(sys.argv):
        REDIRECT_HOST = sys.argv[_i + 1]
CALLBACK_TIMEOUT_SECONDS = 300  # 浏览器授权时限（5 分钟）


class _CallbackServer(ThreadingHTTPServer):
    """宿主 loopback 回调监听：单次授权回调即足够。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.callback_event = threading.Event()
        self.result: dict[str, str | None] = {"code": None, "state": None, "error": None}


class _CallbackHandler(BaseHTTPRequestHandler):
    """接收 AS 的重定向：``/callback?code=...&state=...``（或 ``?error=access_denied``）。"""

    def do_GET(self) -> None:  # noqa: N802
        qs = parse_qs(urlparse(self.path).query)
        server = cast(_CallbackServer, self.server)
        server.result = {
            "code": qs.get("code", [None])[0],
            "state": qs.get("state", [None])[0],
            "error": qs.get("error", [None])[0],
        }
        server.callback_event.set()
        body = (
            b"<html><body><h2>Authorization callback received.</h2>"
            b"<p>You may close this window and return to the terminal.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # 静默访问日志 / silence access log
        return


def _make_callback_server(host: str) -> _CallbackServer:
    """按回调主机建监听：localhost → 双栈 AF_INET6（::），127.0.0.1 → AF_INET。

    macOS 上 AF_INET6 绑定 ``::`` 默认同时接受 IPv4（浏览器无论解析 localhost 到
    ::1 还是 127.0.0.1 都能连上）。
    """
    if host == "localhost":

        class _DualStackServer(_CallbackServer):
            address_family = socket.AF_INET6

        return _DualStackServer(("::", 0), _CallbackHandler)

    class _IPv4Server(_CallbackServer):
        address_family = socket.AF_INET

    return _IPv4Server(("127.0.0.1", 0), _CallbackHandler)


async def _wait_for_jira_tools(comp: Computer, bundle_id: str, timeout: float = 60.0) -> list[str]:
    """授权交换完成后，交互式 connect 任务提交活跃 client 可能仍需数秒——轮询工具列表。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    tools: list[str] = []
    while loop.time() < deadline:
        available = await comp.aget_available_tools()
        # SMCPTool 是 TypedDict → 返回元素为 dict，按 key 访问（非属性）
        tools = sorted(t["name"] for t in available if t["bundle_id"] == bundle_id)
        if tools:
            return tools
        await asyncio.sleep(1.0)
    return tools


async def _run() -> int:
    jira_cfg = TypeAdapter(MCPServerConfig).validate_python(
        {
            "type": "streamable",
            "name": "jira",
            # CC 同款端点：裸 401 challenge → SDK 准入后由 mcp well-known 回退链发现
            # 服务器 origin 的 AS metadata（authorize: /v1/authorize，DCR: cf. 域）
            "server_parameters": {"url": "https://mcp.atlassian.com/v1/mcp"},
            "oauth": {
                # DCR 动态注册：无需手工创建 OAuth 应用。scopes 声明不碍事——mcp 的
                # scope 选择策略（challenge → PRM → ASM）在无 challenge scope 时由
                # 服务器侧代填（CC 同款行为）
                "scopes": ["read:jira-work", "read:me", "offline_access"],
                "mode": {"type": "authorizationCode", "registration": "dynamic"},
            },
        }
    )
    comp = Computer(
        name="jira-oauth-verify",
        inputs=set(),
        mcp_servers={jira_cfg},
        auto_connect=False,
        auto_reconnect=False,
    )
    async with comp:
        bundle_id = resolve_bundle_id(jira_cfg)

        # 宿主 loopback 回调监听（ephemeral 端口；默认 localhost + 双栈，规避 org 白名单
        # 只放行 localhost 而拒绝 127.0.0.1 的场景）
        server = _make_callback_server(REDIRECT_HOST)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        redirect_uri = f"http://{REDIRECT_HOST}:{server.server_address[1]}/callback"
        print(f"[1] Jira server 已挂载（bundle_id={bundle_id!r}），回调监听 {redirect_uri}")

        # 注册 flow + launch：裸 challenge 准入 → well-known 回退链 discovery → DCR → PKCE
        flow = comp.create_oauth_flow(bundle_id, OAuthBeginRequest(redirect_uri=redirect_uri))
        print("[2] 发起授权流程（challenge 准入 → well-known discovery → DCR → PKCE）...")
        try:
            launch = await asyncio.wait_for(flow.launch(), timeout=120)
        except Exception as exc:
            print(f"[✗] 授权流程启动失败（详见异常）：{type(exc).__name__}: {exc}")
            return 1
        print("[3] 授权 URL 已生成：\n")
        print(f"    {launch.authorization_url}\n")

        if SMOKE:
            await flow.cancel(OAuthCancellationReason.Cancelled)
            print("[smoke] 仅验证到授权 URL 生成，流程已取消，退出。")
            return 0

        print(f"    请用浏览器打开上面的 URL，登录 Atlassian 账号并完成授权（{CALLBACK_TIMEOUT_SECONDS // 60} 分钟超时）。")
        print("    授权完成后浏览器会跳回本机回调页，回到终端等待结果。")

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, server.callback_event.wait),
                CALLBACK_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            outcome = await flow.cancel(OAuthCancellationReason.Timeout)
            print(f"[✗] 等待浏览器回调超时，流程已终止（outcome={outcome.outcome}）。")
            return 1

        result = server.result
        if result.get("error"):
            error = result["error"]
            print(f"[✗] 授权端返回错误：{error!r}")
            if error == "access_denied":
                outcome = await flow.cancel_callback(
                    OAuthCancellation(
                        state=result["state"] or "",
                        reason=OAuthCancellationReason.AccessDenied,
                    )
                )
                print(f"    流程已终止（outcome={outcome.outcome}）。")
            return 1

        # 防御：AS 回跳既无 error 也无 code/state（异常形态）——不构造 OAuthCallback 以免
        # pydantic 校验异常掩盖真实诊断信息。
        if not result.get("code") or not result.get("state"):
            print(f"[✗] 回调缺少 code/state 参数（原始 query 未含合法授权码）：{result!r}")
            return 1

        print("[4] 收到浏览器回调，提交授权码（token 交换）...")
        outcome = await flow.complete(OAuthCallback(code=result["code"], state=result["state"]))
        if outcome.outcome != "authorized":
            print(f"[✗] 授权未完成：outcome={outcome.outcome!r}")
            return 1
        print(f"[✓] 授权完成！granted_scopes={outcome.scopes}")

        # ── 验证 1：oauth_status ──
        status = await comp.oauth_status(bundle_id)
        detail = f"（scopes={status.scopes}）" if status.state == "authorized" else ""
        print(f"[✓] oauth_status = {status.state!r}{detail}")

        # ── 验证 2：已授权连接的工具列表 ──
        tools = await _wait_for_jira_tools(comp, bundle_id)
        if not tools:
            print("[✗] 60 秒内未从已授权连接获取到任何工具。")
            return 1
        print(f"[✓] 从 Jira 官方 MCP 已授权连接获取到 {len(tools)} 个工具：")
        for name in tools:
            print(f"    - {name}")

        print("\n=== 验收通过：Jira 官方 MCP OAuth 授权全流程 OK ===")
        return 0
    return 1  # pragma: no cover


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
