# -*- coding: utf-8 -*-
# filename: test_collect_candidates.py
# @Time    : 2026/07/17
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
#143 / R4: ``cli/resolve.py::collect_candidates`` 的三源合并与**归属推导**。

归属（``attribution``）不是装饰——协议 §5.1-3 / PROTO-10 扩条把它定为 **MUST**：多命中报错时必须同时打印
bundle_id + display name + 归属，「只列 bundle_id 用户分不清哪个是自己的」。故三源各自的归属值都需守卫：

1. **声明面**（``resolve_mcp_declarations``）→ origin 字面（``user`` / ``project`` / ``local`` / ``embed`` /
   ``flag`` / ``policy``）；
2. **ledger**（``collect_enabled_bundled_servers``）→ ``plugin:<plugin>``——它恰好补上声明面的结构性缺口
   （``origin == plugin`` **不进** resolve）；
3. **运行期**兜底 → ``runtime``（ad-hoc ``amount_server`` 的纯 transient 投影）。

隔离同 ``test_repl_addressing.py``：chdir + XDG + A2C_SKILL_HOME + ``Computer(skill_home=)`` 四面锚 tmp。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from a2c_smcp.computer.cli import resolve as resolve_mod
from a2c_smcp.computer.cli.resolve import collect_candidates
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.mcp_clients.model import MCPServerConfig
from a2c_smcp.computer.settings.mcp_config import McpWriteScope
from a2c_smcp.computer.settings.recovery import BundledServerRecord


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("A2C_SKILL_HOME", str(tmp_path / "skills"))
    monkeypatch.chdir(tmp_path)


def _stdio_dict(name: str, *, bundle_id: str | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "type": "stdio",
        "name": name,
        "disabled": True,
        "server_parameters": {"command": "echo", "args": [], "env": None, "cwd": None, "encoding": "utf-8"},
    }
    if bundle_id is not None:
        cfg["bundle_id"] = bundle_id
    return cfg


async def _fresh_computer(tmp_path: Path) -> Computer:
    comp = Computer(
        name="cands", inputs=set(), mcp_servers=set(), auto_connect=False, auto_reconnect=False,
        skill_home=tmp_path / "skills",
    )
    await comp.boot_up()
    return comp


def _attribution_of(comp: Computer, bundle_id: str) -> str:
    return next(c.attribution for c in collect_candidates(comp) if c.bundle_id == bundle_id)


def _bundled_record(name: str, *, bundle_id: str, plugin: str, tmp_path: Path) -> BundledServerRecord:
    """构造一条 ledger bundled 记录（ledger 源的归属输入）。"""
    cfg = TypeAdapter(MCPServerConfig).validate_python(_stdio_dict(name, bundle_id=bundle_id))
    return BundledServerRecord(
        plugin_id=f"{plugin}@mkt", plugin=plugin, marketplace="mkt", install_path=tmp_path / "p", config=cfg,
    )


# ── 源 1：声明面 origin ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected"),
    [(McpWriteScope.LOCAL, "local"), (McpWriteScope.PROJECT, "project"), (McpWriteScope.USER, "user")],
)
async def test_declaration_origin_becomes_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: McpWriteScope, expected: str,
) -> None:
    """声明面条目的归属 = 其 origin 字面（三个可写 scope 各自可分辨，非同值）。"""
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("decl.srv"), scope)

    assert _attribution_of(comp, "decl_srv") == expected


# ── 源 2：ledger plugin 归属 ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ledger_bundled_server_attributed_to_its_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ledger 派生的 enabled bundled server → ``plugin:<plugin>``。

    这是**声明面结构性缺口的唯一补法**：``origin == plugin`` 不进 resolve（SettingsScope 无 PLUGIN 成员），
    plugin bundled server 走 transient ``amount_server`` 挂载 ⇒ 不查 ledger 就只能标成 ``runtime``，
    用户在多命中列表里就分不出「哪条是插件带来的」。
    """
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    record = _bundled_record("fs.srv", bundle_id="bundle_fs", plugin="fs-tools", tmp_path=tmp_path)
    monkeypatch.setattr(resolve_mod, "collect_enabled_bundled_servers", lambda home, declared: [record])

    assert _attribution_of(comp, "bundle_fs") == "plugin:fs-tools"


@pytest.mark.asyncio
async def test_user_declaration_wins_over_same_bundle_id_ledger_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 bundle_id 时**用户自己的声明胜出**（§2.5 用户主权）——显示用户能操作的那条真相。

    正对照：把用户声明拿掉，同一条就该回落 ``plugin:fs-tools`` ⇒ 证明本断言不是永真。
    """
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    record = _bundled_record("fs.srv", bundle_id="bundle_fs", plugin="fs-tools", tmp_path=tmp_path)
    monkeypatch.setattr(resolve_mod, "collect_enabled_bundled_servers", lambda home, declared: [record])
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("fs.srv", bundle_id="bundle_fs"), McpWriteScope.LOCAL)

    assert _attribution_of(comp, "bundle_fs") == "local", "用户声明胜出"

    # 正对照：撤掉用户声明 → 归属回落 plugin（否则上面的断言可能只是碰巧）。
    await comp.aremove_server("bundle_fs")
    assert _attribution_of(comp, "bundle_fs") == "plugin:fs-tools"


# ── 源 3：运行期兜底 ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adhoc_transient_projection_attributed_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无声明、非 ledger bundled 的 ad-hoc ``amount_server`` 投影 → ``runtime``。"""
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.amount_server(_stdio_dict("adhoc.srv"))

    assert _attribution_of(comp, "adhoc_srv") == "runtime"


# ── 查找空间：运行期 ∪ 声明面（#143 决策 1）────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_candidate_space_unions_runtime_and_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选 = 运行期 ∪ 声明面：两侧各有独占条目时都必须在，且按 bundle_id 去重不重复计。"""
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("both.srv"), McpWriteScope.LOCAL)  # 声明 + 运行期
    await comp.amount_server(_stdio_dict("only.rt"))  # 仅运行期

    cands = collect_candidates(comp)
    by_id = {c.bundle_id: c for c in cands}

    assert by_id["both_srv"].attribution == "local"
    assert by_id["only_rt"].attribution == "runtime"
    assert len(cands) == len(by_id), "同一 bundle_id 不得产生重复候选"


# ── 降级：账本不可读不连坐（#155 读层容错）────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreadable_ledger_degrades_without_collateral_and_leaves_a_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """账本读失败 ⇒ 只丢 plugin 归属标注，**不该**让整个寻址瘫痪（用户的 server 与 plugin 账本无关）。

    且降级 **MUST 留痕**：静默降级会让「归属显示 runtime 而非 plugin:x」变成查无实据的怪事。
    """
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("mine.srv"), McpWriteScope.LOCAL)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(resolve_mod, "collect_enabled_bundled_servers", _boom)

    # a2c_smcp 根 logger 设了 propagate=False（utils/logger.py:29）⇒ caplog 默认挂 root 抓不到，
    # 须把 caplog.handler 直接挂到本模块 logger 上（同 tests/unit_tests/computer/settings/test_store.py:161 约定）。
    resolve_mod.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING)
    try:
        cands = collect_candidates(comp)
    finally:
        resolve_mod.logger.removeHandler(caplog.handler)

    assert any(c.bundle_id == "mine_srv" for c in cands), "账本坏了不该连坐用户自己的 server（去连坐）"
    assert any("ledger unreadable" in r.getMessage() for r in caplog.records), "降级必须留痕"
