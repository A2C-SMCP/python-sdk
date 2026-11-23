# -*- coding: utf-8 -*-
# filename: test_read_roots.py
# @Author  : JQQ
# @Software: PyCharm
"""
``readRoots`` settings 键的字段级校验与跨 scope 合并单测（v0.5.0 #236 / protocol#65）.
Unit tests for the ``readRoots`` settings key: field validation + cross-scope merge.

协议依据 / Protocol: ``security.md`` §读取通道安全（readRoots 授权）/ ``blob-transfer.md``
§生产者三：按路径读取；``data-structures.md`` §ResolvePathReq note（受信 scope 集合）。

测试意图 / Test intentions:
- 数组形态 + **逐项绝对路径**；非法项（相对路径 / 非字符串）**逐条过滤并记诊断**，其余根仍生效
- **#304 item 3**：判据只有 ``normalize_path``（**单一判据**，与运行期 ``build_read_roots`` 同源）——
  Windows verbatim ``\\\\?\\C:\\...`` 配置期即可用、drive-relative ``C:repos\\`` 双端一致拒绝；
  平台差异经 ``_validate_read_roots_for`` 显式接缝在任意 OS 上对拍（CI 只有 Linux）
- ``project`` scope 供给 → **整字段拒绝**（读授权不得由入 git 的仓库扩张）；受信 scope 保留
- 跨 scope 读合并 = **拼接去重**（低 scope 在前；授权只增不减，高 scope 不遮蔽低 scope）
- 缺省缺席 = 不可读（fail-closed 的配置侧half；运行期由 resolve 管线保证）
- ``~`` 不展开（协议 ``normalize_path`` 同样不展开），故不算绝对路径
"""

import pytest

from a2c_smcp.computer.blob.path_resolver import build_read_roots
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.settings.schema import (
    FIELD_READ_ROOTS,
    SettingsScope,
    _validate_read_roots_for,
    validate_settings,
)
from a2c_smcp.computer.settings.scope import ResolvedSettings, merge_layers
from a2c_smcp.utils.path_norm import PathPlatform, native_platform, normalize_path


# ---------------------------------------------------------------------------
# 字段级校验 / field-level validation
# ---------------------------------------------------------------------------
def test_read_roots_keeps_absolute_items() -> None:
    """合法绝对路径（**宿主平台**形态 + canonical ``/C:/...``）全部保留且不记错.

    形态预判与运行期同源：用 ``normalize_path(宿主平台)``，故 Windows 原生路径在 POSIX 宿主上
    属 ``invalid_path``（运行期同样不可用），不应保留。
    """
    if native_platform() == "posix":
        items = ["/home/u/docs", "/var/lib/data/", "/C:/Users/u/canonical"]
    else:
        items = ["C:\\Users\\u\\docs", "C:/Users/u/other", "/C:/Users/u/canonical"]
    cleaned, errors = validate_settings({FIELD_READ_ROOTS: items}, SettingsScope.USER)
    assert errors == []
    assert cleaned[FIELD_READ_ROOTS] == items


def test_read_roots_rejects_absolute_but_invalid_shape() -> None:
    """绝对路径但**形态非法**（含 U+0000）→ 配置期即丢弃并记 ``invalid path`` 诊断（不留给运行期）."""
    cleaned, errors = validate_settings({FIELD_READ_ROOTS: ["/srv/ok", "/srv/\x00bad"]}, SettingsScope.USER)

    assert cleaned[FIELD_READ_ROOTS] == ["/srv/ok"]
    assert len(errors) == 1
    assert errors[0].field == f"{FIELD_READ_ROOTS}[1]"
    assert "invalid path" in errors[0].reason


def test_read_roots_rejects_foreign_platform_form() -> None:
    """非宿主平台形态（POSIX 上的 ``C:\\...`` / Windows 上的 ``/home/...``）→ 过滤 + 诊断."""
    foreign = "C:\\Users\\u\\docs" if native_platform() == "posix" else "/home/u/docs"
    cleaned, errors = validate_settings({FIELD_READ_ROOTS: [foreign]}, SettingsScope.USER)

    assert cleaned[FIELD_READ_ROOTS] == []
    assert len(errors) == 1


def test_read_roots_filters_invalid_items_individually() -> None:
    """非法项逐条过滤并记诊断，**不**整字段判废——其余授权根仍然生效."""
    raw = {FIELD_READ_ROOTS: ["/ok/one", "relative/path", 42, "~/docs", "/ok/two"]}
    cleaned, errors = validate_settings(raw, SettingsScope.USER)

    assert cleaned[FIELD_READ_ROOTS] == ["/ok/one", "/ok/two"]
    # 3 条诊断，且带逐项点路径（可定位到具体元素）
    assert len(errors) == 3
    assert {e.field for e in errors} == {f"{FIELD_READ_ROOTS}[1]", f"{FIELD_READ_ROOTS}[2]", f"{FIELD_READ_ROOTS}[3]"}
    assert all(e.scope is SettingsScope.USER for e in errors)


