# -*- coding: utf-8 -*-
# filename: home.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL Home 路径解析 + 隔离 fail-fast + 安装目录布局（v0.2.1）
SKILL Home resolution + isolation fail-fast + install-dir layout (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §4（本地安装位置判给 SDK）、
                      §9.2（Staging 隔离：MUST NOT 跨用户共享 / 不放系统目录）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §2.3 / §5。

解析优先级（镜像 Claude Code「默认用户 home + env 覆盖」范式）/ Resolution priority:
1. 环境变量 ``A2C_SKILL_HOME``（显式覆盖；容器 / 多实例旋钮）/ env override.
2. ``$XDG_DATA_HOME/a2c/skills``（XDG_DATA_HOME 须为绝对路径，否则按 XDG 规范忽略）/ XDG-first.
3. ``~/.a2c/skills``（回退）/ fallback.

布局 / Layout（skill.md §4 推荐三级分组）：``<home>/<source>/<...>/<skill>/``
- mcp:         ``<home>/mcp/<server>/<skill>/``
- marketplace: ``<home>/marketplace/<repo>/<...>/<skill>/``
- user:        ``<home>/user/<skill>/``

隔离铁律 / Isolation invariant（skill.md §9.2）：SKILL Home **MUST NOT** 跨用户共享，不放系统
目录（如 ``/var/lib/a2c-skills``）。:func:`resolve_skill_home` 在解析时校验，违反即 fail-fast
抛 :class:`SkillHomeIsolationError`（启动期暴露，绝不静默落到系统目录）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import is_within

logger = get_logger(__name__)

# env 覆盖键 / Env override key（镜像 CC ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` 范式）。
SKILL_HOME_ENV = "A2C_SKILL_HOME"
XDG_DATA_HOME_ENV = "XDG_DATA_HOME"

# 默认相对布局 / Default relative layout fragments.
_A2C_DATA_SUBPATH = ("a2c", "skills")
_DOTDIR_SUBPATH = (".a2c", "skills")

# source namespace 目录名 / Source-namespace directory names（skill.md §4）。
SOURCE_MCP = "mcp"
SOURCE_MARKETPLACE = "marketplace"
SOURCE_USER = "user"

# 禁止的系统 / 跨用户共享前缀 —— SDK 自决的保守 deny-list（非穷举），落实 skill.md §9.2
# 「不放系统目录」意图。注意：**不**列入 ``/tmp`` 与 macOS ``/private/var/folders``（约定上按
# 调用 / 用户隔离的临时根），以免误伤测试夹具与合法的容器临时 home。
# Forbidden system / cross-user-shared prefixes — SDK-defined conservative deny-list (not
# exhaustive) realizing skill.md §9.2 "no system dirs". Deliberately excludes ``/tmp`` and
# macOS ``/private/var/folders`` (conventionally per-invocation/per-user temp roots) to avoid
# breaking test fixtures and legitimate ephemeral container homes.
_FORBIDDEN_SYSTEM_PREFIXES: tuple[Path, ...] = (
    Path("/usr"),
    Path("/etc"),
    Path("/opt"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/boot"),
    Path("/sys"),
    Path("/proc"),
    Path("/dev"),
    Path("/run"),
    Path("/srv"),
    Path("/var/lib"),
    Path("/var/cache"),
    Path("/var/log"),
    Path("/var/run"),
    Path("/var/spool"),
)


class SkillHomeError(Exception):
    """SKILL Home 解析 / 校验失败基类 / Base for SKILL Home resolution & validation failures."""


class SkillHomeIsolationError(SkillHomeError):
    """
    SKILL Home 违反隔离铁律（系统 / 跨用户共享目录）/ SKILL Home violates the isolation invariant.

    协议依据 skill.md §9.2。启动期 fail-fast，交由用户改用每用户独立、非系统共享的目录。
    Protocol skill.md §9.2. Fail-fast at startup; the user must point to a per-user, non-system dir.
    """


def _strip_macos_private(path: Path) -> Path:
    """
    macOS 去 ``/private`` 别名 / Strip the macOS ``/private`` alias。

    macOS 上 ``/var`` ``/etc`` ``/tmp`` 实为 ``/private/var`` 等的符号链接，``Path.resolve()`` 会展开
    为 ``/private/...``。为让系统前缀 deny-list（以 canonical ``/var`` 等表达）对解析后路径仍生效，
    比对前先剥掉前导 ``/private`` 段。非 macOS 或无该前缀时为恒等变换。
    On macOS ``/var`` etc. are symlinks under ``/private``; ``resolve()`` expands to ``/private/...``.
    Strip a leading ``/private`` so the canonical deny-list still matches; identity otherwise.
    """
    parts = path.parts
    if len(parts) >= 2 and parts[0] == os.sep and parts[1] == "private":
        return Path(os.sep, *parts[2:])
    return path


def validate_skill_home_isolation(path: Path) -> None:
    """
    校验隔离铁律，违反即抛 :class:`SkillHomeIsolationError` / Validate isolation invariant; raise on violation.

    判废条件 / Rejected when the resolved absolute path is:
    - 文件系统根 ``/`` 本身 / the filesystem root itself；
    - 等于或位于 :data:`_FORBIDDEN_SYSTEM_PREFIXES` 任一系统 / 跨用户共享前缀之下 /
      equal to or nested under any forbidden system / shared prefix.

    :param path: **已 resolve 的绝对路径** / an already-resolved absolute path.
    """
    # 先剥 macOS /private 别名再判定，确保根 / 系统前缀对 ``/private`` 等别名同样生效
    # （否则 ``/private`` 剥离后为 ``/`` 但根判定已在剥离前放过）。
    # Strip the macOS /private alias *before* both checks so root / system-prefix guards also
    # catch aliases like ``/private`` (which strips to ``/`` but would slip a pre-strip root check).
    canonical = _strip_macos_private(path)
    if canonical.parent == canonical:
        raise SkillHomeIsolationError(f"SKILL Home must not be the filesystem root: {path}")
    for prefix in _FORBIDDEN_SYSTEM_PREFIXES:
        if is_within(canonical, prefix):
            raise SkillHomeIsolationError(
                f"SKILL Home must not live under system / cross-user shared dir {prefix}: {path} "
                f"(skill.md §9.2 — use a per-user, non-system directory)",
            )


def resolve_skill_home(env: Mapping[str, str] | None = None) -> Path:
    """
    解析 SKILL Home 绝对路径并校验隔离 / Resolve the absolute SKILL Home path and validate isolation.

    优先级 ``A2C_SKILL_HOME`` > ``$XDG_DATA_HOME/a2c/skills`` > ``~/.a2c/skills``（见模块 docstring）。
    返回 ``resolve()`` 后的绝对路径；**不**创建目录（落盘由 staging 负责，见 :func:`ensure_skill_home`）。

    :param env: 环境映射，默认 ``os.environ``（便于测试注入）/ env mapping, defaults to ``os.environ``.
    :raises SkillHomeIsolationError: 解析结果违反 skill.md §9.2 隔离铁律 / resolved path violates isolation.
    """
    environ: Mapping[str, str] = os.environ if env is None else env

    override = environ.get(SKILL_HOME_ENV, "").strip()
    if override:
        home = Path(override).expanduser()
        source = SKILL_HOME_ENV
    else:
        xdg = environ.get(XDG_DATA_HOME_ENV, "").strip()
        xdg_path = Path(xdg) if xdg else None
        # XDG 规范：XDG_DATA_HOME 必须为绝对路径，相对值按未设置处理。
        # XDG spec: XDG_DATA_HOME must be absolute; treat relative values as unset.
        if xdg_path is not None and xdg_path.is_absolute():
            home = xdg_path.joinpath(*_A2C_DATA_SUBPATH)
            source = XDG_DATA_HOME_ENV
        else:
            home = Path.home().joinpath(*_DOTDIR_SUBPATH)
            source = "~"

    resolved = home.expanduser().resolve()
    validate_skill_home_isolation(resolved)
    logger.debug("Resolved SKILL Home via %s → %s", source, resolved)
    return resolved


def ensure_skill_home(env: Mapping[str, str] | None = None) -> Path:
    """
    解析并创建 SKILL Home 目录 / Resolve and create the SKILL Home directory（``mkdir -p``）。

    解析 + 隔离校验同 :func:`resolve_skill_home`，校验通过后 ``mkdir(parents=True, exist_ok=True)``。
    供 staging 启动时建根使用 / Used by staging to materialize the root at startup.
    """
    home = resolve_skill_home(env)
    home.mkdir(parents=True, exist_ok=True)
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
