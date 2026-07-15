"""
文件名: conftest.py
作者: JQQ
创建日期: 2025/9/22
最后修改日期: 2025/9/22
版权: 2023 JQQ. All rights reserved.
依赖: pytest, pexpect
描述:
  中文: e2e 测试公共夹具，负责启动与关闭 CLI 交互进程。
  English: Common fixtures for e2e tests to spawn and cleanup CLI interactive process.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest

from tests.e2e.computer.utils import PROMPT_RE, expect_prompt_stable

pexpect = pytest.importorskip("pexpect", reason="e2e tests require pexpect; install with `pip install pexpect`.")


@contextmanager
def _spawn_cli(*extra_args: str, cwd: str | None = None, cleanup_durable: bool = True) -> Iterator[pexpect.spawn]:
    """
    中文: 启动 CLI 交互进程，返回 pexpect child；确保在退出时清理。可通过 `cwd` 指定子进程工作目录，默认使用项目根目录。
    English: Spawn the CLI interactive process and ensure cleanup on exit. You can specify child process working directory via `cwd`;
        defaults to project root.

    ``cleanup_durable`` (#139)：退出时是否还原本次 durable `server add` 落下的 `.tfrobot`。**重启存活 e2e** 需在同一
    cwd 连续 spawn 两次、令第一个进程写下的声明**留存**给第二个进程读到，故对该场景传 ``False``（配合 tmp cwd，由
    pytest ``tmp_path`` 兜底清理，绝不污染仓库）。默认 ``True`` = 精确还原（既有 e2e 行为不变）。
    """
    print("spawn cli...")
    env = os.environ.copy()
    # 确保 Python 输出不被缓冲，便于 pexpect 捕获 / Unbuffered Python output for stable pexpect reads
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 降低 prompt_toolkit 的控制序列噪音（如 CPR），提升匹配稳定性
    # Reduce prompt_toolkit control sequences to stabilize matching
    env["PROMPT_TOOLKIT_NO_CPR"] = "1"
    env["PROMPT_TOOLKIT_DISABLE_BRACKETED_PASTE"] = "1"
    env["PROMPT_TOOLKIT_COMPLETE_STYLE"] = "column"
    env["PROMPT_TOOLKIT_EDITING_MODE"] = "emacs"
    env["PROMPT_TOOLKIT_MOUSE_SUPPORT"] = "0"
    env["PROMPT_TOOLKIT_ENABLE_SUSPEND"] = "0"
    # 使用最简终端，促使 prompt_toolkit 降级，减少 ANSI 控制序列
    # Use dumb TERM to reduce advanced terminal features
    env["TERM"] = "dumb"  # 使用最简单的终端类型
    # 禁用分页与固定列宽，进一步减少输出差异 / Disable pager and fix width to reduce variability
    env["PAGER"] = "cat"
    env["COLUMNS"] = "120"
    env["LINES"] = "24"
    env["NO_COLOR"] = "1"
    # 强制使用UTF-8编码 / Force UTF-8 encoding
    env["LC_ALL"] = "en_US.UTF-8"
    env["LANG"] = "en_US.UTF-8"
    # 优先使用已安装的 console script；否则回退到 python -c 调用 main()
    # Prefer console script if available; fallback to python -c main()
    console_script = shutil.which("a2c-computer")
    if console_script:
        args = [console_script, "--no-color", "run"]
    else:
        print("a2c-computer not found in shell")
        args = [
            sys.executable,
            "-c",
            "from a2c_smcp.computer.cli.main import main; main()",
            "--no-color",
            "run",
        ]
    if extra_args:
        args.extend(extra_args)

    # 计算与设置工作目录 / Compute and set working directory
    # 默认将工作目录设置为项目根目录（本文件位于 tests/e2e/conftest.py，向上两级即为项目根）
    # By default, set cwd to project root (this file lives at tests/e2e/conftest.py; go up two levels)
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    spawn_cwd = cwd or project_root

    # #137 ②：REPL `server add`/`rm` 现为 durable，落盘于 spawn_cwd/.tfrobot/{mcp.local.json,mcp.json}
    # （project/local 锚 cwd，#116）。e2e 默认 cwd=项目根、config 文件走相对路径（不便改 tmp cwd），故对两个 durable
    # 落点做**字节级快照**，测试结束**精确还原**至运行前状态——防污染仓库 + 防跨 spawn 残留被下个进程 boot 审批读到
    # （PENDING 提示导致 e2e 挂起，#110 同类死循环）。快照-还原对「仓库根已有真实 .tfrobot」亦安全（还原其原内容，
    # 绝不删/改用户既有文件）。
    tfrobot_dir = os.path.join(spawn_cwd, ".tfrobot")
    tfrobot_preexisted = os.path.isdir(tfrobot_dir)
    durable_targets = (os.path.join(tfrobot_dir, "mcp.local.json"), os.path.join(tfrobot_dir, "mcp.json"))

    def _snapshot(path: str) -> bytes | None:
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    durable_snapshot = {p: _snapshot(p) for p in durable_targets}

    print("a2c-computer starting...")
    # 保证每次发送前有一个时延保持稳定
    child = pexpect.spawn(args[0], args[1:], env=env, encoding="utf-8", timeout=60, cwd=spawn_cwd)
    # 控制窗口大小，减少 CPR 请求 / Set winsize to reduce CPR
    child.delaybeforesend = 0.1
    try:
        child.setwinsize(24, 120)
    except Exception:
        pass
    try:
        yield child
    finally:
        # 优雅退出; 若仍存活则强杀 / Try graceful exit then hard kill if needed
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
        # #137 ②：精确还原 durable 落点至运行前状态——本次新建则删、原有则写回原字节（对既有真实 .tfrobot 亦安全）。
        # #139：重启存活 e2e 传 cleanup_durable=False，令首个进程写下的声明留存给下个进程读到（tmp cwd 由 pytest 兜底清理）。
        if cleanup_durable:
            for path, original in durable_snapshot.items():
                if original is None:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    with open(path, "wb") as fh:
                        fh.write(original)
            # 本次新建的空 .tfrobot 目录一并清走（原有目录保留）。
            if not tfrobot_preexisted and os.path.isdir(tfrobot_dir):
                shutil.rmtree(tfrobot_dir, ignore_errors=True)


def _wait_ready(child: pexpect.spawn) -> None:
    """等待 CLI 启动横幅 + 稳定 `a2c>` 提示符（`cli_proc` 与 `cli_proc_factory` 共用）。"""
    print("a2c-computer started up")
    child.expect([r"Enter interactive mode, type 'help' for commands", PROMPT_RE])
    # 若匹配到横幅，则继续等待提示符 / If banner matched, then wait for prompt
    if (
        child.match
        and hasattr(child.match, "re")
        and child.match.re
        and getattr(child.match.re, "pattern", "").startswith("Enter interactive")
    ):
        pass  # fall through to wait prompt below
    # 等待提示符，并在必要时发送空回车触发刷新 / Wait for prompt, poke with empty enter if needed
    for _ in range(5):
        try:
            print("waiting for [a2c>]...")
            expect_prompt_stable(child, quiet=0.5, max_wait=5.0)
            break
        except pexpect.TIMEOUT:
            child.sendline("")
    else:
        child.expect(PROMPT_RE)
    child.sendline("")
    expect_prompt_stable(child, quiet=0.5, max_wait=12.0)


@pytest.fixture()
def cli_proc() -> Iterator[pexpect.spawn]:
    """
    中文: 提供一个已启动并等待在 `a2c>` 提示符的 CLI 进程。
    English: Provide a CLI process ready at `a2c>` prompt.
    """
    with _spawn_cli() as child:
        _wait_ready(child)
        yield child


@pytest.fixture()
def cli_proc_factory() -> Callable[..., AbstractContextManager[pexpect.spawn]]:
    """按需 spawn 一个**已就绪**的 CLI（可指定 cwd / extra_args / cleanup_durable）的工厂（#139）。

    重启存活 e2e 用它在**同一 cwd** 连续 spawn 两次、并对首个进程传 ``cleanup_durable=False``，令其 durable
    `server add` 写下的声明留存给第二个进程读到。用 tmp cwd（pytest ``tmp_path``）兜底清理、绝不污染仓库。
    """

    @contextmanager
    def _make(*extra_args: str, cwd: str | None = None, cleanup_durable: bool = True) -> Iterator[pexpect.spawn]:
        with _spawn_cli(*extra_args, cwd=cwd, cleanup_durable=cleanup_durable) as child:
            _wait_ready(child)
            yield child

    return _make
