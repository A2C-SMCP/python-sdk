"""测试 console 模块的 set_no_color 行为。"""

from __future__ import annotations

from a2c_smcp.computer.utils import console as console_util


def test_set_no_color_affects_all_references() -> None:
    """set_no_color(True) 后，cli.utils 的 console 引用也应看到 no_color=True。

    #160：修复前 console_util.set_no_color 新建 Console 实例，cli.utils 持有
    导入期旧引用 → no_color 始终为 False（stale）。修复后改为就地 mutate no_color
    属性，所有引用自动跟随。
    """
    from a2c_smcp.computer.cli import utils as cli_utils

    # 保存原始状态
    original = console_util.console.no_color
    original_err = console_util.console_err.no_color

    try:
        # 切换 no_color = True
        console_util.set_no_color(True)
        assert console_util.console.no_color is True, "console_util.console.no_color should be True"
        assert cli_utils.console.no_color is True, (
            "cli.utils.console.no_color should be True — stale reference bug (#160)"
        )
        assert console_util.console_err.no_color is True, "console_util.console_err.no_color should be True"
        assert cli_utils.console_err.no_color is True, (
            "cli.utils.console_err.no_color should be True — stale reference bug (#160)"
        )

        # 切换回 no_color = False
        console_util.set_no_color(False)
        assert console_util.console.no_color is False
        assert cli_utils.console.no_color is False
        assert console_util.console_err.no_color is False
        assert cli_utils.console_err.no_color is False
    finally:
        # 恢复原始状态，避免影响其他测试
        console_util.set_no_color(original)
        # console_err 的原始 no_color 也需要恢复
        console_util.console_err.no_color = original_err


def test_set_no_color_is_idempotent() -> None:
    """多次调用 set_no_color 不会产生副作用或创建多余对象。"""
    original = console_util.console.no_color
    original_err = console_util.console_err.no_color

    try:
        console_util.set_no_color(True)
        c1 = console_util.console
        e1 = console_util.console_err

        console_util.set_no_color(True)  # 重复调用
        c2 = console_util.console
        e2 = console_util.console_err

        # 对象引用应不变（就地 mutate，非重建）
        assert c1 is c2, "console object should not be replaced"
        assert e1 is e2, "console_err object should not be replaced"
        assert c2.no_color is True, "console.no_color should be True"
        assert e2.no_color is True, "console_err.no_color should be True"
    finally:
        console_util.set_no_color(original)
        console_util.console_err.no_color = original_err
