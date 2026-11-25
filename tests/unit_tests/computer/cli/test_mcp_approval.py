# -*- coding: utf-8 -*-
# filename: test_mcp_approval.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
启动期 MCP 批准框单元测试（v0.2.1 #69 Group B，§9.2）/ Boot-time MCP approval-box unit tests。

测试意图 / Test intentions（fake Computer 桩 + 内存 session；XDG 重定向隔离 user；policy monkeypatch 为空）:
- PENDING + TTY ``[y]`` → 写 local ``enabledMcpjsonServers`` + 挂载；**重启幂等**（二次 gate → ENABLED、不再弹）；
- PENDING + 非 TTY（session=None）→ skip+WARN、不挂、不写；``--approve-all-mcp`` → 挂载但**不落盘**；
- ENABLED（user origin trusted）→ 直挂；DISABLED（local disabled）→ 跳过；
- ``ext``（envFile）合回挂载 dict（否则 spawn 丢失）；``resolved.errors`` 呈现 WARN。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

import a2c_smcp.computer.cli.commands.plugin as plugin_cmd
from a2c_smcp.computer.cli.commands.plugin import run_mcp_approval
from a2c_smcp.computer.settings.mcp_config import resolve_mcp_config
from a2c_smcp.computer.settings.schema import SettingsScope, SettingsValidationError
from a2c_smcp.computer.settings.scope import user_settings_path, workdir_local_settings_path


@pytest.fixture(autouse=True)
def _no_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """policy 读取 OS 源不确定 → 测试中固定为空，保证确定性 / pin policy empty for determinism。

    fix-review #4 后 ``resolved_settings`` 移入 ``cli.commands.__init__``（函数内 lazy import policy），
    故 monkeypatch 须打到来源模块 ``settings.policy``（lazy import 会取到打过的属性）。
    """
    import a2c_smcp.computer.settings.policy as policy_mod

    monkeypatch.setattr(policy_mod, "resolve_policy_settings", lambda **_: {})


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _project_mcp(wd: Path, servers: dict[str, Any]) -> None:
    _write_json(wd / ".tfrobot" / "mcp.json", {"servers": servers, "inputs": []})


def _user_mcp(env: dict[str, str], servers: dict[str, Any]) -> None:
    _write_json(Path(env["XDG_CONFIG_HOME"]) / "a2c" / "mcp.json", {"servers": servers, "inputs": []})


class _Session:
    """内存 PromptSession 桩：按序返回预置答案 / in-memory prompt session returning canned answers。"""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def prompt_async(self, prompt: str, **_: Any) -> str:
        self.prompts.append(prompt)
        return self._answers.pop(0)


class _FakeComp:
    """Computer 桩：**声明面解析逐字复用生产实现**，只桩掉挂载副作用 / Stub that reuses the real declaration resolve。

    ``resolve_mcp_declarations`` **刻意不自己实现**，而是照 :meth:`Computer.resolve_mcp_declarations` 同款委托
    :func:`resolve_mcp_config`（同样透传 flag/embed 两条 boot 声明式输入）——否则桩会把「flag/embed 层不存在」
    这个前提固化进测试，令 #154/#164 的层序与 origin 条款**在此文件内永远测不到**（Epic #147 记载的 ``_FakeComputer``
    假绿正是此形状）。真实构造路径的契约测试见 ``tests/integration_tests/computer/cli/test_mcp_flag_config.py``（F7）。
    """

    def __init__(
        self,
        home: Path,
        *,
        mcp_flag_config: Path | None = None,
        mcp_servers: list[Any] | None = None,
    ) -> None:
        self.skill_home = home
        self.injected: list[Any] = []
        self.mounted: list[dict[str, Any]] = []
        self.unmounted: list[str] = []
        self.mcp_manager: Any = None  # pre-boot：无活跃集（与 CLI 首次 boot 同形）
        self._mcp_flag_config = mcp_flag_config
        self._mcp_servers = list(mcp_servers or [])

    def resolve_mcp_declarations(self, *, env: Any = None) -> Any:
        return resolve_mcp_config(env=env, flag_config_path=self._mcp_flag_config, embed_servers=self._mcp_servers)

    def add_or_update_input(self, inp: Any) -> None:
        self.injected.append(inp)

    def list_inputs(self) -> tuple[Any, ...]:
        # 累积池：#155 的坍缩前检读它。注意本桩的 add_or_update_input **永不抛**（真 Computer 会），
        # 故「注入失败」类契约在本文件**测不到**——其真实构造路径守卫见
        # tests/integration_tests/computer/cli/test_mcp_flag_config.py（F7）。
        return tuple(self.injected)

    async def amount_server(
        self, cfg: dict[str, Any], *, session: Any = None, plugin: Any = None, marketplace: Any = None,
    ) -> None:
        # #137 ③：run_mcp_approval 走 transient amount_server（boot 读已声明 mcp.json = 投影，不回写）。
        self.mounted.append(cfg)

    async def aunmount_server_by_id(self, bundle_id: str) -> None:
        self.unmounted.append(bundle_id)


