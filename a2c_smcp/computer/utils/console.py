"""
全局 Console 工具（#226 惰性导入 / lazy import）。
- 统一提供一个可被全局引用的 rich.Console 实例。
- 支持在运行时切换 no_color 以适配非 TTY/IDE 控制台。
- rich 仅在本模块首次被访问 `console` / `console_err` 时导入：未安装 ``a2c-smcp[cli]`` 时
  import 本模块不报错，访问时抛携带安装提示的 ImportError（#187 惰性契约）。
- 注意：缺 extra 时 `hasattr(module, "console")` / `getattr(module, "console", None)` 会**抛**
  ImportError（PEP 562 的 `__getattr__` 异常不会被这两个内建吞掉）——需要「可选 UI」降级时
  请先调用 :func:`console_available`。

Global Console helpers (lazy import, #226). ``rich`` is imported only when ``console`` /
``console_err`` is first accessed; importing this module never requires the ``[cli]`` extra,
and a missing extra raises an actionable ImportError at access time (#187 contract).
"""

from __future__ import annotations

import importlib.util
import threading
from typing import TYPE_CHECKING, Any

from a2c_smcp.computer.utils.optional_deps import require_cli_dependency

if TYPE_CHECKING:
    from rich.console import Console

    # 仅供类型检查：运行时由模块级 __getattr__（PEP 562）按需提供同名属性。
    # Type-checking only: at runtime the same names are provided lazily by module __getattr__ (PEP 562).
    console: Console
    console_err: Console

# 注意：不要在其他模块中使用 "from ... import console" 然后绑定到局部变量，
# 应使用 "from a2c_smcp.computer.utils import console as console_util" 然后访问 console_util.console。
# set_no_color 就地 mutate 实例（不重建），通过模块属性访问可确保始终拿到正确实例。
# 惰性化后该契约不变：__getattr__ 缓存同一实例，身份稳定。
# Keep the #160 contract: callers access ``console_util.console`` as a module attribute, and
# ``set_no_color`` mutates the cached instance in place (identity is stable across accesses).
_console: Console | None = None

# stderr 侧 Console（#157）：供「stdout 是机读 JSON」的命令打诊断/警示，避免污染 stdout。
# 典型：``settings show`` 的 stdout 恒为 JSON（供 ``| jq`` 消费），scope 越权被过滤的解释须走 stderr。
# stderr Console: for diagnostics emitted by commands whose stdout is machine-readable JSON.
_console_err: Console | None = None

# 三态：None = 调用方尚未显式设置，让 rich 自行按 NO_COLOR 环境变量推导初始值；
# 显式设置后，后创建的实例也沿用该值（保持与 eager 实现相同的可观察行为）。
# Tri-state: None = not explicitly set, so rich derives the initial value from NO_COLOR.
_no_color: bool | None = None
_lock = threading.Lock()


def _create_console(*, stderr: bool) -> Console:
    """中文: 创建并配置一个 Console 实例（调用方须持有 ``_lock``）。"""
    console_module = require_cli_dependency("rich.console")
    instance: Console = console_module.Console(stderr=stderr)
    if _no_color is not None:
        instance.no_color = _no_color
    return instance


def __getattr__(name: str) -> Any:
    """PEP 562 惰性属性：``console`` / ``console_err`` 首次访问时才导入 rich 并缓存实例。"""
    if name == "console":
        global _console
        with _lock:
            if _console is None:
                _console = _create_console(stderr=False)
            return _console
    if name == "console_err":
        global _console_err
        with _lock:
            if _console_err is None:
                _console_err = _create_console(stderr=True)
            return _console_err
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def console_available() -> bool:
    """中文: 判断 rich 是否可导入（**不执行导入**）。

    纯 core 安装下 ``hasattr(console_util, "console")`` / ``getattr(console_util, "console", None)``
    会抛 ImportError（PEP 562 语义：``hasattr`` 只吞 AttributeError）。需要「可选 UI」降级时，
    请先调用本谓词，不要用 ``getattr(..., default)`` 探测。

    English: Whether ``rich`` is importable, without importing it. Under a bare-core install,
    ``hasattr`` / ``getattr(module, "console", default)`` raise ImportError instead of returning a
    default (PEP 562); use this predicate for optional-UI fallbacks.

    Returns:
        bool: rich 是否可导入 / whether rich is importable.
    """
    try:
        return importlib.util.find_spec("rich") is not None
    except (ImportError, ValueError):
        return False


def __dir__() -> list[str]:
    """中文: 暴露惰性属性名（``console`` / ``console_err``），供 ``dir()`` 与 IDE 补全。"""
    return sorted({*globals(), "console", "console_err"})


def set_no_color(flag: bool) -> None:
    """在运行时切换全局 Console 的 no_color 配置。

    就地 mutate 现有 Console 实例的 no_color 属性（而非重建对象），
    确保所有持有该实例引用的模块自动跟随——无需调用方遵守属性访问契约。
    Rich Console 在 render 时动态检查 self.no_color，就地修改语义等价。

    惰性化（#226）后新增：实例尚未创建时只记录标志，**不触发 rich 导入**；
    后续首次创建实例时自动套用该值。

    Args:
        flag: 是否禁用颜色 / whether to disable color.
    """
    global _no_color
    with _lock:
        _no_color = flag
        if _console is not None:
            _console.no_color = flag
        if _console_err is not None:
            _console_err.no_color = flag
