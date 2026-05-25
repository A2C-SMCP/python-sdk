# -*- coding: utf-8 -*-
# filename: policy.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
policy scope 四子源 first-source-wins（v0.2.1）
Policy-scope four sub-sources, first-source-wins (v0.2.1).

SDK 设计 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §5.2 / §5.4。

policy 是 settings 链**最高优先级**层（企业 / 运维强制）。其内部由四个子源构成，**不合并**——
按优先级取**第一个有内容**者整体叠加进主链（first-source-wins，照 Claude Code：
remote > OS-MDM > managed-settings.json[+.d] > HKCU）。
Policy is the highest-priority settings layer. Its four sub-sources are NOT merged — the first
(highest-priority) one with content wins wholesale.

============== ========= ====================================================== ===========
优先级           平台       位置                                                    v0.2.1
============== ========= ====================================================== ===========
1（最高）         全平台      Remote managed endpoint                                **stub**（不联网）
2               macOS     /Library/Managed Preferences/com.a2c.computer.plist    ✓
2               Windows   HKLM\\SOFTWARE\\Policies\\A2CComputer\\Settings          ✓（best-effort）
3               全平台      managed-settings.json (+ managed-settings.d/*.json)    ✓
4（最低）         Windows   HKCU\\SOFTWARE\\Policies\\A2CComputer\\Settings          ✓（best-effort）
============== ========= ====================================================== ===========

**v0.2.1 范围**：实现层级 2/3/4 的本地文件 / 注册表读取；Remote 留 stub、**不发起网络请求**
（30 min poll + OAuth 留 v0.2.2+）。Linux/macOS/Windows 三平台都至少能读 layer 3。
**v0.2.1 scope**: layers 2/3/4 local readers; Remote is a stub (no network). All three platforms
read at least layer 3.

本模块只做 **first-source-wins 选取**，返回原始 dict；类型 / scope 校验由
:func:`a2c_smcp.computer.settings.scope.resolve_settings` 按 POLICY scope 统一执行。
This module only performs the first-source-wins selection; type/scope validation is centralized
in ``scope.resolve_settings`` under the POLICY scope.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from a2c_smcp.computer.settings.scope import merge_read
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# Remote managed endpoint（v0.2.2+ 实现）/ Remote managed endpoint base env (v0.2.2+).
REMOTE_POLICY_BASE_ENV = "A2C_BASE_API_URL"
REMOTE_POLICY_PATH = "/api/computer/settings"

# Windows 注册表子键（HKLM / HKCU 共用）/ Windows registry subkey (shared by HKLM / HKCU).
WINDOWS_POLICY_SUBKEY = r"SOFTWARE\Policies\A2CComputer\Settings"
# 注册表内承载 settings JSON 的值名（best-effort）/ Registry value name carrying the settings JSON.
WINDOWS_POLICY_VALUE_NAME = "Settings"

# 各平台 managed-settings.json 位置（layer 3）/ Per-platform managed-settings.json locations (layer 3).
MACOS_PLIST_PATH = Path("/Library/Managed Preferences/com.a2c.computer.plist")
MACOS_MANAGED_DIR = Path("/Library/Application Support/A2CComputer")
WINDOWS_MANAGED_DIR = Path(r"C:\Program Files\A2CComputer")
LINUX_MANAGED_DIR = Path("/etc/a2c-computer")

MANAGED_SETTINGS_FILENAME = "managed-settings.json"
MANAGED_SETTINGS_DROPIN_DIRNAME = "managed-settings.d"


@dataclass(frozen=True)
class PolicySource:
    """
    一个 policy 子源 / A single policy sub-source。

    ``priority`` 越小越高（1 = 最高）；``loader`` 返回该子源内容（``dict``）或 ``None``（缺席 / 不可读）。
    Lower ``priority`` ranks higher (1 = highest); ``loader`` returns the content dict or ``None``.
    """

    name: str
    priority: int
    loader: Callable[[], dict[str, Any] | None]


# ---------------------------------------------------------------------------
# 子源读取器 / Sub-source readers
# ---------------------------------------------------------------------------
def _read_json_file(path: Path) -> dict[str, Any] | None:
    """读取单个 JSON 文件 → dict（缺失 / 损坏 / 非对象 → None）/ Read one JSON file (missing/corrupt/non-object → None)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Policy JSON %s unreadable/corrupt, skipped: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Policy JSON %s root is not an object, skipped", path)
        return None
    return data


def load_managed_settings(managed_dir: Path) -> dict[str, Any] | None:
    """
    layer 3 读取器：``managed-settings.json`` + ``managed-settings.d/*.json`` 深合并 / layer-3 reader。

    base 文件先读，随后按文件名字典序合并 ``managed-settings.d/*.json``（drop-in 覆盖 base，沿用
    :func:`~a2c_smcp.computer.settings.scope.merge_read` 读语义）。皆缺 → ``None``。
    Reads the base file then merges ``managed-settings.d/*.json`` in lexical order (drop-ins
    override base, read-merge semantics). All absent → ``None``.
    """
    base = _read_json_file(managed_dir / MANAGED_SETTINGS_FILENAME)
    merged: dict[str, Any] | None = base

    dropin_dir = managed_dir / MANAGED_SETTINGS_DROPIN_DIRNAME
    if dropin_dir.is_dir():
        for entry in sorted(dropin_dir.glob("*.json")):
            piece = _read_json_file(entry)
            if piece is None:
                continue
            merged = piece if merged is None else merge_read(merged, piece)

    return merged


def load_macos_plist(path: Path = MACOS_PLIST_PATH) -> dict[str, Any] | None:
    """layer 2（macOS）读取器：解析 Managed Preferences plist / layer-2 macOS plist reader。"""
    if not path.exists():
        return None
    try:
        import plistlib

        data = plistlib.loads(path.read_bytes())
    except Exception as exc:  # plistlib.InvalidFileException / OSError 等 / parse or IO error
        logger.warning("Policy plist %s unreadable, skipped: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def load_windows_registry(hive_name: str, subkey: str = WINDOWS_POLICY_SUBKEY) -> dict[str, Any] | None:
    """
    layer 2/4（Windows）读取器：从注册表值读取承载的 settings JSON / layer-2/4 Windows registry reader。

    best-effort：读取 ``<hive>\\<subkey>`` 下名为 :data:`WINDOWS_POLICY_VALUE_NAME` 的字符串值并按
    JSON 解析（registry 扁平、无法原生承载嵌套数组 / 对象，故约定承载 JSON 串）。非 Windows /
    ``winreg`` 不可用 / 键缺失 → ``None``。
    Best-effort: reads a JSON-string value named :data:`WINDOWS_POLICY_VALUE_NAME` under
    ``<hive>\\<subkey>`` and parses it. Non-Windows / missing key → ``None``.
    """
    if sys.platform != "win32":
        # 非 Windows：mypy 据 sys.platform 收窄判定以下不可达，故 winreg import 不被检查（无需 type:ignore）。
        # Non-Windows: mypy narrows on sys.platform; the rest is unreachable, so the winreg import is not checked.
        return None
    import winreg

    hive = getattr(winreg, hive_name, None)
    if hive is None:
        return None
    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _ = winreg.QueryValueEx(key, WINDOWS_POLICY_VALUE_NAME)
    except OSError:  # 键 / 值不存在 / key or value missing
        return None
    if not isinstance(value, str):
        return None
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:  # pragma: no cover - 取决于运行机注册表内容
        logger.warning("Windows policy registry value is not valid JSON, skipped: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def load_remote_policy(env: Mapping[str, str]) -> dict[str, Any] | None:
    """
    layer 1（remote）读取器 —— **v0.2.1 stub，恒返回 None、不联网** / layer-1 remote reader (v0.2.1 stub).

    v0.2.2+ 将实现 ``${A2C_BASE_API_URL}/api/computer/settings`` 的 30 min poll + OAuth/team
    关联（见 §5.2）/ v0.2.2+ will add a 30-min poll + OAuth against that endpoint.
    """
    base = env.get(REMOTE_POLICY_BASE_ENV, "").strip()
    if base:
        logger.debug("Remote managed-policy endpoint configured (%s) but disabled in v0.2.1 (stub)", base)
    return None


# ---------------------------------------------------------------------------
# 默认子源装配 + first-source-wins / Default sources + first-source-wins
# ---------------------------------------------------------------------------
def default_policy_sources(platform: str, env: Mapping[str, str]) -> list[PolicySource]:
    """
    按平台装配默认 policy 子源 / Assemble default policy sub-sources per platform（§5.2）。

    :param platform: ``sys.platform`` 风格字符串（``"darwin"`` / ``"win32"`` / 其余按 Linux）。
    """
    sources: list[PolicySource] = [
        PolicySource(name="remote", priority=1, loader=lambda: load_remote_policy(env)),
    ]

    if platform == "darwin":
        sources.append(PolicySource(name="macos-plist", priority=2, loader=lambda: load_macos_plist()))
        sources.append(PolicySource(name="managed-settings", priority=3, loader=lambda: load_managed_settings(MACOS_MANAGED_DIR)))
    elif platform == "win32":
        sources.append(PolicySource(name="hklm-registry", priority=2, loader=lambda: load_windows_registry("HKEY_LOCAL_MACHINE")))
        sources.append(PolicySource(name="managed-settings", priority=3, loader=lambda: load_managed_settings(WINDOWS_MANAGED_DIR)))
        sources.append(PolicySource(name="hkcu-registry", priority=4, loader=lambda: load_windows_registry("HKEY_CURRENT_USER")))
    else:  # Linux / 其余 POSIX：无 OS 原生 MDM，仅 layer 3。
        sources.append(PolicySource(name="managed-settings", priority=3, loader=lambda: load_managed_settings(LINUX_MANAGED_DIR)))

    return sources


def resolve_policy_settings(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    sources: Sequence[PolicySource] | None = None,
) -> dict[str, Any]:
    """
    first-source-wins 选取 policy 内容 / Select policy content by first-source-wins（§5.2 / §5.4）。

    按 ``priority`` 升序（1 最高）逐源尝试，返回**第一个非空 dict**整体；皆空 → ``{}``。子源**不合并**
    （照 CC：只取最高且有内容者）。单源 loader 抛错 → 记 WARN 跳过、继续下一源（不阻断启动）。
    Tries sources by ascending priority, returns the first non-empty dict wholesale; all empty →
    ``{}``. Sub-sources are not merged. A loader raising is logged and skipped (never blocks startup).

    :param env: 环境映射，默认 ``os.environ`` / env mapping, defaults to ``os.environ``.
    :param platform: 平台串，默认 ``sys.platform`` / platform string, defaults to ``sys.platform``.
    :param sources: 自定义子源（便于测试注入）；缺省按 :func:`default_policy_sources` 装配 / injectable for tests.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    plat = sys.platform if platform is None else platform
    srcs = list(sources) if sources is not None else default_policy_sources(plat, environ)

    for source in sorted(srcs, key=lambda s: s.priority):
        try:
            loaded = source.loader()
        except Exception as exc:  # 单源读取异常不阻断 / a single source error must not block
            logger.warning("Policy source %r failed to load, skipped: %s", source.name, exc)
            continue
        if loaded:  # 非 None 且非空 / non-None and non-empty
            logger.debug("Policy resolved from sub-source %r (priority %d)", source.name, source.priority)
            return loaded

    return {}