def _mounted_names(comp: _FakeComp) -> set[str]:
    return {str(m.get("name")) for m in comp.mounted}


def _stdio() -> dict[str, Any]:
    return {"type": "stdio", "server_parameters": {"command": "node"}}


# ── PENDING + TTY [y] → 写 local + 挂载 + 重启幂等 ──────────────────────────────
@pytest.mark.asyncio
async def test_pending_approve_yes_writes_local_and_mounts_then_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"shared": _stdio()})  # project origin → 非 trusted → PENDING
    monkeypatch.chdir(wd)  # #116：project/local 层锚定进程 cwd

    comp = _FakeComp(home)
    sess = _Session(["y"])
    # run_mcp_approval 读 os.environ 派生 XDG → 用 monkeypatch env 注入
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, sess, approve_all=False, settings_flag_path=None)
    assert "shared" in _mounted_names(comp)  # [y] → 挂载
    local = json.loads(workdir_local_settings_path(wd).read_text(encoding="utf-8"))
    assert local["enabledMcpjsonServers"] == ["shared"]  # 写 local scope（cwd 单根）

    # 重启幂等：二次启动 gate 读到 local enabled → ENABLED 直挂、不再弹（session 无答案也不报错）
    comp2 = _FakeComp(home)
    sess2 = _Session([])
    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp2, sess2, approve_all=False, settings_flag_path=None)
    assert "shared" in _mounted_names(comp2)
    assert sess2.prompts == []  # 不再弹批准框


# ── PENDING + 非 TTY → skip+WARN（不挂不写）；--approve-all-mcp → 挂载不落盘 ──────
@pytest.mark.asyncio
async def test_pending_non_tty_skips_and_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"shared": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=False, settings_flag_path=None)  # session=None = 非 TTY
    assert _mounted_names(comp) == set()  # 未挂
    assert not workdir_local_settings_path(wd).exists()  # 未写


@pytest.mark.asyncio
async def test_pending_non_tty_approve_all_mounts_without_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"shared": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)
    assert "shared" in _mounted_names(comp)  # --approve-all-mcp → 挂载
    assert not workdir_local_settings_path(wd).exists()  # 仅本次、不落盘


# ── ENABLED（user origin trusted）直挂 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_user_origin_mounts_without_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    monkeypatch.chdir(wd)  # #116：project/local 锚 cwd——chdir 到空 wd，隔离真实仓库 .tfrobot/（与 PENDING 用例一致）
    _user_mcp(env, {"mine": _stdio()})  # user origin → trusted_origin → ENABLED、免批准
    comp = _FakeComp(home)
    sess = _Session([])  # 不应被调用
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, sess, approve_all=False, settings_flag_path=None)
    assert "mine" in _mounted_names(comp)
    assert sess.prompts == []


