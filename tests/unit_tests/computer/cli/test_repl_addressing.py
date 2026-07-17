# -*- coding: utf-8 -*-
# filename: test_repl_addressing.py
# @Time    : 2026/07/17
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
#143 / R4: REPL `server rm` / `start` / `stop` 的 name 寻址与**静默假成功**回归。

病理（本文件复现的即是它）: 三个动词把用户敲的 token **原样**传给以 **bundle_id** 为键的库层 API，
两者只在 ``bundle_id == normalize_name(name)`` 时碰巧相等。name 含 ``.`` / 空格 / CJK 或配了显式
``bundle_id`` 时查不到 → 底层静默 no-op → REPL 照样打印成功 ⇒ 用户以为删了/停了，server 还在跑、
工具还暴露给 Agent，**无从察觉**。

修法（协议 sdk-api-guidance §5.1 / PROTO-10）: 解析上移 CLI ``resolve_target``；未命中/多命中 MUST 报错。

隔离（三面都要锚，缺一即污染开发者真实环境）:

- ``chdir(tmp)``——``aremove_server`` 是 durable，会写 ``cwd/.tfrobot/``（#137 已踩）；
- ``XDG_CONFIG_HOME``→tmp——user scope ``mcp.json``；
- ``A2C_SKILL_HOME`` + ``XDG_DATA_HOME``→tmp **且** ``Computer(skill_home=)``——``boot_up`` 会 ``mkdir`` +
  迁移账本，``collect_candidates`` 会读 ``installed_plugins.json``；XDG_CONFIG_HOME **管不到**它
  （SKILL Home 解析链是 ``A2C_SKILL_HOME → $XDG_DATA_HOME/a2c/skills → ~/.a2c/skills``）。

``disabled=True`` + ``auto_connect=False`` 免拉起真实进程。

⚠️ 夹具铁律（Epic #147 陷阱其一）: name 与 bundle_id **必须分叉** —— 全文件用 ``my.server`` → ``my_server``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import a2c_smcp.computer.cli.main as cli_main
from a2c_smcp.computer.cli.main import _interactive_loop
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.mcp_config import McpWriteScope
from a2c_smcp.utils.bundle_id import resolve_bundle_id

# 分叉夹具：`.` 被 normalize_name 折成 `_` ⇒ display name ≠ bundle_id，断言才有鉴别力。
_DISPLAY_NAME = "my.server"
_BUNDLE_ID = "my_server"


class FakePromptSession:
    """中文: 将脚本化命令注入交互循环。English: Feed scripted inputs to interactive loop."""

    def __init__(self, commands: list[str]) -> None:
        self._commands = list(commands)

    async def prompt_async(self, *_: str, **__: Any) -> str:
        if not self._commands:
            raise EOFError
        return self._commands.pop(0)


