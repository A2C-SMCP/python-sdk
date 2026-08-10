"""
全局 Console 工具。
- 统一提供一个可被全局引用的 rich.Console 实例。
- 支持在运行时切换 no_color 以适配非 TTY/IDE 控制台。
"""

from __future__ import annotations

from rich.console import Console

# 注意：不要在其他模块中使用 "from ... import console" 然后绑定到局部变量，
# 应使用 "from a2c_smcp.computer.utils import console as console_util" 然后访问 console_util.console。
# set_no_color 就地 mutate 实例（不重建），通过模块属性访问可确保始终拿到正确实例。
console: Console = Console()

# stderr 侧 Console（#157）：供「stdout 是机读 JSON」的命令打诊断/警示，避免污染 stdout。
# 典型：``settings show`` 的 stdout 恒为 JSON（供 ``| jq`` 消费），scope 越权被过滤的解释须走 stderr。
# stderr Console: for diagnostics emitted by commands whose stdout is machine-readable JSON.
console_err: Console = Console(stderr=True)


def set_no_color(flag: bool) -> None:
    """在运行时切换全局 Console 的 no_color 配置。

    就地 mutate 现有 Console 实例的 no_color 属性（而非重建对象），
    确保所有持有该实例引用的模块自动跟随——无需调用方遵守属性访问契约。
    Rich Console 在 render 时动态检查 self.no_color，就地修改语义等价。
    """
    console.no_color = flag
    console_err.no_color = flag