# ── ext（envFile）合回挂载 dict（#69 Group B 风险 2）─────────────────────────────
@pytest.mark.asyncio
async def test_envfile_ext_merged_into_mount_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    monkeypatch.chdir(wd)  # #116：project/local 锚 cwd——chdir 到空 wd，隔离真实仓库 .tfrobot/（与 PENDING 用例一致）
    envfile = str(wd / ".env")
    _user_mcp(env, {"mine": {**_stdio(), "envFile": envfile}})  # user origin → 直挂
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, _Session([]), approve_all=False, settings_flag_path=None)
    mine = next(m for m in comp.mounted if m.get("name") == "mine")
    assert mine.get("envFile") == envfile  # ext 合回，否则 spawn 丢 envFile


# ── resolved.errors 呈现 WARN ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_malformed_server_surfaces_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    # 畸形 server（缺 type）→ drop + error；valid 同存
    _project_mcp(wd, {"bad": {"server_parameters": {}}, "good": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)
    out = capsys.readouterr().out
    assert "mcp.json" in out  # resolved.errors 呈现（被 drop 的 bad 不静默）


# ── #157：project scope 自我批准 —— 拦下 + 响亮失败 ─────────────────────────────
@pytest.mark.asyncio
async def test_project_self_approval_filtered_and_surfaced_at_boot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被 clone 仓库携 project settings.json 自我批准 → **不挂载**（非 TTY 无 approve_all）+ **打印解释**。

    这是 #157 的用户侧终局断言：过滤生效（安全）**且**用户看得懂为何自己的 settings 没生效（协议 §2.1
    「响亮失败」）。只断言过滤、不断言呈现 = 文案写进黑洞的假绿。
    End-to-end: the self-approval is filtered AND the user is told why.
    """
    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"evil": _stdio()})
    _write_json(wd / ".tfrobot" / "settings.json", {"enableAllProjectMcpServers": True})  # ← 入 git
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    import unittest.mock as _m

    with _m.patch.dict(os.environ, env, clear=False):
        # session=None（非 TTY）且未 --approve-all-mcp → 若自我批准生效会直挂；被过滤后应 skip+WARN。
        await run_mcp_approval(comp, None, approve_all=False, settings_flag_path=None)

    assert _mounted_names(comp) == set(), "project 自我批准 MUST NOT 让恶意 server 直挂"
    out = capsys.readouterr().out
    assert "enableAllProjectMcpServers" in out  # 越权字段被点名
    assert "settings.local.json" in out  # 且给出可操作去向


# ── #294 复审：启动展示行的 markup 安全（可达崩溃）─────────────────────────────
def _no_ws(text: str) -> str:
    """去掉全部空白：rich 会按终端宽度折行，人类输出断言不得依赖换行位置。"""
    return "".join(text.split())


@pytest.mark.asyncio
async def test_settings_error_line_escapes_markup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    #294 复审实证的**可达崩溃**：手编 `extraKnownMarketplaces` 键即可让 `format_settings_errors` 的
    `field` 含 `[/]`；该行此前未 `escape()` ⇒ rich 抛 `MarkupError`，`run_mcp_approval` 直接中断
    （**跳过整个审批门**）。本用例断言：不抛、且原文逐字可见、scope 文本不被当标签吞掉。

    可达前提：`.tfrobot/mcp.json` 有 ≥1 个 server（否则 `run_mcp_approval` 早退）。
    """
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"good": _stdio()})
    _write_json(
        Path(env["XDG_CONFIG_HOME"]) / "a2c" / "settings.json",
        {"extraKnownMarketplaces": {"evil[/]a[/]b": {}}},
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)  # 不得抛 MarkupError

    out = _no_ws(capsys.readouterr().out)
    assert "settings.json[user]" in out  # scope 文本没被当标签吃掉
    assert "evil[/]a[/]b" in out  # field 原文逐字可见


