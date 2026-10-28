# -*- coding: utf-8 -*-
# filename: test_read_path_protected.py
# @Author  : JQQ
# @Software: PyCharm
"""
``readRoots`` 授权面 vs 固定拒绝集的**接线级**测试（v0.5.0 #236 / protocol#65）.
Wiring-level tests for the fixed rejection set on top of ``readRoots`` authorization.

协议依据 / Protocol: ``blob-transfer.md`` §固定拒绝集；``security.md`` §读取通道安全（零凭证传播）.

测试意图 / Test intentions:
- 工作目录治理目录 ``.tfrobot`` 内的**具体文件**（``settings.json`` / ``settings.local.json`` /
  ``mcp.json``）必须在拒绝集内；
- **硬链接**到这些文件时路径前缀失效（链接落在 readRoot 内、目标路径不同），只有**文件身份**
  （``dev``/``ino``）能挡 ⇒ 必须 forbidden（隔离审查 🟡5）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from a2c_smcp.computer.blob.path_resolver import PathResolveError
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.scope import ResolvedSettings
from a2c_smcp.utils.path_norm import normalize_path

GOVERNANCE_FILES = ("settings.json", "settings.local.json", "mcp.json")


@pytest.fixture()
def computer(tmp_path: Path) -> Computer:
    """隔离宿主环境：XDG / SKILL Home 全部指向 tmp（不写开发者目录）。"""
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "XDG_DATA_HOME": str(tmp_path / "xdgdata"),
        "XDG_STATE_HOME": str(tmp_path / "xdgstate"),
        "A2C_SKILL_HOME": str(tmp_path / "skills"),
    }
    return Computer(name="comp-test", blob_cache_root=tmp_path / "cache", project_root=tmp_path / "cwd", env=env)


def _grant(computer: Computer, monkeypatch: pytest.MonkeyPatch, *roots: str) -> None:
    """注入 settings 视图（``readRoots`` = ``roots``），走真实 read_roots 编译路径."""
    monkeypatch.setattr(
        computer,
        "_resolve_declared_settings",
        lambda: ResolvedSettings(settings={"readRoots": list(roots)}, errors=[], per_scope_layers={}),
    )


def _make_governance_dir(tmp_path: Path) -> Path:
    gov = tmp_path / "cwd" / ".tfrobot"
    gov.mkdir(parents=True, exist_ok=True)
    return gov


def test_governance_files_are_in_rejection_set(
    tmp_path: Path, computer: Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.tfrobot`` 内的三个治理文件（存在时）都进拒绝集——硬链接防护依赖文件身份."""
    gov = _make_governance_dir(tmp_path)
    for name in GOVERNANCE_FILES:
        (gov / name).write_text("{}", encoding="utf-8")

    _grant(computer, monkeypatch)

    protected = computer.protected_paths().paths
    for name in GOVERNANCE_FILES:
        assert normalize_path(str(gov / name)) in protected, f"{name} 必须在拒绝集内"


@pytest.mark.parametrize("name", GOVERNANCE_FILES)
def test_hardlink_to_governance_file_is_forbidden(
    tmp_path: Path, computer: Computer, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """根内**硬链接**指向 ``.tfrobot/<file>`` → ``forbidden``（路径前缀失效，靠文件身份挡住）."""
    gov = _make_governance_dir(tmp_path)
    target = gov / name
    target.write_text('{"token": "secret"}', encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    link = root / "innocent.json"
    try:
        os.link(target, link)
    except OSError:  # pragma: no cover - 文件系统不支持硬链接（跨设备等）
        pytest.skip("hardlink unsupported on this filesystem")

    _grant(computer, monkeypatch, str(root))

    with pytest.raises(PathResolveError) as exc:
        computer.resolve_read_path(str(link))

    assert exc.value.reason == "forbidden"


def test_hardlink_to_state_value_store_is_forbidden(
    tmp_path: Path, computer: Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同款：硬链接到 state 目录的 ``input-values.json``（可含 secret 值）→ ``forbidden``."""
    state_dir = tmp_path / "xdgstate" / "a2c"
    state_dir.mkdir(parents=True)
    store = state_dir / "input-values.json"
    store.write_text('{"secret_input": "s3cr3t"}', encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    link = root / "innocent.json"
    try:
        os.link(store, link)
    except OSError:  # pragma: no cover
        pytest.skip("hardlink unsupported on this filesystem")

    _grant(computer, monkeypatch, str(root))

    with pytest.raises(PathResolveError) as exc:
        computer.resolve_read_path(str(link))

    assert exc.value.reason == "forbidden"


def _home_only_computer(tmp_path: Path, home: Path) -> Computer:
    """仅注入 ``HOME``（XDG 全未设置）的 Computer —— protocol#64 身份路径跟随注入 HOME。"""
    return Computer(
        name="home-only",
        blob_cache_root=tmp_path / "cache",
        project_root=tmp_path / "cwd",
        skill_home=tmp_path / "skills",
        env={"HOME": str(home)},
    )


def test_injected_home_identity_is_in_rejection_set_and_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 fix-review：注入 HOME（XDG 未设）时，``computer-id`` 的**实际使用位置**必须在拒绝集。

    ``readRoots`` 覆盖该 home 也不得读出稳定身份（目录前缀 + 文件身份双保险）。
    """
    from a2c_smcp.computer.identity import identity_store_path

    home = tmp_path / "srv-home"
    computer = _home_only_computer(tmp_path, home)
    env = {"HOME": str(home)}
    identity = identity_store_path(env)
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text("abcdef01-2345-4678-8abc-def012345678\n", encoding="utf-8")

    _grant(computer, monkeypatch, str(home))

    protected = computer.protected_paths().paths
    assert normalize_path(str(identity)) in protected, "身份文件必须在拒绝集内"
    assert normalize_path(str(identity.parent)) in protected, "身份目录必须在拒绝集内（前缀保护）"

    with pytest.raises(PathResolveError) as exc:
        computer.resolve_read_path(str(identity))
    assert exc.value.reason == "forbidden"


def test_hardlink_to_injected_home_identity_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同款硬链接变体：路径前缀失效 ⇒ 靠身份文件在拒绝集里的 file_id 兜底。"""
    from a2c_smcp.computer.identity import identity_store_path

    home = tmp_path / "srv-home"
    computer = _home_only_computer(tmp_path, home)
    identity = identity_store_path({"HOME": str(home)})
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text("abcdef01-2345-4678-8abc-def012345678\n", encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    link = root / "innocent.txt"
    try:
        os.link(identity, link)
    except OSError:  # pragma: no cover - 文件系统不支持硬链接（跨设备等）
        pytest.skip("hardlink unsupported on this filesystem")

    _grant(computer, monkeypatch, str(root))

    with pytest.raises(PathResolveError) as exc:
        computer.resolve_read_path(str(link))
    assert exc.value.reason == "forbidden"