def test_read_roots_all_items_invalid_yields_empty_list() -> None:
    """全部非法 → 空数组（而非字段消失）；空 = 不可读，与缺省同义（fail-closed）."""
    cleaned, errors = validate_settings({FIELD_READ_ROOTS: ["rel", "~/x"]}, SettingsScope.USER)
    assert cleaned[FIELD_READ_ROOTS] == []
    assert len(errors) == 2


def test_read_roots_non_list_drops_whole_field() -> None:
    """非数组形态 → 整字段判废（回退默认 = 不可读）+ 一条诊断."""
    for bad in ("/single/path", {"a": "/x"}, 7, None):
        cleaned, errors = validate_settings({FIELD_READ_ROOTS: bad}, SettingsScope.USER)
        assert FIELD_READ_ROOTS not in cleaned
        assert len(errors) == 1


def test_read_roots_absent_means_unreadable() -> None:
    """缺省缺席（不写该键）→ cleaned 中不存在该键；Computer 侧按空 = 不可读处理."""
    cleaned, errors = validate_settings({"landingRoot": "/tmp/landing"}, SettingsScope.USER)
    assert FIELD_READ_ROOTS not in cleaned
    assert errors == []


# ---------------------------------------------------------------------------
# scope 治理 / scope governance
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scope", [SettingsScope.USER, SettingsScope.LOCAL, SettingsScope.FLAG, SettingsScope.POLICY])
def test_read_roots_allowed_from_trusted_scopes(scope: SettingsScope) -> None:
    """受信 scope（user/local/flag/policy）供给 → 保留（``embed`` 只存在于 mcp.json 轴）."""
    cleaned, errors = validate_settings({FIELD_READ_ROOTS: ["/trusted/root"]}, scope)
    assert cleaned[FIELD_READ_ROOTS] == ["/trusted/root"]
    assert errors == []


def test_read_roots_rejected_from_project_scope() -> None:
    """project scope（入 git、随仓库分发）供给 → **整字段拒绝** + 记错（读授权扩张面）."""
    cleaned, errors = validate_settings({FIELD_READ_ROOTS: ["/", "/etc"]}, SettingsScope.PROJECT)

    assert FIELD_READ_ROOTS not in cleaned
    assert len(errors) == 1
    assert errors[0].field == FIELD_READ_ROOTS
    assert errors[0].scope is SettingsScope.PROJECT
    assert "project scope" in errors[0].reason


# ---------------------------------------------------------------------------
# 跨 scope 合并 / cross-scope merge
# ---------------------------------------------------------------------------
def test_read_roots_merge_concatenates_and_dedups() -> None:
    """跨 scope 读合并 = 低 scope 在前拼接去重；高 scope 不遮蔽低 scope 已授的根."""
    layers = [
        {FIELD_READ_ROOTS: ["/from-user", "/shared"]},
        {FIELD_READ_ROOTS: ["/shared", "/from-local"]},
        {FIELD_READ_ROOTS: ["/from-policy"]},
    ]
    merged = merge_layers(layers)
    assert merged[FIELD_READ_ROOTS] == ["/from-user", "/shared", "/from-local", "/from-policy"]


def test_read_roots_merge_is_union_not_override() -> None:
    """单层缺省不抹掉其它层（数组语义），与 landingRoot 的标量「高覆盖」形成对照."""
    merged = merge_layers([{}, {FIELD_READ_ROOTS: ["/only"]}, {}])
    assert merged[FIELD_READ_ROOTS] == ["/only"]


# ---------------------------------------------------------------------------
# #304 item 3：单一判据（配置期 == 运行期）/ single predicate shared with runtime
# ---------------------------------------------------------------------------
# 覆盖三族形态：POSIX 绝对、Windows 盘符绝对、Windows verbatim 前缀、drive-relative、
# verbatim-UNC、设备前缀、以及「带首尾空白」（readRoots **不** trim，与 landingRoot 相反）。
_READ_ROOTS_PREDICATE_VECTORS = [
    "/x",
    "C:\\ok",
    "C:repos\\",
    "\\\\?\\C:\\data\\share",
    "\\\\?\\UNC\\server\\share\\a.txt",
    "\\\\server\\share",
    "\\\\.\\C:\\x",
    "",
    "/x ",  # POSIX 合法（尾随空格是普通字符）、Windows 非法——两侧各自一致即可
    " /x ",  # 前导空白：两侧都非法（readRoots **不** trim）
]


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_read_roots_config_time_and_runtime_share_one_predicate(platform: PathPlatform) -> None:
    """配置期保留集合与运行期 ``build_read_roots`` 编译集合**逐项一致**（单一判据）。

    只比较 ``configured``（运行期归一形态）——``real`` 依赖宿主 cwd，跨平台模拟下无意义。
    """
    cleaned, errors = _validate_read_roots_for(FIELD_READ_ROOTS, _READ_ROOTS_PREDICATE_VECTORS, SettingsScope.USER, platform)
    compiled = build_read_roots(_READ_ROOTS_PREDICATE_VECTORS, platform=platform)

    assert [normalize_path(item, platform) for item in cleaned] == [r.configured for r in compiled]
    # 非法项**逐条**记诊断（不整字段判废）
    assert len(errors) == len(_READ_ROOTS_PREDICATE_VECTORS) - len(cleaned)