@pytest.mark.asyncio
async def test_settings_error_line_redacts_marketplace_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#295：`run_mcp_approval` 消费同一 `format_settings_errors`——错误行不得回显 marketplace 键里的凭据。

    escape 由 #294 已加（同族用例见上），本用例钉**脱敏**轴：userinfo 剥离、结构仍可定位。
    """
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"good": _stdio()})
    _write_json(
        Path(env["XDG_CONFIG_HOME"]) / "a2c" / "settings.json",
        {"extraKnownMarketplaces": {"https://alice:s3cret@example.com/repo.git": {}}},
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    assert "s3cret" not in out  # 凭据不落
    assert "https://example.com/repo.git" in out  # userinfo 剥离但名字仍可定位


# ── #295 隔离审查 🔴：审批门 server 名展示行的 markup 安全（既存缺陷，非本单引入）──────────
@pytest.mark.parametrize("mode", ["approve_all", "non_tty", "disabled"])
@pytest.mark.asyncio
async def test_approval_lines_escape_markup_in_server_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """
    既存缺陷（非本单引入）：`.tfrobot/mcp.json` 的 server 名是裸 `str`（无字符集约束），仓库可分发
    `evil[/]x` 令审批门在展示行抛 `MarkupError`；`interactive_impl` 的 `except Exception` 会把**整门**
    降级为一行 WARN ⇒ 恶意名之后的 server 不再挂载/摘除/提示。

    三景分别钉住：`--approve-all-mcp` 的 mounted 行、非 TTY 的 skipped 行、DISABLED 的 skipped 行。
    恶意名刻意排在字典首位——若中断，`'good'` 不会出现在输出里。
    """
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"evil[/]x": _stdio(), "good": _stdio()})
    if mode == "disabled":
        _write_json(workdir_local_settings_path(wd), {"disabledMcpjsonServers": ["evil[/]x"]})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=mode == "approve_all", settings_flag_path=None)  # 不得抛

    out = _no_ws(capsys.readouterr().out)
    assert "evil[/]x" in out  # 原文逐字可见
    assert "'good'" in out  # 恶意名之后仍继续处理（整门未被中断）
    if mode == "approve_all":
        assert "evil[/]x" in _mounted_names(comp)


@pytest.mark.parametrize("name", ["evil[/]x", "shared"])
@pytest.mark.asyncio
async def test_approval_prompt_shows_repr_server_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    """#295：审批 prompt 改用 `{name!r}`——干净名渲染与旧 `'{name}'` 逐字一致，控制字符不再可伪造提示行。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {name: _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    sess = _Session(["y"])

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, sess, approve_all=False, settings_flag_path=None)

    assert sess.prompts[0].startswith("⚠ Unapproved workspace MCP server ")
    assert f"'{name}'" in sess.prompts[0]  # repr 形态：干净名与旧写法一致，markup 名不被解析
    assert name in _mounted_names(comp)  # [y] 后挂载成功（mounted 展示行同样不抛）