class _NoClient:
    """不触网的最小 SMCP 客户端桩（本文件只验证寻址，不验证 emit）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.connected = False

    async def emit_update_config(self) -> None:  # pragma: no cover - 未连接时 CLI 不调
        pass


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离落盘面：project/local 锚 cwd（#116），user 锚 XDG → tmp，SKILL Home 锚 tmp。

    ``A2C_SKILL_HOME`` **必须**设——``XDG_CONFIG_HOME`` 管不到 SKILL Home：其解析链是
    ``A2C_SKILL_HOME → $XDG_DATA_HOME/a2c/skills → ~/.a2c/skills``。不设则 ``boot_up`` 会在**开发者真实
    home** 建目录、``collect_candidates`` 会读**真实 installed_plugins.json**（本机装了同名 plugin 即翻车）。
    ``_fresh_computer`` 另传 ``skill_home=`` 双保险（同 ``test_computer_dual_path_crud.py:108`` 约定）。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("A2C_SKILL_HOME", str(tmp_path / "skills"))
    monkeypatch.chdir(tmp_path)


def _stdio_dict(name: str, *, bundle_id: str | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "type": "stdio",
        "name": name,
        "disabled": True,  # 免 boot/auto_connect 拉起真实进程
        "server_parameters": {"command": "echo", "args": [], "env": None, "cwd": None, "encoding": "utf-8"},
    }
    if bundle_id is not None:
        cfg["bundle_id"] = bundle_id
    return cfg


async def _fresh_computer(tmp_path: Path) -> Computer:
    """F7: 走**真实构造路径**（CLI 空集构造 + boot_up），不依赖 `_FakeComputer` 桩。

    ``skill_home`` 显式锚 tmp：``boot_up`` 会 ``ensure_skill_home()`` 建目录 + ``migrate_legacy_installs`` 写账本，
    绝不能落到开发者真实 home（见 :func:`_isolate`）。
    """
    comp = Computer(
        name="repl_addr",
        inputs=set(),
        mcp_servers=set(),
        auto_connect=False,
        auto_reconnect=False,
        skill_home=tmp_path / "skills",
    )
    await comp.boot_up()
    return comp


async def _run_repl(comp: Computer, commands: list[str], monkeypatch: pytest.MonkeyPatch, capsys: Any) -> str:
    """驱动真实交互循环并返回其打印输出。"""
    monkeypatch.setattr(cli_main, "SMCPComputerClient", _NoClient)
    monkeypatch.setattr(cli_main, "PromptSession", lambda: FakePromptSession([*commands, "exit"]))
    monkeypatch.setattr(cli_main, "patch_stdout", lambda raw: _no_patch_stdout())
    await _interactive_loop(comp)
    return capsys.readouterr().out


class _no_patch_stdout:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


# ── 假成功：stop ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_unknown_token_must_error_not_fake_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """🔴 现状: ``_astop_client`` 的 ``pop(bundle_id, None)`` 吞掉 miss ⇒ REPL 打印「✅ 停止完成」。

    协议 §5.1-5: 未命中 MUST 报错，MUST NOT 静默成功（假成功回执：打印「已停止」而 server 仍在跑）。
    """
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)

    out = await _run_repl(comp, ["stop nonexistent"], monkeypatch, capsys)

    assert "停止完成" not in out, "未注册 token 却打印停止成功 = 静默假成功（#143 P0）"
    assert "未找到" in out or "not found" in out


def _spy_stop(comp: Computer, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """探针：记录 REPL **实际交给库层**的 token。

    这是 #143 唯一有鉴别力的观测点。反例（勿重蹈）：断言「停完后不在 ``_active_clients``」——本文件夹具
    ``disabled=True`` + ``auto_connect=False`` 下 client 从未启动，该集合恒空 ⇒ **没停也成立**，零鉴别力。
    """
    seen: list[str] = []
    original = comp.mcp_manager.astop_client

    async def _recording(bundle_id: str) -> None:
        seen.append(bundle_id)
        await original(bundle_id)

    monkeypatch.setattr(comp.mcp_manager, "astop_client", _recording)
    return seen


@pytest.mark.asyncio
async def test_stop_by_display_name_passes_resolved_bundle_id_to_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """🔴 现状: ``stop my.server`` 把 **display name** 原样当 bundle_id 传下去 → 查不到 → 静默 no-op + 假 ✅。

    改后: name 唯一命中 → 解析为 ``my_server`` 再交库层。库层收到的**必须**是 bundle_id（R4）。
    """
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.amount_server(_stdio_dict(_DISPLAY_NAME))
    assert any(resolve_bundle_id(c) == _BUNDLE_ID for c in comp.mcp_manager.server_configs())
    seen = _spy_stop(comp, monkeypatch)

    out = await _run_repl(comp, [f"stop {_DISPLAY_NAME}"], monkeypatch, capsys)

    assert seen == [_BUNDLE_ID], f"库层应收到解析后的 bundle_id，实收 {seen}（原样传 display name = #143 病根）"
    assert "停止完成" in out


# ── 假成功：server rm ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_rm_unknown_token_must_error_not_fake_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """🔴 现状: 声明面无匹配 + 运行期无该 id ⇒ 落 ``aremove_server`` 档⑤ no-op ⇒ 仍打印「已移除配置」。"""
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)

    out = await _run_repl(comp, ["server rm nonexistent"], monkeypatch, capsys)

    assert "已移除配置" not in out, "未找到目标却打印已移除 = 静默假成功（#143 P0）"
    assert "未找到" in out or "not found" in out


@pytest.mark.asyncio
async def test_server_rm_by_display_name_actually_removes_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """🔴 现状: ``server rm my.server`` 拿 name 当 bundle_id 比对声明 → 匹配不上 → 无声不删 + 假成功。"""
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict(_DISPLAY_NAME), McpWriteScope.LOCAL)
    assert _DISPLAY_NAME in comp.resolve_mcp_declarations(env={}).servers

    out = await _run_repl(comp, [f"server rm {_DISPLAY_NAME}"], monkeypatch, capsys)

    assert "已移除配置" in out
    assert _DISPLAY_NAME not in comp.resolve_mcp_declarations(env={}).servers, "声明未被真正删除"


# ── 多命中：列候选（bundle_id + name + 归属三者）────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_name_collision_lists_candidates_with_id_name_and_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """协议 §5.1-3 + PROTO-10 扩条: 多命中 → 报错并列出各候选的 **bundle_id + display name + 归属**。

    只列 bundle_id 用户分不清哪个是自己的。
    """
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    # 同名合法共存（协议 §5.6）：显式异 bundle_id 是两个不同身份。
    # ⚠️ 归属**刻意分叉**（local 声明 vs runtime 投影）：若两条归属同值，「候选须列归属」的断言会同值致盲
    #    ——Epic #147 陷阱第一条。归属存在的唯一理由就是让用户分清哪个是自己的，夹具必须体现该差异。
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("dup.srv", bundle_id="bundle_left"), McpWriteScope.LOCAL)
    await comp.amount_server(_stdio_dict("dup.srv", bundle_id="bundle_right"))

    out = await _run_repl(comp, ["stop dup.srv"], monkeypatch, capsys)

    assert "停止完成" not in out, "多命中 MUST 报错，MUST NOT 任选一个执行"
    assert "bundle_left" in out and "bundle_right" in out, "候选须列 bundle_id"
    assert "dup.srv" in out, "候选须列 display name（只列 bundle_id 用户分不清哪个是自己的）"
    # PROTO-10 扩条：归属**也**是 MUST。两条归属分叉 ⇒ 本断言可证伪（删掉输出里的归属即转红）。
    assert "(local)" in out, "候选须列归属：用户自己声明的那条"
    assert "(runtime)" in out, "候选须列归属：纯运行期投影那条"


@pytest.mark.asyncio
async def test_stop_by_explicit_bundle_id_hits_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """协议 §5.1-2: 0 个 name 命中 ∧ token 是合法已注册 bundle_id → 精确命中执行（消歧后的正解路径）。"""
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.amount_server(_stdio_dict("dup.srv", bundle_id="bundle_left"))
    await comp.amount_server(_stdio_dict("dup.srv", bundle_id="bundle_right"))
    seen = _spy_stop(comp, monkeypatch)

    out = await _run_repl(comp, ["stop bundle_left"], monkeypatch, capsys)

    assert seen == ["bundle_left"], f"应精确命中显式 bundle_id，实收 {seen}"
    assert "停止完成" in out


# ── start：与 stop 同构（三个动词共用 _resolve_or_report，勿只守两个）───────────────────────────


def _spy_start(comp: Computer, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """探针：记录 REPL 实际交给 ``astart_client`` 的 token（同 :func:`_spy_stop` 理由）。"""
    seen: list[str] = []
    original = comp.mcp_manager.astart_client

    async def _recording(bundle_id: str) -> None:
        seen.append(bundle_id)
        await original(bundle_id)

    monkeypatch.setattr(comp.mcp_manager, "astart_client", _recording)
    return seen


@pytest.mark.asyncio
async def test_start_unknown_token_reports_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """``start`` 未命中不假成功（本就不假），但报错**内容**已变更：从库层内部错误 → 人机面「未找到」。

    旧: ❌ 启动服务器失败: Unknown server bundle_id='nonexistent'（把内部 id 概念漏给用户）
    新: ❌ 未找到服务器 'nonexistent'
    """
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)

    out = await _run_repl(comp, ["start nonexistent"], monkeypatch, capsys)

    assert "启动完成" not in out
    assert "未找到" in out or "not found" in out


@pytest.mark.asyncio
async def test_start_by_display_name_passes_resolved_bundle_id_to_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """``start my.server`` 同样须解析后再下传——历史原样传 name → 库层抛 Unknown server（不假成功但够不着）。"""
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.amount_server(_stdio_dict(_DISPLAY_NAME))
    seen = _spy_start(comp, monkeypatch)

    await _run_repl(comp, [f"start {_DISPLAY_NAME}"], monkeypatch, capsys)

    assert seen == [_BUNDLE_ID], f"库层应收到解析后的 bundle_id，实收 {seen}"


@pytest.mark.asyncio
async def test_start_name_collision_lists_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """``start`` 的多命中同样列候选（三动词语义一致，不能只有 stop/rm 守住）。"""
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.aadd_or_aupdate_server_in_scope(_stdio_dict("dup.srv", bundle_id="bundle_left"), McpWriteScope.LOCAL)
    await comp.amount_server(_stdio_dict("dup.srv", bundle_id="bundle_right"))

    out = await _run_repl(comp, ["start dup.srv"], monkeypatch, capsys)

    assert "启动完成" not in out, "多命中 MUST 报错，MUST NOT 任选一个执行"
    assert "bundle_left" in out and "bundle_right" in out
    assert "(local)" in out and "(runtime)" in out


# ── 决策 1 补丁：已声明未挂载不假成功、不漏内部错（#143 隔离审查 🔴3）─────────────────────────────


@pytest.mark.asyncio
async def test_stop_declared_but_unmounted_is_not_fake_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """决策 1（∪声明面）让「已声明未挂载」解析成功；``stop`` 对它须诚实陈述，MUST NOT 打「✅ 停止完成」。

    构造：durable 声明落盘、但用一个全新 Computer boot（不过审批门）⇒ 声明在、运行期无。
    """
    _isolate(tmp_path, monkeypatch)
    seed = await _fresh_computer(tmp_path)
    await seed.aadd_or_aupdate_server_in_scope(_stdio_dict(_DISPLAY_NAME), McpWriteScope.PROJECT)

    comp = await _fresh_computer(tmp_path)  # 全新实例：声明可读、但未挂载
    assert not any(resolve_bundle_id(c) == _BUNDLE_ID for c in comp.mcp_manager.server_configs())

    out = await _run_repl(comp, [f"stop {_DISPLAY_NAME}"], monkeypatch, capsys)

    assert "停止完成" not in out, "已声明未挂载却打印停止成功 = 决策 1 引入的新假回执"
    assert "尚未挂载" in out or "not mounted" in out


@pytest.mark.asyncio
async def test_start_declared_but_unmounted_does_not_leak_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    """``start`` 对「已声明未挂载」须诚实陈述，MUST NOT 漏库层内部 ``Unknown server bundle_id=...``。"""
    _isolate(tmp_path, monkeypatch)
    seed = await _fresh_computer(tmp_path)
    await seed.aadd_or_aupdate_server_in_scope(_stdio_dict(_DISPLAY_NAME), McpWriteScope.PROJECT)

    comp = await _fresh_computer(tmp_path)
    out = await _run_repl(comp, [f"start {_DISPLAY_NAME}"], monkeypatch, capsys)

    assert "启动完成" not in out
    assert "Unknown server" not in out, "内部 bundle_id 概念不得漏给用户"
    assert "已声明但未挂载" in out or "declared but not mounted" in out


# ── 守卫：`all` 不进解析 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("cmd", "expected"), [("stop all", "所有服务器停止完成"), ("start all", "所有服务器启动完成")])
async def test_all_keyword_bypasses_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, cmd: str, expected: str,
) -> None:
    """``start|stop all`` 是关键字而非 server 标识 ⇒ 先短路，不进 resolve_target（否则会报「未找到 all」）。

    两个动词各有一份短路代码 ⇒ 各守一次（只守 stop 则 start 那份失守）。
    """
    _isolate(tmp_path, monkeypatch)
    comp = await _fresh_computer(tmp_path)
    await comp.amount_server(_stdio_dict(_DISPLAY_NAME))

    out = await _run_repl(comp, [cmd], monkeypatch, capsys)

    assert expected in out
    assert "未找到" not in out


# ── 库层 name 入口移除（验收信号取「函数不存在」，非文档降级）──────────────────────────────────


def test_library_has_no_name_addressed_unmount_entry() -> None:
    """R4: 库层公开 API 一律收 bundle_id，无 name 启发式 ⇒ ``aunmount_server(name)`` 必须**不存在**。

    验收信号取「函数不存在」而非「文档说别用」——Epic #147 教训：字段/函数降级为诊断 = 产出 P0 的模式，
    删掉才拦得住下一个实现者。零生产调用方（级联走 ``aunmount_server_by_id``）⇒ 纯删除。
    """
    assert not hasattr(Computer, "aunmount_server"), "库层 name 寻址入口应已删除（R4）"
    assert hasattr(Computer, "aunmount_server_by_id"), "bundle_id 停摘入口应保留"