def test_read_roots_accepts_windows_verbatim_prefix() -> None:
    """Windows verbatim ``\\\\?\\C:\\...``：``normalize_path`` 接受并剥前缀，配置期须放行（#304）。

    对照：同一输入按 POSIX 平台非法（非 ``/`` 开头）→ 丢弃 + 诊断；盘符绝对形态 ``C:\\ok``
    在 Windows 下同样必须放行（正向锚——防止 ``normalize_path`` 回归时两侧同向丢弃而「同源」仍绿）。
    """
    raw = ["\\\\?\\C:\\data\\share", "C:\\ok"]

    cleaned, errors = _validate_read_roots_for(FIELD_READ_ROOTS, raw, SettingsScope.USER, "windows")
    assert cleaned == raw, "verbatim / 盘符形态应保留原值（不在配置期改写）"
    assert errors == []

    cleaned, errors = _validate_read_roots_for(FIELD_READ_ROOTS, raw, SettingsScope.USER, "posix")
    assert cleaned == []
    assert len(errors) == len(raw)


def test_read_roots_rejects_drive_relative_on_windows() -> None:
    """drive-relative ``C:repos\\`` 双端一致拒绝（协议 ``normalize_path`` 已定义 invalid_path）。"""
    cleaned, errors = _validate_read_roots_for(FIELD_READ_ROOTS, ["C:repos\\"], SettingsScope.USER, "windows")

    assert cleaned == []
    assert len(errors) == 1
    assert "invalid path" in errors[0].reason


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_read_roots_production_entry_uses_host_platform(monkeypatch: pytest.MonkeyPatch, platform: PathPlatform) -> None:
    """生产入口取 ``native_platform()``，不得写死平台.

    逐个把 ``schema.native_platform`` 换成 ``posix`` / ``windows``，判定必须随翻——
    **任一宿主**上写死任一平台的变异都会在另一参数上红（只测「相反平台」在 Windows 宿主上
    会与「写死 posix」的变异重合而恒真）。
    """
    monkeypatch.setattr("a2c_smcp.computer.settings.schema.native_platform", lambda: platform)

    raw = ["\\\\?\\C:\\data"]
    cleaned, errors = validate_settings({FIELD_READ_ROOTS: raw}, SettingsScope.USER)
    seam_cleaned, seam_errors = _validate_read_roots_for(FIELD_READ_ROOTS, raw, SettingsScope.USER, platform)

    assert cleaned[FIELD_READ_ROOTS] == seam_cleaned
    assert [(e.field, e.reason) for e in errors] == [(e.field, e.reason) for e in seam_errors]
    # 正对照：两个平台的判定必须真的不同（否则「写死任一平台」都能蒙混）
    assert cleaned[FIELD_READ_ROOTS] == (raw if platform == "windows" else [])


def test_read_roots_production_entry_delegates_to_seam() -> None:
    """生产入口（宿主平台）与显式接缝结果一致——钉住 ``_validate_read_roots`` 的委托关系."""
    items = ["/x", "relative/path", "C:\\ok"]

    cleaned, errors = validate_settings({FIELD_READ_ROOTS: items}, SettingsScope.USER)
    seam_cleaned, seam_errors = _validate_read_roots_for(FIELD_READ_ROOTS, items, SettingsScope.USER, native_platform())

    assert cleaned[FIELD_READ_ROOTS] == seam_cleaned
    assert [(e.field, e.reason) for e in errors] == [(e.field, e.reason) for e in seam_errors]


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_read_roots_runtime_wiring_uses_host_platform(monkeypatch: pytest.MonkeyPatch, platform: PathPlatform) -> None:
    """运行期接线 ``Computer._compile_read_roots`` → ``build_read_roots(items)`` 取宿主平台.

    双参数：钉住运行期默认平台来源——写死**任一**平台（``posix`` 或 ``windows``）都会在另一个
    参数上红。若在此写死 ``platform="posix"``，Linux CI 会全绿，而 Windows 宿主会出现
    「配置期接受 ``C:\\...``、运行期丢弃」——正是本单要消灭的配置期/运行期分叉。
    """
    monkeypatch.setattr("a2c_smcp.computer.blob.path_resolver.native_platform", lambda: platform)
    computer = object.__new__(Computer)  # 只测编译逻辑，跳过 __init__ 的重资源构造
    resolved = ResolvedSettings(settings={FIELD_READ_ROOTS: ["\\\\?\\C:\\data"]})

    roots = computer._compile_read_roots(resolved)

    assert [root.configured for root in roots] == (["/C:/data"] if platform == "windows" else [])