@pytest.mark.asyncio
async def test_input_collision_line_escapes_markup_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复审 🔴：`_inject_inputs_collision_safe` 的 id 冲突行在**审批门循环之前**执行。

    `.tfrobot/mcp.json` 可声明两个 `[/]` 形态 id——`env_segment` 把它们坍缩到同一环境变量名，冲突行的
    `detail` 含两处 `[/]`：未转义时 rich 抛 `MarkupError` ⇒ 异常逸出 `run_mcp_approval` ⇒ 整门跳过。
    """
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _write_json(
        wd / ".tfrobot" / "mcp.json",
        {
            "servers": {"good": _stdio()},
            "inputs": [
                {"id": "evil[/]a-b", "description": "x"},
                {"id": "evil[/]a_b", "description": "y"},
            ],
        },
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)  # 不得抛

    out = _no_ws(capsys.readouterr().out)
    assert "evil[/]a-b" in out and "evil[/]a_b" in out  # 冲突双方原文可见
    assert "'good'" in out  # 后续 server 仍被处理（整门未中断）


@pytest.mark.asyncio
async def test_approval_mount_failure_line_escapes_markup_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复审 🟡：挂载失败行的 `{exc}` 若含 markup，未 escape 时兜底打印自身再抛（`except` 内不得可抛）。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"good": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    async def _boom(_cfg: Any, session: Any = None) -> None:
        raise RuntimeError("boom[/]x")

    monkeypatch.setattr(comp, "amount_server", _boom)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)  # 不得抛

    out = _no_ws(capsys.readouterr().out)
    assert "boom[/]x" in out  # 异常原文可见


@pytest.mark.asyncio
async def test_approval_unmount_failure_line_escapes_markup_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复审 🟡3：DISABLED 分支的摘除失败行同样有 `{exc}` 插值——此前只有 mount 行被锁。

    构造：server 已在活跃集（`mcp_manager.server_configs()` 含其 config）且 local 声明 disabled ⇒
    `_ensure_unmounted` 走摘除分支；`aunmount_server_by_id` 抛含 markup 的异常。
    """
    import unittest.mock as _m
    from types import SimpleNamespace

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"evil[/]x": _stdio(), "good": _stdio()})
    _write_json(workdir_local_settings_path(wd), {"disabledMcpjsonServers": ["evil[/]x"]})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    async def _boom(_bid: str) -> None:
        raise RuntimeError("boom[/]x")

    monkeypatch.setattr(comp, "aunmount_server_by_id", _boom)

    with _m.patch.dict(os.environ, env, clear=False):
        decl = comp.resolve_mcp_declarations(env=env)
        comp.mcp_manager = SimpleNamespace(server_configs=lambda: [decl.servers["evil[/]x"].config])
        await run_mcp_approval(comp, None, approve_all=False, settings_flag_path=None)  # 不得抛

    out = _no_ws(capsys.readouterr().out)
    assert "boom[/]x" in out  # 异常原文可见
    assert "\\[" not in out  # 无双重转义残留（escape 只做一次，rich 已还原）


@pytest.mark.asyncio
async def test_approval_prompt_escapes_control_chars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复审 🟡：prompt 的 `{name!r}` 须把换行/控制字符转义成字面 `\\n`，不能伪造提示行。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"evil\nx": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    sess = _Session(["y"])

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, sess, approve_all=False, settings_flag_path=None)

    prompt = sess.prompts[0]
    assert "evil\\nx" in prompt  # repr 形态：字面反斜杠 + n
    assert "evil\nx" not in prompt  # 裸换行不得出现在提示文本里


@pytest.mark.asyncio
async def test_approval_deny_line_escapes_markup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复审 🟡：deny 分支的展示行同样须 escape（此前无覆盖）。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"evil[/]x": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, _Session(["n"]), approve_all=False, settings_flag_path=None)  # 不得抛

    out = _no_ws(capsys.readouterr().out)
    assert "deniedMCPserver'evil[/]x'" in out  # 文案与文字均可见


@pytest.mark.asyncio
async def test_mcp_json_error_line_escapes_markup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同族兄弟行：`mcp.json` 的错误文本里含用户可编 server 名，同样必须 `escape()`。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"bad[/]name": {"server_parameters": {}}, "good": _stdio()})  # 缺 type → drop + error
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)  # 不得抛 MarkupError

    out = _no_ws(capsys.readouterr().out)
    assert "mcp.json" in out
    assert "bad[/]name" in out  # 原文逐字可见


