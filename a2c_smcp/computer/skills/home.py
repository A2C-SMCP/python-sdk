# -*- coding: utf-8 -*-
# filename: home.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL Home 路径解析 + 安装目录布局（v0.2.1）
SKILL Home resolution + install-dir layout (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §4（本地安装位置判给 SDK）、
                      §9.2（Staging 隔离）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §2.3 / §5。

解析优先级（镜像 Claude Code「默认用户 home + env 覆盖」范式）/ Resolution priority:
1. 环境变量 ``A2C_SKILL_HOME``（显式覆盖；容器 / 多实例旋钮）/ env override.
2. ``$XDG_DATA_HOME/a2c/skills``（XDG_DATA_HOME 须为绝对路径，否则按 XDG 规范忽略）/ XDG-first.
3. ``~/.a2c/skills``（回退）/ fallback.

布局 / Layout（skill.md §4 推荐三级分组）：``<home>/<source>/<...>/<skill>/``
- mcp:         ``<home>/mcp/<server>/<skill>/``
- marketplace: ``<home>/marketplace/<repo>/<...>/<skill>/``
- user:        ``<home>/user/<skill>/``

跨用户隔离策略 / Cross-user isolation stance（对齐 Claude Code）：
A2C 默认把 SKILL Home 落到**每用户私有**路径（home / XDG），并在创建时用 ``0o700`` 防御性写，
把「不跨用户共享」交给 **OS 文件权限**保证——与 Claude Code 对 ``~/.claude`` 的姿态一致（CC 视
其为 OS 已保证的每用户私有边界，应用层不做属主 / 权限运行时校验，env 覆盖直接放行）。
**不**做 path 前缀 deny-list：黑名单对「他人 home / 共享可写目录」天然拦不全，且 CC 亦不采用。
A2C defaults SKILL Home to a per-user-private path and creates it ``0o700``; "no cross-user
sharing" is delegated to OS permissions — matching Claude Code's stance on ``~/.claude``. No
path-prefix deny-list (a blacklist can't catch "other home / shared-writable" and CC avoids it).

.. note::
   协议 skill.md §9.2 当前措辞为「MUST NOT 跨用户共享」。本实现按 CC 对齐采用「每用户默认 +
   ``0o700`` + 用户自负 env 覆盖」落实，**待**协议把该 MUST 校准为 SHOULD / 部署层指引（协议侧
   变更由协议维护者推进）。若未来需应用层硬强制（属主 + 权限位 + realpath + TOCTOU），另开专项。
   §9.2 currently says "MUST NOT cross-user share"; this impl realizes it CC-style and the
   protocol wording is pending a MUST→SHOULD/deployment-guidance calibration (driven protocol-side).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import resolve_xdg_first

logger = get_logger(__name__)

# env 覆盖键 / Env override key（镜像 CC ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` 范式）。
SKILL_HOME_ENV = "A2C_SKILL_HOME"
XDG_DATA_HOME_ENV = "XDG_DATA_HOME"

# 默认相对布局 / Default relative layout fragments.
_A2C_DATA_SUBPATH = ("a2c", "skills")
_DOTDIR_SUBPATH = (".a2c", "skills")

# workspace 登记工作目录内的 user 源 DropIn 子路径 / Per-workdir user-source DropIn subpath。
_WORKDIR_SKILL_SUBPATH = (".tfrobot", "skills")

# 每用户私有目录权限 / Per-user-private directory mode（防御性写，隔离交给 OS）。
SKILL_HOME_MODE = 0o700

# source namespace 目录名 / Source-namespace directory names（skill.md §4）。
SOURCE_MCP = "mcp"
SOURCE_MARKETPLACE = "marketplace"
SOURCE_USER = "user"


def resolve_skill_home(env: Mapping[str, str] | None = None) -> Path:
    """
    解析 SKILL Home 绝对路径 / Resolve the absolute SKILL Home path。

    优先级 ``A2C_SKILL_HOME`` > ``$XDG_DATA_HOME/a2c/skills`` > ``~/.a2c/skills``（见模块 docstring）。
    返回 ``resolve()`` 后的绝对路径；**不**创建目录（落盘由 staging 负责，见 :func:`ensure_skill_home`）。
    不做 path deny-list 校验——跨用户隔离按 CC 对齐交给 OS 权限（见模块 docstring「跨用户隔离策略」）。

    :param env: 环境映射，默认 ``os.environ``（便于测试注入）/ env mapping, defaults to ``os.environ``.
    """
    environ: Mapping[str, str] = os.environ if env is None else env

    override = environ.get(SKILL_HOME_ENV, "").strip()
    if override:
        # env 覆盖：最高优先级旋钮，绕过 XDG/fallback。/ env override: highest-priority knob.
        resolved = Path(override).expanduser().resolve()
        logger.debug("Resolved SKILL Home via %s → %s", SKILL_HOME_ENV, resolved)
        return resolved

    # 无覆盖：XDG-first（``$XDG_DATA_HOME/a2c/skills``）→ 回退 ``~/.a2c/skills``，复用共享解析。
    # No override: XDG-first via the shared resolver, fallback ``~/.a2c/skills``.
    resolved = resolve_xdg_first(
        environ,
        XDG_DATA_HOME_ENV,
        *_A2C_DATA_SUBPATH,
        fallback=Path.home().joinpath(*_DOTDIR_SUBPATH),
    )
    logger.debug("Resolved SKILL Home (XDG-first %s) → %s", XDG_DATA_HOME_ENV, resolved)
    return resolved


def ensure_skill_home(env: Mapping[str, str] | None = None) -> Path:
    """
    解析并创建 SKILL Home 目录 / Resolve and create the SKILL Home directory。

    解析同 :func:`resolve_skill_home`，随后 ``mkdir(parents=True, exist_ok=True)`` 并以
    :data:`SKILL_HOME_MODE`（``0o700``）防御性创建——保证**新建**的 home 为每用户私有（POSIX）。
    既存目录的权限不改（与 CC 一致，不对用户已有目录做 chmod）；env 覆盖到不可写位置时由 OS 的
    ``mkdir`` 自然 fail-fast。Windows 上 mode 基本被忽略，隔离由 per-user AppData ACL 承担。
    Resolve as :func:`resolve_skill_home`, then create ``0o700`` (defensive, per-user-private on
    POSIX). Pre-existing dirs are left untouched; unwritable targets fail-fast via OS ``mkdir``.

    供 staging 启动时建根使用 / Used by staging to materialize the root at startup.
    """
    home = resolve_skill_home(env)
    home.mkdir(mode=SKILL_HOME_MODE, parents=True, exist_ok=True)
    return home


def mcp_skill_dir(home: Path, server: str, skill: str) -> Path:
    """mcp 源安装目录 / mcp-source install dir：``<home>/mcp/<server>/<skill>/``。"""
    return home / SOURCE_MCP / server / skill


def marketplace_skill_dir(home: Path, repo: str, *inner: str) -> Path:
    """
    marketplace 源安装目录 / marketplace-source install dir：``<home>/marketplace/<repo>/<...inner>/``。

    ``inner`` 为仓库内命名空间路径段（通常以 ``<plugin>/<skill>`` 收尾），由 staging（#61）按物化树传入。
    ``inner`` are repo-internal namespace path segments (typically ending ``<plugin>/<skill>``).
    """
    return home.joinpath(SOURCE_MARKETPLACE, repo, *inner)


def user_skill_dir(home: Path, skill: str) -> Path:
    """user 源安装目录 / user-source install dir：``<home>/user/<skill>/``。"""
    return home / SOURCE_USER / skill


def user_dropin_root(home: Path) -> Path:
    """SKILL Home 内的 user 源 DropIn 发现根 / Global user-source DropIn root：``<home>/user/``。"""
    return home / SOURCE_USER


def workdir_skill_root(workdir: Path) -> Path:
    """
    workspace 登记工作目录的 user 源 DropIn 发现根 / Per-workdir user-source DropIn root。

    ``<workdir>/.tfrobot/skills/``——**就地发现、不 staging 进 SKILL Home**（与 marketplace clone 树相反，
    见 design-0.2.1-cli-marketplace-ux.md §5.0）。skill 属能力发现层：跨**全部已登记工作目录**全局并集、
    置最低优先级、**不随 active workdir 切换**（§2.3/§5.1）。
    ``<workdir>/.tfrobot/skills/`` — discovered **in place** (not staged into SKILL Home, unlike the
    marketplace clone tree). User skills form a workspace-global capability set across all registered
    workdirs (lowest precedence, independent of the active workdir).
    """
    return workdir.joinpath(*_WORKDIR_SKILL_SUBPATH)
