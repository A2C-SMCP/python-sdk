# -*- coding: utf-8 -*-
# filename: test_manager_host.py
# @Time    : 2026/4/30
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
中文: `MCPServerManager` host 反查索引 + 注册期 fail-fast 单元测试。
英文: Unit tests for `MCPServerManager` host reverse-index + registration-time fail-fast.

覆盖 v0.2 协议指南 §4.4 / §6.4 host 唯一性硬约束在 SDK 端的实现（Issue #12）。
Covers v0.2 protocol §4.4 / §6.4 host uniqueness hard constraint on the SDK side (Issue #12).
"""
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import StdioServerParameters
from mcp.types import AnyUrl, CallToolResult, Resource

from a2c_smcp.computer.mcp_clients.base_client import MCPCapabilityNotSupportedError
from a2c_smcp.computer.mcp_clients.manager import HostConflictError, MCPServerManager
from a2c_smcp.computer.mcp_clients.model import StdioServerConfig


def _make_resource(uri: str, name: str = "r") -> Resource:
    return Resource(uri=AnyUrl(uri), name=name)


class _MockHostClient:
    """
    支持 list_resources_page 翻页 + capability 模拟的最小 MCP Client mock。

    pages: list[tuple[list[Resource], str | None]] — 按顺序模拟单页翻页响应。
    raise_on_list: 异常实例，模拟 list_resources_page 抛错（含 MCPCapabilityNotSupportedError）。
    """

    def __init__(
        self,
        pages: list[tuple[list[Resource], str | None]] | None = None,
        raise_on_list: Exception | None = None,
    ) -> None:
        self.aconnect = AsyncMock()
        self.adisconnect = AsyncMock()
        self.list_tools = AsyncMock(return_value=[])
        call_ret = MagicMock(spec=CallToolResult)
        call_ret.result = None
        call_ret.meta = None
        self.call_tool = AsyncMock(return_value=call_ret)
        self.state = "connected"
        self.message_handler = None
        self._pages = pages or [([], None)]
        self._page_idx = 0
        self._raise_on_list = raise_on_list

    async def list_resources_page(
        self, cursor: str | None = None,
    ) -> tuple[list[Resource], str | None]:
        if self._raise_on_list is not None:
            raise self._raise_on_list
        # 简单实现: 按调用顺序返回 _pages 中的下一项；忽略 cursor 内容
        if self._page_idx >= len(self._pages):
            return [], None
        page = self._pages[self._page_idx]
        self._page_idx += 1
        return page


def _config(name: str) -> StdioServerConfig:
    return StdioServerConfig(
        name=name,
        disabled=False,
        forbidden_tools=[],
        tool_meta={},
        default_tool_meta=None,
        server_parameters=MagicMock(spec=StdioServerParameters),
    )


@pytest.fixture
async def manager() -> AsyncGenerator[MCPServerManager, None]:
    m = MCPServerManager(auto_connect=True, auto_reconnect=True)
    yield m
    await m.aclose()


def _patch_factory(monkeypatch, mapping: dict[str, _MockHostClient]) -> None:
    """按 server name 把 client_factory 路由到对应 mock。"""
    def factory(config, message_handler=None):  # noqa: ARG001
        if config.name not in mapping:
            return _MockHostClient()
        client = mapping[config.name]
        client.message_handler = message_handler
        return client

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)


@pytest.mark.asyncio
async def test_two_servers_different_hosts_both_register(manager, monkeypatch):
    """两个 server 声明不同 host → 都能成功注册；find_server_by_host 正确反查。"""
    clients = {
        "server-a": _MockHostClient(
            pages=[([
                _make_resource("dpe://com.example.docs/a.md"),
                _make_resource("window://com.example.docs/main"),
            ], None)],
        ),
        "server-b": _MockHostClient(
            pages=[([_make_resource("dpe://com.example.code/file.py")], None)],
        ),
    }
    _patch_factory(monkeypatch, clients)

    await manager.aadd_or_aupdate_server(_config("server-a"))
    await manager.aadd_or_aupdate_server(_config("server-b"))

    assert manager.find_server_by_host("com.example.docs") == "server-a"
    assert manager.find_server_by_host("com.example.code") == "server-b"
    assert manager.find_server_by_host("com.example.unknown") is None


@pytest.mark.asyncio
async def test_one_server_multiple_hosts_allowed(manager, monkeypatch):
    """1 server 声明 N 个 host（聚合多上游）→ 注册成功，索引含全部 host。"""
    clients = {
        "aggregator": _MockHostClient(
            pages=[([
                _make_resource("dpe://com.example.docs/a"),
                _make_resource("dpe://com.example.code/b"),
                _make_resource("window://com.example.editor/main"),
            ], None)],
        ),
    }
    _patch_factory(monkeypatch, clients)

    await manager.aadd_or_aupdate_server(_config("aggregator"))

    for host in ("com.example.docs", "com.example.code", "com.example.editor"):
        assert manager.find_server_by_host(host) == "aggregator"


@pytest.mark.asyncio
async def test_host_conflict_fail_fast(manager, monkeypatch):
    """两个 server 声明相同 host → 后注册者被拒绝（HostConflictError），先注册者保留。"""
    shared_host_resources = [_make_resource("dpe://com.example.shared/x")]
    clients = {
        "first": _MockHostClient(pages=[(shared_host_resources, None)]),
        "second": _MockHostClient(pages=[(shared_host_resources, None)]),
    }
    _patch_factory(monkeypatch, clients)

    await manager.aadd_or_aupdate_server(_config("first"))

    with pytest.raises(HostConflictError) as exc_info:
        await manager.aadd_or_aupdate_server(_config("second"))

    err = exc_info.value
    assert err.host == "com.example.shared"
    assert err.conflicting_servers == ("first", "second")

    # 先注册者保留索引；后注册者被回滚
    assert manager.find_server_by_host("com.example.shared") == "first"
    assert "second" not in manager._active_clients
    # config 也应回滚（aadd_or_aupdate_server 的 try/except 已恢复 backup_config）
    clients["second"].adisconnect.assert_awaited()


@pytest.mark.asyncio
async def test_aremove_server_releases_hosts_for_reuse(manager, monkeypatch):
    """server 注销后 host 释放——同 host 可被新 server 注册（避免"假冲突"）。"""
    clients = {
        "owner-a": _MockHostClient(
            pages=[([_make_resource("dpe://com.example.docs/x")], None)],
        ),
        "owner-b": _MockHostClient(
            pages=[([_make_resource("dpe://com.example.docs/y")], None)],
        ),
    }
    _patch_factory(monkeypatch, clients)

    await manager.aadd_or_aupdate_server(_config("owner-a"))
    assert manager.find_server_by_host("com.example.docs") == "owner-a"

    await manager.aremove_server("owner-a")
    assert manager.find_server_by_host("com.example.docs") is None

    # 释放后同 host 应能被另一个 server 注册成功
    await manager.aadd_or_aupdate_server(_config("owner-b"))
    assert manager.find_server_by_host("com.example.docs") == "owner-b"


@pytest.mark.asyncio
async def test_register_time_list_failure_marks_pending(manager, monkeypatch):
    """注册期 list_resources 临时失败（非 capability 缺失）→ 注册成功 + WARN + 标 pending。"""
    transient_err = RuntimeError("network glitch")
    clients = {
        "flaky": _MockHostClient(raise_on_list=transient_err),
    }
    _patch_factory(monkeypatch, clients)

    await manager.aadd_or_aupdate_server(_config("flaky"))

    # server 已注册成功（不阻塞业务）
    assert "flaky" in manager._active_clients
    # 但 host 索引为空（pending）
    assert manager._host_to_servers == {}
    assert "flaky" in manager._host_index_pending


@pytest.mark.asyncio
async def test_capability_missing_no_pending_no_hosts(manager, monkeypatch):
    """server 未声明 resources capability → 注册成功，无 host 贡献，**不**进入 pending（永久状态）。"""
    clients = {
        "no-resources": _MockHostClient(
            raise_on_list=MCPCapabilityNotSupportedError("no resources"),
        ),
    }
    _patch_factory(monkeypatch, clients)

    await manager.aadd_or_aupdate_server(_config("no-resources"))

    assert "no-resources" in manager._active_clients
    assert manager._host_to_servers == {}
    assert manager._host_index_pending == set()


@pytest.mark.asyncio
async def test_aretry_pending_host_index_success(manager, monkeypatch):
    """pending server 在 list 恢复后调用 aretry_pending_host_index → 写入索引并清除 pending。"""
    flaky_client = _MockHostClient(raise_on_list=RuntimeError("network glitch"))
    clients = {"flaky": flaky_client}
    _patch_factory(monkeypatch, clients)

    await manager.aadd_or_aupdate_server(_config("flaky"))
    assert "flaky" in manager._host_index_pending

    # 模拟网络恢复：撤销 raise，让后续 list 返回真实 pages
    flaky_client._raise_on_list = None
    flaky_client._pages = [([_make_resource("dpe://com.example.late/x")], None)]  # type: ignore[list-item]
    flaky_client._page_idx = 0

    cleared = await manager.aretry_pending_host_index("flaky")
    assert cleared is True
    assert manager.find_server_by_host("com.example.late") == "flaky"
    assert "flaky" not in manager._host_index_pending


@pytest.mark.asyncio
async def test_aretry_pending_host_index_conflict_unregisters(manager, monkeypatch):
    """pending server 在 retry 时检测到与已注册 server 冲突 → 注销该 server 并抛 HostConflictError。"""
    incumbent_client = _MockHostClient(
        pages=[([_make_resource("dpe://com.example.shared/x")], None)],
    )
    flaky_client = _MockHostClient(raise_on_list=RuntimeError("network glitch"))
    clients = {"incumbent": incumbent_client, "flaky": flaky_client}
    _patch_factory(monkeypatch, clients)

    await manager.aadd_or_aupdate_server(_config("incumbent"))
    await manager.aadd_or_aupdate_server(_config("flaky"))
    assert "flaky" in manager._host_index_pending

    # 网络恢复后 flaky 暴露与 incumbent 共用 host
    flaky_client._raise_on_list = None
    flaky_client._pages = [([_make_resource("dpe://com.example.shared/y")], None)]  # type: ignore[list-item]
    flaky_client._page_idx = 0

    with pytest.raises(HostConflictError) as exc_info:
        await manager.aretry_pending_host_index("flaky")

    err = exc_info.value
    assert err.host == "com.example.shared"
    assert err.conflicting_servers == ("incumbent", "flaky")
    # flaky 已被注销，incumbent 保留
    assert "flaky" not in manager._active_clients
    assert manager.find_server_by_host("com.example.shared") == "incumbent"


@pytest.mark.asyncio
async def test_aretry_pending_no_op_when_not_pending(manager, monkeypatch):
    """retry 调用一个不在 pending 集合的 server → 直接返回 True，幂等无副作用。"""
    clients = {
        "happy": _MockHostClient(pages=[([_make_resource("dpe://com.example.h/a")], None)]),
    }
    _patch_factory(monkeypatch, clients)
    await manager.aadd_or_aupdate_server(_config("happy"))

    assert await manager.aretry_pending_host_index("happy") is True
    assert await manager.aretry_pending_host_index("never-existed") is True


@pytest.mark.asyncio
async def test_clear_all_clears_host_index(manager, monkeypatch):
    """ainitialize / aclose 路径下 _clear_all 必须把 host 索引和 pending 一并清空。"""
    clients = {
        "s1": _MockHostClient(pages=[([_make_resource("dpe://com.example.h1/a")], None)]),
        "s2": _MockHostClient(raise_on_list=RuntimeError("net")),
    }
    _patch_factory(monkeypatch, clients)
    await manager.aadd_or_aupdate_server(_config("s1"))
    await manager.aadd_or_aupdate_server(_config("s2"))
    assert manager._host_to_servers
    assert manager._host_index_pending

    await manager.ainitialize([])

    assert manager._host_to_servers == {}
    assert manager._host_index_pending == set()


@pytest.mark.asyncio
async def test_only_dpe_and_window_schemes_contribute_hosts(manager, monkeypatch):
    """host 收集仅看 dpe:// 与 window:// scheme；其他 scheme 不参与索引。"""
    clients = {
        "mixed": _MockHostClient(
            pages=[([
                _make_resource("dpe://com.example.real/a"),
                _make_resource("window://com.example.win/main"),
                _make_resource("https://example.com/not-counted"),
                _make_resource("file:///tmp/local"),
                _make_resource("custom-scheme://noise/not-counted"),
            ], None)],
        ),
    }
    _patch_factory(monkeypatch, clients)
    await manager.aadd_or_aupdate_server(_config("mixed"))

    assert manager.find_server_by_host("com.example.real") == "mixed"
    assert manager.find_server_by_host("com.example.win") == "mixed"
    # 其他 scheme 的 host 不应进入索引
    assert manager.find_server_by_host("example.com") is None
    assert manager.find_server_by_host("noise") is None
    assert "tmp" not in manager._host_to_servers


@pytest.mark.asyncio
async def test_pagination_exhausted(manager, monkeypatch):
    """注册期穷举翻页：跨页 host 全部进入索引。"""
    clients = {
        "paginated": _MockHostClient(
            pages=[
                ([_make_resource("dpe://com.example.p1/a")], "cursor-2"),
                ([_make_resource("dpe://com.example.p2/b")], "cursor-3"),
                ([_make_resource("window://com.example.p3/main")], None),
            ],
        ),
    }
    _patch_factory(monkeypatch, clients)
    await manager.aadd_or_aupdate_server(_config("paginated"))

    for host in ("com.example.p1", "com.example.p2", "com.example.p3"):
        assert manager.find_server_by_host(host) == "paginated"


@pytest.mark.asyncio
async def test_restart_same_server_not_false_conflict(manager, monkeypatch):
    """同名 server 重启（aremove + add）不应 false-conflict 自身。"""
    def _fresh_client() -> _MockHostClient:
        return _MockHostClient(
            pages=[([_make_resource("dpe://com.example.same/x")], None)],
        )

    def factory(config, message_handler=None):  # noqa: ARG001
        # 每次工厂调用返回新实例；保持 host 资源声明一致
        c = _fresh_client()
        c.message_handler = message_handler
        return c

    monkeypatch.setattr("a2c_smcp.computer.mcp_clients.manager.client_factory", factory)

    await manager.aadd_or_aupdate_server(_config("reborn"))
    assert manager.find_server_by_host("com.example.same") == "reborn"

    # 注销后再注册同名 server，应能成功（host 已释放）
    await manager.aremove_server("reborn")
    await manager.aadd_or_aupdate_server(_config("reborn"))
    assert manager.find_server_by_host("com.example.same") == "reborn"


@pytest.mark.asyncio
async def test_find_server_by_host_returns_none_for_unknown(manager, monkeypatch):
    """无匹配 host → 返回 None（上层据此映射 4014 MCP Server Not Found）。"""
    clients = {
        "s1": _MockHostClient(pages=[([_make_resource("dpe://com.example.real/a")], None)]),
    }
    _patch_factory(monkeypatch, clients)
    await manager.aadd_or_aupdate_server(_config("s1"))

    assert manager.find_server_by_host("com.example.unknown") is None
    assert manager.find_server_by_host("") is None


@pytest.mark.asyncio
async def test_extract_hosts_static_helper_filters_correctly():
    """_extract_hosts_from_resources 静态：仅 dpe:// + window:// 计入；非法 URI 跳过。"""
    items: list[Resource] = [
        _make_resource("dpe://com.example.a/x"),
        _make_resource("window://com.example.b/main"),
        _make_resource("https://example.com/skip"),
    ]
    hosts = MCPServerManager._extract_hosts_from_resources(items)
    assert hosts == {"com.example.a", "com.example.b"}