@pytest.mark.asyncio
async def test_approval_mount_failure_line_redacts_pydantic_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#289 + #295 合并面：失败行的异常文本走 `exception_text`——pydantic `input_value`（渲染后 secret）
    不得回显（独立审查 🟡2）。"""
    import unittest.mock as _m

    from pydantic import BaseModel

    class _Payload(BaseModel):
        token: int  # 传字符串触发 int_parsing，input_value 就是 secret

    def _validation_error() -> BaseException:
        try:
            _Payload(token="SUPER_SECRET_TOKEN")  # type: ignore[arg-type]
        except Exception as exc:
            return exc
        raise AssertionError("expected ValidationError")

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"good": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)
    exc = _validation_error()

    async def _boom(_cfg: Any, session: Any = None) -> None:
        raise exc

    monkeypatch.setattr(comp, "amount_server", _boom)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)  # 不得抛

    out = _no_ws(capsys.readouterr().out)
    assert "SUPER_SECRET_TOKEN" not in out  # input_value 不回显
    assert "validationerror(s)in[token:int_parsing]" in out  # 仍有可定位的摘要（exception_text 形态）


# ── #304 item 2：mcp.json 摘要行 / DEBUG 明细 ──────────────────────────────────
_MCP_MISMATCH_REASON = (
    "server 'name' field 'alice:pw@mp' != map key 'clean' (the map key is the canonical identity)"
)


def test_mcp_declaration_error_line_redacts_server_name_credentials() -> None:
    """摘要行的 field 残段**整值**过名字漏斗；source 字段不进摘要。"""
    line = plugin_cmd.mcp_declaration_error_line(
        SettingsValidationError(
            scope=SettingsScope.PROJECT,
            field="servers.alice:pw@mp",
            reason="server definition must be an object",
            source_path="/proj/.tfrobot/mcp.json",
        )
    )

    assert line == "⚠ mcp.json[project]: servers.<redacted-git-source> — server definition must be an object"
    assert "alice:" not in line
    assert "source_path" not in line


def test_mcp_declaration_error_line_redacts_name_embedded_in_reason() -> None:
    """reason 里的原始 ``name`` 值（``mcp_config`` 的 name-mismatch 文案）也必须过散文漏斗。"""
    line = plugin_cmd.mcp_declaration_error_line(
        SettingsValidationError(scope=SettingsScope.PROJECT, field="servers.clean", reason=_MCP_MISMATCH_REASON)
    )

    assert "alice:" not in line and "pw@mp" not in line
    assert "<redacted-git-source>" in line
    assert "servers.clean" in line  # 干净 map key 仍可定位


def test_mcp_declaration_error_line_preserves_clean_server_name() -> None:
    line = plugin_cmd.mcp_declaration_error_line(
        SettingsValidationError(
            scope=SettingsScope.PROJECT, field="servers.bad[/]name", reason="server definition must be an object"
        )
    )

    assert line == "⚠ mcp.json[project]: servers.bad[/]name — server definition must be an object"


def test_mcp_declaration_error_line_keeps_constant_field() -> None:
    line = plugin_cmd.mcp_declaration_error_line(
        SettingsValidationError(scope=SettingsScope.USER, field="<file>", reason="unreadable or corrupt JSON: x")
    )

    assert line.startswith("⚠ mcp.json[user]: <file> — ")


def test_mcp_declaration_error_detail_sanitizes_all_four_segments() -> None:
    detail = plugin_cmd.mcp_declaration_error_detail(
        SettingsValidationError(
            scope=SettingsScope.USER,
            field="servers.alice:pw@mp",
            reason=_MCP_MISMATCH_REASON,
            source_path="alice%3Apw%40host-mcp.json",
        )
    )

    assert "scope=user" in detail
    assert "servers.<redacted-git-source>" in detail
    assert "source_path=<redacted-git-source>" in detail
    assert "alice%3A" not in detail
    assert "pw@mp" not in detail


def test_mcp_declaration_error_detail_scrubs_bidi_and_marks_missing_source() -> None:
    detail = plugin_cmd.mcp_declaration_error_detail(
        SettingsValidationError(
            scope=SettingsScope.USER,
            field="servers.good\u202emp",
            reason="server definition must be an object",
            source_path=None,
        )
    )

    assert "\u202e" not in detail
    assert "source_path=<unknown>" in detail


# ── #304 item 2：真实 sink（console 摘要 + DEBUG 明细） ────────────────────────
@pytest.mark.asyncio
async def test_mcp_json_error_line_redacts_server_name_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"alice:pw@mp": {"server_parameters": {}}, "good": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    assert "mcp.json[project]" in out
    assert "alice:" not in out and "pw@mp" not in out
    assert "servers.<redacted-git-source>" in out


@pytest.mark.asyncio
async def test_mcp_json_error_reason_redacts_name_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reason 轴：map key 干净、server 体 ``name`` 是凭据形态 → name-mismatch 文案里的原始值被掩蔽。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(
        wd,
        {"clean": {"type": "stdio", "server_parameters": {"command": "node"}, "name": "alice:pw@mp"}},
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    assert "clean" in out
    assert "alice:" not in out and "pw@mp" not in out
    assert "<redacted-git-source>" in out


@pytest.mark.asyncio
async def test_mcp_json_error_never_echoes_credential_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """apiKey / env / args 秘值既不进摘要行、也不进 DEBUG 明细（钉 ``pydantic_error_text`` 不回归）。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(
        wd,
        {
            "evil": {
                "type": "stdio",
                "command": "npx",
                "args": ["ARG_SECRET_X"],
                "env": {"API_KEY": "ENV_SECRET_X"},
                "apiKey": "APIKEY_SECRET_X",
            }
        },
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    src = plugin_cmd.logger
    prev_level = src.level
    src.setLevel(logging.DEBUG)
    src.addHandler(caplog.handler)
    try:
        with _m.patch.dict(os.environ, env, clear=False):
            await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)
    finally:
        src.removeHandler(caplog.handler)
        src.setLevel(prev_level)

    combined = capsys.readouterr().out + caplog.text
    for secret in ("ARG_SECRET_X", "ENV_SECRET_X", "APIKEY_SECRET_X"):
        assert secret not in combined, f"凭据外泄: {secret}"


