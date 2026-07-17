"""
文件名: test_cli_run_with_mcp_config.py
作者: JQQ
创建日期: 2025/9/22
版权: 2023 JQQ. All rights reserved.
依赖: pytest, pexpect
描述:
  中文: 启动 CLI 时通过 --mcp-config/-c 传入 flag 层 mcp.json，验证服务加载与工具可见。
  English: Pass --mcp-config (the flag-layer mcp.json) at startup and verify servers load and tools are visible.

  #154：旧 `--config`（收「裸 server 对象/数组」、绕开 scope 合并与审批门直挂）已更名为 `--mcp-config`
  且形状硬切为 mcp.json 的 `{servers, inputs}` —— 它现在是 flag scope 的 mcp.json（优先级次高、仅低于
  policy），与 `--settings`（flag 层 settings.json）构成 flag scope 的**文件对**（协议 §2.5-3）。

  注：REPL 面的 `server add @<file>` 仍吃**裸 server** 形状（另一条通路、另一套夹具），见
  `test_cli_mcp_config_start.py` / `test_cli_inputs_resolve.py` —— 本次不动它们。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import sys
import time
from contextlib import contextmanager

import pytest

from tests.e2e.computer.utils import strip_ansi

pexpect = pytest.importorskip("pexpect", reason="e2e tests require pexpect; install with `pip install pexpect`.")


# ANSI-aware prompt matching and helper to strip ANSI sequences
# 与 conftest/test_cli_interactive 保持一致，确保在包含控制序列/光标移动的终端下也能稳定匹配提示符
ANSI = r"(?:\x1b\[[0-?]*[ -/]*[@-~])*"
PROMPT_RE = re.compile(ANSI + r"a2c>" + ANSI)


@contextmanager
def _spawn_cli_with_args(*extra_args: str):
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")
    env.setdefault("PROMPT_TOOLKIT_DISABLE_BRACKETED_PASTE", "1")
    env.setdefault("TERM", "dumb")
    console_script = shutil.which("a2c-computer")
    if console_script:
        args = [console_script, "--no-color", "run", *extra_args]
    else:
        args = [
            sys.executable,
            "-c",
            "from a2c_smcp_cc.cli.main import main; main()",
            "--no-color",
            "run",
            *extra_args,
        ]
    # 计算与设置工作目录 / Compute and set working directory
    # 默认将工作目录设置为项目根目录（本文件位于 tests/e2e/conftest.py，向上两级即为项目根）
    # By default, set cwd to project root (this file lives at tests/e2e/conftest.py; go up two levels)
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    child = pexpect.spawn(args[0], args[1:], env=env, encoding="utf-8", timeout=25, cwd=project_root)
    try:
        child.setwinsize(24, 120)
    except Exception:
        pass
    try:
        yield child
    finally:
        if child.isalive():
            try:
                child.sendline("exit")
                child.expect([pexpect.EOF, "Bye"], timeout=5)
            except Exception:
                pass
        if child.isalive():
            try:
                child.kill(signal.SIGKILL)
            except Exception:
                pass


def _wait_prompt(child: pexpect.spawn, timeout: float = 15.0) -> None:
    child.expect(PROMPT_RE, timeout=timeout)


def _assert_tools(child: pexpect.spawn, name: str, retries: int = 10, delay: float = 1.0) -> None:
    for _ in range(retries):
        child.sendline("tools")
        _wait_prompt(child)
        out = strip_ansi((child.before or "").strip())
        if name in out:
            return
        time.sleep(delay)
    child.sendline("tools")
    _wait_prompt(child)
    out = strip_ansi((child.before or "").strip())
    assert name in out, f"tools 未包含 {name}. 输出:\n{out}"


def _assert_status(child: pexpect.spawn, server: str, retries: int = 8, delay: float = 0.8) -> None:
    for _ in range(retries):
        child.sendline("status")
        _wait_prompt(child)
        out = strip_ansi((child.before or "").strip())
        if server in out:
            return
        time.sleep(delay)
    child.sendline("status")
    _wait_prompt(child)
    out = strip_ansi((child.before or "").strip())
    assert server in out, f"status 未出现 {server}. 输出:\n{out}"


@pytest.mark.e2e
def test_run_with_mcp_config_param_loads_server() -> None:
    """
    启动参数含 --mcp-config @tests/e2e/computer/configs/mcp_flag_direct_execution.json，应能加载并启动服务：
    - 进入 a2c> 后检查 status 含 e2e.direct
    - tools 中包含 hello
    若自动启动存在延迟，调用一次 start all 作为补偿

    夹具键 `e2e.direct`（**非** `e2e-direct`）：`normalize_name` 折叠 `.`→`_` 但**不折叠 `-`** ⇒ `e2e-direct`
    的 bundle_id 恰等于自身、两概念不分叉（conformance §2.0 违规）。`e2e.direct` → bundle_id `e2e_direct`。

    保留 `--mcp-config=@...` 的**等号形**：这是全仓唯一钉住等号形解析的地方。

    ⚠️ **status 断言的是 bundle_id `e2e_direct`，不是 display name** —— `MCPServerManager.get_server_status()`
    刻意以 **bundle_id 为身份**返回（#129/#130/#131 BundleID 模型），而 `cli/utils.py` 把它渲染在标着 "Name"
    的列下。**旧夹具 `e2e-test` 看不见这个分叉**（`-` 不折叠 ⇒ name ≡ bundle_id ⇒ 同值致盲，正是 Epic #147
    「stub 同值陷阱」那一族）；换成分叉夹具后才暴露出来。「人机面该显示 display name」属 REPL 寻址/展示轴
    （#143 / #144），**不在本 Issue 范围**——此处如实断言**今日行为**，勿改成 `e2e.direct` 掩盖它。
    """
    cfg_arg = "--mcp-config=@tests/e2e/computer/configs/mcp_flag_direct_execution.json"
    with _spawn_cli_with_args(cfg_arg) as child:
        # 等横幅/提示符
        try:
            child.expect("Enter interactive mode, type 'help' for commands")
        except Exception:
            pass
        # 可能需要轻推以出现提示符
        for _ in range(5):
            try:
                _wait_prompt(child, timeout=5)
                break
            except pexpect.TIMEOUT:
                child.sendline("")
        else:
            _wait_prompt(child, timeout=10)

        # 若 auto-connect 未马上激活，补打一遍 start all
        child.sendline("start all")
        _wait_prompt(child)

        # 身份 = bundle_id（见 docstring：status 渲染 get_server_status() 的 bundle_id，非 display name）
        _assert_status(child, "e2e_direct", retries=10, delay=0.8)
        _assert_tools(child, "hello", retries=12, delay=1.0)