@pytest.mark.asyncio
async def test_mcp_json_error_detail_is_logged_at_debug(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """完整明细（scope / field / reason / 脱敏 source）走 DEBUG，且 console 只留摘要。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"bad": {"server_parameters": {}}, "good": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    src = plugin_cmd.logger
    prev_level = src.level
    src.setLevel(logging.DEBUG)
    src.addHandler(caplog.handler)
    try:
        with _m.patch.dict(os.environ, env, clear=False):
            await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)
    finally:
        src.removeHandler(caplog.handler)
        src.setLevel(prev_level)

    details = [r.getMessage() for r in caplog.records if "mcp.json declaration error detail" in r.getMessage()]
    assert len(details) == 1
    assert "scope=project" in details[0]
    assert "field=servers.bad" in details[0]
    assert ".tfrobot/mcp.json" in details[0]  # 干净 source 可定位（脱敏后原样）


@pytest.mark.asyncio
async def test_mcp_json_error_detail_stays_off_console_at_info_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """INFO 级对照：明细**不得**产出任何日志记录（杀 debug→info 变异）。

    项目 logger 的 console handler 在 import 时绑定原始 ``sys.stdout``，``capsys`` 看不到它的输出——
    故不能断言 stdout，直接把 ``caplog.handler`` 挂到模块 logger，在 INFO 级别下断言零条明细记录。
    """
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(wd, {"bad": {"server_parameters": {}}, "good": _stdio()})
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    src = plugin_cmd.logger
    prev_level = src.level
    src.setLevel(logging.INFO)
    src.addHandler(caplog.handler)
    try:
        with _m.patch.dict(os.environ, env, clear=False):
            await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)
    finally:
        src.removeHandler(caplog.handler)
        src.setLevel(prev_level)

    details = [r for r in caplog.records if "mcp.json declaration error detail" in r.getMessage()]
    assert details == []
    out = _no_ws(capsys.readouterr().out)
    # debug→console.print 变异：详情会直接写 stdout——摘要行不含 `source_path=`，出现即明细上屏。
    assert "source_path=" not in out
    assert "mcp.json[project]" in out  # 摘要行仍在（正对照）


@pytest.mark.asyncio
async def test_mcp_json_error_reason_redacts_whitespace_split_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#304 审查 🔴：name-mismatch 内嵌的原始值即使**被空白劈开**（``alice:my pw@corp``）也必须 fail-closed.

    散文漏斗按空白分词，两半都不命中 userinfo 形态；修法在 ``mcp_config`` **构造 reason 处**走段漏斗。
    """
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(
        wd,
        {"clean": {"type": "stdio", "server_parameters": {"command": "node"}, "name": "alice:my pw@corp"}},
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    assert "alice:my" not in out and "pw@corp" not in out
    assert "<redacted-git-source>" in out


@pytest.mark.asyncio
async def test_mcp_json_error_loc_redacts_whitespace_split_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pydantic ``extra_forbidden`` 的 loc 段含手编 key → 摘要里的 loc 逐段过名字漏斗（同被空白劈开的向量）。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(
        wd,
        {"evil": {"type": "stdio", "server_parameters": {"command": "node"}, "alice:my pw@corp": 1}},
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    assert "alice:my" not in out and "pw@corp" not in out
    assert "<redacted-git-source>" in out


@pytest.mark.asyncio
async def test_mcp_json_error_line_redacts_input_id_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``inputs.<id>`` 轴的 field 残段同样过名字漏斗（钉 ``MCP_DECLARATION_FIELD_PREFIXES`` 覆盖 inputs）。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _write_json(
        wd / ".tfrobot" / "mcp.json",
        {"servers": {"good": _stdio()}, "inputs": [{"id": "alice:pw@mp"}]},  # 缺必填字段 → invalid
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    assert "alice:" not in out and "pw@mp" not in out
    assert "inputs.<redacted-git-source>" in out


@pytest.mark.asyncio
async def test_mcp_json_error_file_reason_omits_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``<file>`` 的 OSError 文案不再内嵌文件路径（#304 审查 🔴 第三路径）.

    路径可由 env / cwd 控制，展示侧的散文漏斗回收不了「被空白劈开」的凭据；故在 ``mcp_config``
    **构造 reason 处**只留 ``strerror``——定位由 DEBUG 明细里的脱敏 ``source_path`` 承载。
    """
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    (wd / ".tfrobot" / "mcp.json").mkdir(parents=True)  # 目录 → IsADirectoryError
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    # 平台无关：仅断言 reason 前缀在场、且路径不回显（Windows 上对目录 read_text 抛 PermissionError，
    # 不该把 POSIX 专有的 "Is a directory" 文案写进断言）。
    assert "unreadableorcorruptJSON:" in out
    assert _no_ws(str(wd)) not in out  # 路径不进 reason（source 只进 DEBUG 且已脱敏）


@pytest.mark.asyncio
async def test_mcp_json_error_input_loc_redacts_extra_key_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_validate_input`` 的 pydantic loc 同样逐段过名字漏斗（inputs 额外键向量）。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _write_json(
        wd / ".tfrobot" / "mcp.json",
        {
            "servers": {"good": _stdio()},
            "inputs": [{"id": "ok", "type": "promptString", "description": "d", "alice:my pw@corp": 1}],
        },
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    assert "alice:my" not in out and "pw@corp" not in out
    assert "<redacted-git-source>" in out


@pytest.mark.asyncio
async def test_mcp_json_error_non_string_name_value_not_echoed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非字符串 ``name``（如 ``{"token": ...}``）不 echo 值——嵌套裸 token 不在名字漏斗可识别形态内。"""
    import unittest.mock as _m

    wd, home, env = tmp_path / "proj", tmp_path / "home", _env(tmp_path)
    wd.mkdir()
    home.mkdir()
    _project_mcp(
        wd,
        {"clean": {"type": "stdio", "server_parameters": {"command": "node"}, "name": {"token": "ghp_SECRET"}}},
    )
    monkeypatch.chdir(wd)
    comp = _FakeComp(home)

    with _m.patch.dict(os.environ, env, clear=False):
        await run_mcp_approval(comp, None, approve_all=True, settings_flag_path=None)

    out = _no_ws(capsys.readouterr().out)
    assert "ghp_SECRET" not in out
    assert "<non-string>" in out
