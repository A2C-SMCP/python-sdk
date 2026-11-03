# -*- coding: utf-8 -*-
# filename: test_packaging_dependencies.py
# @Time    : 2026/09/29
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
发布包依赖覆盖守卫（#224）/ Declared-dependency coverage guard.

事故背景 / Incident
-------------------
``skills/staging.py`` 的模块级 ``import yaml`` 早在 v0.2.1（commit ``21e6016``）随 SKILL 子系统引入，
但 ``pyyaml`` 从未写进 ``[project] dependencies``。之所以一路逃逸到 v0.4.0：

- dev / CI 环境**恒有** yaml —— ``poethepoet``（dev 组）传递依赖 pyyaml；
- mypy 被 ``ignore_missing_imports`` 显式豁免 ``yaml``（该 override 正是引入提交加的那两行）；
- ruff ``select`` 全为单文件规则，无跨文件解析能力；
- 无任何「干净安装」冒烟 —— ``publish.yml`` 只 ``uv build``，从不安装、从不 import。

直到使用者在干净 pipx 环境跑 ``a2c-computer --help``，才崩在导入阶段。

守卫意图 / Guard intent
-----------------------
断言 ``a2c_smcp/`` 源码里**每个第三方顶层 import 都有一条声明路径**。判定顺序：

1. **声明面直配**：import 名（PEP 503 归一化后）本身就是某个已声明发行包（core 或任一 extra）——
   覆盖 ``keyring`` / ``mcp`` / ``vrl_python`` → ``vrl-python`` 这类同名或仅分隔符不同的情形。
2. **安装映射回落**：该 import 的**提供者**（``packages_distributions()``）落在声明闭包内 ——
   覆盖 ``yaml`` → ``pyyaml``、``socketio`` → ``python-socketio`` 这类改名情形。

两条都不满足才算违规。

⚠️ 为什么不能只用第 2 条 / Why rule 2 alone is wrong
---------------------------------------------------
``packages_distributions()`` 只认识**已安装**的发行包。若只用第 2 条，则「已声明但当前环境未装」的包
（如只在 ``keyring`` extra 里的 ``keyring`` —— CI 与本地 ``uv sync --all-groups`` 都**不装 extra**）
会因候选集为空被判违规 ⇒ **每个 PR 恒红**，且报错文案误导为「加进 core 依赖」。
故第 1 条作为主判据，第 2 条只做改名回落。

⚠️ marker 必须求值 / Markers must be evaluated
---------------------------------------------
可达闭包展开依赖时，PEP 508 marker 必须以 ``extra=""`` 求值。忽略 marker 的天真实现会顺着
``mcp → starlette → "pyyaml ; extra == 'full'"`` 与 ``watchdog → "PyYAML ; extra == 'watchmedo'"``
认为 pyyaml 可达 —— 于是**守卫会对它要防的那个 bug 假绿**（实测：无过滤版违规数 0，marker-aware 版 1）。
此不变量由 :func:`test_extra_gated_requirements_are_not_treated_as_installed` 钉住。

⚠️ 刻意排除 dev / test group / Dev groups are excluded on purpose
----------------------------------------------------------------
CI 与本地都装 ``uv sync --group dev --group test``，而 ``poethepoet``（dev 组）依赖 pyyaml。
若把 dev group 也算作提供者，本守卫**永远不会红** —— 那正是 #224 逃逸的通道。

已知边界 / Known limitations（措辞勿强于实现）
--------------------------------------------
- 只认 **AST 静态 import**（含函数内静态 import）：``importlib.import_module(...)`` 这类**动态**导入
  不可见 —— 如 ``computer/cli/utils.py`` 加载配置给出的 ``pkg.mod:attr``、``testing/server.py:195``
  加载 ``"werkzeug.serving"``。注意同一文件的 ``:166`` / ``:261`` 是**函数内静态** import，
  **在**扫描面内（靠判据 1 放行），勿据本条误以为 server extra 的成员未被覆盖。
- 可达根收纳**全部** extra，故本守卫只看「有无声明路径」，**不**区分 core / extra 归属。
  「核心路径 import 却只声明在 extra」这一反向错位需另行把关：针对 #224 的具体裁决，
  由 :func:`test_yaml_provider_is_a_core_dependency` 单点钉住（理由见 CHANGELOG：SKILL 是协议核心通道，
  ``parse_skill_frontmatter`` 由 ``Computer.boot_up()`` 的库路径调用）。
- **改名 + 未安装**的声明包仍会假红：判据 1 只兜住「import 名 == 发行包名」（PEP 503 归一化后），
  判据 2 需要该发行包**已安装**才能建映射。当前依赖集不受影响 —— 改名的成员只有
  ``pyyaml → yaml`` 与 ``python-socketio → socketio``，二者都在 core，随安装必然在场；
  三个 extra 的其余成员（keyring / typer / rich / prompt-toolkit / werkzeug / uvicorn）名字同形，
  由判据 1 覆盖。**但**若日后新增形如 ``python-dotenv``（模块 ``dotenv``）的 **extra** 成员且
  CI 不装该 extra，守卫会对合规的 pyproject 报违规。届时应扩判据（显式别名表）而非改 pyproject。
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from importlib.metadata import distributions, packages_distributions, requires
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

import a2c_smcp

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"


def _canonical(name: str) -> str:
    """PEP 503 归一化 / PEP 503 normalization (lowercase, collapse ``-_.`` runs)."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _parse_requirement(spec: str) -> Requirement | None:
    """解析依赖字符串；畸形条目返回 ``None``（守卫自身不应因元数据畸形而崩）/ Parse, or ``None``."""
    try:
        return Requirement(spec)
    except InvalidRequirement:
        return None


@dataclass(frozen=True)
class _Declaration:
    """``pyproject.toml`` 的声明依赖面 / The declared dependency surface (no dev/test groups)."""

    core: tuple[Requirement, ...]
    extras: dict[str, tuple[Requirement, ...]]

    @property
    def all_requirements(self) -> list[Requirement]:
        return [*self.core, *(req for members in self.extras.values() for req in members)]

    @property
    def core_names(self) -> set[str]:
        return {_canonical(req.name) for req in self.core}

    @property
    def all_names(self) -> set[str]:
        return self.core_names | {_canonical(req.name) for members in self.extras.values() for req in members}


def _load_declaration() -> _Declaration:
    """
    读 ``[project] dependencies`` + ``optional-dependencies``。

    **刻意不含** ``[dependency-groups]``（dev / test / build）：它们不属于「干净 pip 安装」的
    可达面，且 dev 组的 poethepoet 会带入 pyyaml —— 那正是 #224 得以逃逸的假提供者。
    """
    data: dict[str, Any] = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
    project: dict[str, Any] = data["project"]

    def _parse_all(specs: list[str]) -> tuple[Requirement, ...]:
        return tuple(req for req in (_parse_requirement(spec) for spec in specs) if req is not None)

    return _Declaration(
        core=_parse_all(project.get("dependencies", [])),
        extras={extra: _parse_all(members) for extra, members in project.get("optional-dependencies", {}).items()},
    )


def _default_requires(dist_name: str, extra: str = "") -> set[str]:
    """
    该发行包在指定 extra 上下文下**实际会被安装**的依赖集合。

    关键：marker 以 ``{"extra": extra}`` 求值，因此 ``foo ; extra == "bar"`` 这类
    **extra-gated 条目在默认上下文（``extra=""``）下被正确排除** —— pip / uv 的实际行为。
    """
    try:
        raw = requires(dist_name)
    except Exception:  # noqa: BLE001 - 元数据不可读时按「无依赖」处理，不阻断守卫
        return set()
    if not raw:
        return set()
    out: set[str] = set()
    for item in raw:
        req = _parse_requirement(item)
        if req is None:
            continue
        if req.marker is not None and not req.marker.evaluate({"extra": extra}):
            continue
        out.add(_canonical(req.name))
    return out


def _reachable_distributions(declaration: _Declaration | None = None) -> set[str]:
    """干净 pip 安装可达的发行包闭包（core 与各 extra 均在默认上下文展开）/ Reachable closure."""
    declaration = _load_declaration() if declaration is None else declaration
    roots = declaration.all_requirements
    reach: set[str] = {_canonical(req.name) for req in roots}
    pending: list[str] = list(reach)

    while pending:
        current = pending.pop()
        for dep in _default_requires(current):
            if dep not in reach:
                reach.add(dep)
                pending.append(dep)
    return reach


def _is_type_checking_test(node: ast.expr) -> bool:
    """``if TYPE_CHECKING:`` 判据（``Name`` 或 ``X.TYPE_CHECKING``）/ Detect the TYPE_CHECKING guard."""
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute):
        return node.attr == "TYPE_CHECKING"
    return False


class _ImportCollector(ast.NodeVisitor):
    """
    收集第三方顶层 import 及其 ``file:line`` 位置。

    TYPE_CHECKING 块内的 import **被跳过**：它们在运行时从不执行，不构成安装期需求
    （误报只会制造噪音），故「运行期可导入」才是本守卫的判据。
    仅覆盖静态 import 语句；``importlib.import_module`` 等动态导入不在扫描面（见模块 docstring 边界）。
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.found: dict[str, list[str]] = {}
        self._type_checking_depth = 0

    def _record(self, module: str, lineno: int) -> None:
        if self._type_checking_depth:
            return
        self.found.setdefault(module, []).append(f"{self.filename}:{lineno}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name.split(".")[0], node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # level > 0 = 相对导入（本包内部）/ relative import → skip
        if node.level or node.module is None:
            return
        self._record(node.module.split(".")[0], node.lineno)

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_test(node.test):
            self._type_checking_depth += 1
            try:
                for child in node.body:
                    self.visit(child)
            finally:
                self._type_checking_depth -= 1
            for child in node.orelse:  # else 分支仍属运行期 / the else branch still executes
                self.visit(child)
            return
        self.generic_visit(node)


def _scan_third_party_imports(package_dir: Path) -> dict[str, list[str]]:
    """
    AST 扫包内全部 ``.py`` / ``.pyi``，返回 ``{顶层 import 名: ["file:line", ...]}``。

    跳过 Python 标准库、本包自身（``a2c_smcp`` 及其子包）与相对导入。
    """
    stdlib = set(sys.stdlib_module_names)
    local_roots = {path.name for path in package_dir.iterdir()} | {package_dir.name}

    imports: dict[str, list[str]] = {}
    for path in sorted([*package_dir.rglob("*.py"), *package_dir.rglob("*.pyi")]):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        collector = _ImportCollector(str(path.relative_to(package_dir.parent)))
        collector.visit(tree)
        for module, locations in collector.found.items():
            if module in stdlib or module in local_roots:
                continue
            imports.setdefault(module, []).extend(locations)
    return imports


def _imports() -> dict[str, list[str]]:
    """本包源码的第三方顶层 import 扫描结果 / Third-party imports in the shipped package."""
    return _scan_third_party_imports(Path(a2c_smcp.__file__).parent)


def _providers() -> dict[str, set[str]]:
    """``顶层 import 名 -> 提供它的发行包集合``（stdlib 单一权威，免手维护别名表）"""
    return {module: {_canonical(dist) for dist in dists} for module, dists in packages_distributions().items()}


def _find_violations(providers: dict[str, set[str]] | None = None) -> list[str]:
    """
    返回无声明路径的 import 描述列表（空列表 = 全部合规）。

    判定见模块 docstring：先声明面直配，再安装映射回落。两条都不中才计违规。

    ``providers`` 可注入（默认取当前环境）：便于用例**复现另一种安装面**（如某个 extra 未装），
    而不必 monkeypatch 全局函数。
    """
    declaration = _load_declaration()
    reachable = _reachable_distributions(declaration)
    providers = _providers() if providers is None else providers

    violations: list[str] = []
    for module, locations in sorted(_imports().items()):
        # 1) 声明面直配：import 名本身就是已声明的发行包（不依赖它是否已安装）
        if _canonical(module) in declaration.all_names:
            continue
        # 2) 安装映射回落：提供者落在声明闭包内（覆盖 yaml→pyyaml 这类改名）
        candidates = providers.get(module, set())
        if candidates & reachable:
            continue

        if not candidates:
            why = (
                f"当前环境查不到 {module!r} 的发行包映射（未安装，或 import 名与发行包名不同形）"
                "——请核对它对应的发行包是否已写进 pyproject.toml"
            )
        else:
            why = f"提供者 {sorted(candidates)} 均不在声明依赖闭包内"
        violations.append(f"  import {module!r}: {why} ← {', '.join(locations)}")
    return violations


# ── 守卫本体 / The guard ──────────────────────────────────────────────────
def test_every_third_party_import_has_a_declaration_path() -> None:
    """
    #224 回归锁：源码里每个第三方 import 都必须在声明面有路径。

    红灯形态（修复前）：``yaml`` 既不是声明名、其提供者 ``pyyaml`` 也不在声明闭包里 ——
    因为 pyyaml 只由 dev 组的 poethepoet 带入，干净 pip 安装里根本不存在。
    """
    violations = _find_violations()
    assert not violations, (
        "以下第三方 import 在「干净 pip 安装」的声明依赖面上找不到路径。\n"
        "这意味着 `pip install a2c-smcp` 后 import 会 ModuleNotFoundError（#224 同款缺陷）。\n"
        "修法：把它对应的发行包加进 pyproject.toml —— 不要只加 mypy override。\n"
        "Undeclared third-party imports (no declaration path):\n" + "\n".join(violations)
    )


def test_guard_actually_scans_and_maps_imports() -> None:
    """
    正对照 / Positive control：证明守卫真的扫到了 import、也真的解析出了发行包映射。

    没有本用例时，任何让扫描面变空（路径写错、AST 走空、跳过逻辑过宽）的实现都会让
    主守卫**假绿** —— 本仓既有教训：「弱断言『只断言不存在』须配正对照」。
    """
    imports = _imports()
    providers = _providers()

    # 扫描面非空且达到合理规模（现状 20 个第三方顶层 import）
    assert len(imports) >= 10, f"扫描面异常小（{len(imports)} 个），守卫可能已失效：{sorted(imports)}"
    # #224 的那个 import 必须在扫描面上
    assert "yaml" in imports, f"未扫到 yaml，扫描面可疑：{sorted(imports)}"
    # 且映射层能把 import 名正确解析为发行包名（PEP 503 归一化）
    assert "pyyaml" in providers.get("yaml", set()), f"yaml 未映射到 pyyaml：{providers.get('yaml')}"
    # 对照一个「声明名与 import 名不同形」的既有正常项，防映射层退化
    assert "python-socketio" in providers.get("socketio", set()), f"socketio 映射异常：{providers.get('socketio')}"


def test_keyring_style_declared_but_uninstalled_import_passes() -> None:
    """
    #224 审查整改：**已声明但当前环境未装**的可选依赖不得被判违规。

    CI 的 ``uv sync --group dev --group test --extra cli`` 与本地 ``uv sync --all-groups``
    **都不装 ``keyring`` extra**（前者只装 cli extra，后者不装任何 extra），而 ``keyring`` 无人传递依赖它，
    故它在这两个环境里都不存在。若守卫只看 ``packages_distributions()``（**仅认识已安装发行包**）判定，
    每个 PR 都会因它恒红，且报错文案会误导为「把 keyring 加进 core 依赖」——而正确答案恰恰是
    「它本就该留在 extra」。

    ⚠️ **必须注入 providers**：开发机 ``.venv`` 里 keyring 通常是装着的（不属于上述两个安装面），
    只有在 CI 同款安装面上「未安装」才是问题形态 —— 故用注入复现，而不是依赖运行环境碰巧缺 keyring。

    本用例**真实驱动守卫**（``_find_violations()``），并把 keyring 从安装映射里抹掉以**复现 CI 安装面**
    （注入 providers 而非 monkeypatch 全局）。删掉「声明面直配」那一条判据即会让本用例变红 ——
    这正是它要钉住的退化。

    Regression lock driving the real guard: with keyring absent from the *installed* surface
    (the CI shape), it must not be reported as a violation. Removing the direct-declaration branch
    makes this test fail.
    """
    declaration = _load_declaration()
    # 前提（若前提不成立则本用例已失去意义，故硬断言而非跳过）
    assert "keyring" in declaration.all_names, "keyring 不在声明面，本用例前提失效"
    # ⚠️ 存活性前提：本锁靠「keyring 出现在**扫描面**」才会红。keyring 是 secret_store.py 里的
    #    **函数内静态 import**——日后若有人把扫描器收窄成「只收模块级 import」（视函数内 import 为
    #    惰性可选），本锁会静默变空转而 5 个用例仍全绿。故把该前提钉死在用例里。
    assert "keyring" in _imports(), "keyring 不在扫描面（扫描器被收窄？），本用例将静默失效"

    # 复现「keyring 未安装」：环境里根本没有它的安装映射
    ci_surface = {module: {dist for dist in dists if dist != "keyring"} for module, dists in _providers().items()}
    ci_surface.pop("keyring", None)

    offenders = [item for item in _find_violations(providers=ci_surface) if "'keyring'" in item]
    assert not offenders, (
        "keyring 是「已声明但当前环境未装」的可选依赖，不得判违规 —— 否则 CI 每个 PR 恒红。"
        "（判据应收敛到「声明面直配」，而非依赖 packages_distributions() 的已安装映射）\n"
        f"实际违规项：{offenders}"
    )


def test_yaml_provider_is_a_core_dependency() -> None:
    """
    #224 核心裁决回归锁：提供 ``yaml`` 的发行包必须在 **core** 依赖里，不得挪进 extra。

    理由（CHANGELOG 已论证）：SKILL 是协议核心通道，``parse_skill_frontmatter`` 由
    ``Computer.boot_up()`` 的**库路径**调用（``computer/computer.py`` → ``_restage_mcp_skills`` →
    ``stage_mcp_skills``），与 CLI 无关；放进 ``cli`` extra 只能修一半。

    本用例补上通用守卫的缺口：通用守卫的可达根收纳全部 extra，**不**区分 core / extra 归属，
    故「pyyaml 被挪进 cli extra」会让通用守卫与 CI 冒烟（装 ``[cli]``）**双绿**，
    而 ``pip install a2c-smcp`` + ``Computer.boot_up()`` 仍会在运行期崩。
    """
    declaration = _load_declaration()
    providers = _providers()

    candidates = providers.get("yaml", set())
    assert candidates, "环境内查不到 yaml 的发行包映射，无法判定归属（前提失效）"
    for dist in sorted(candidates):
        assert dist in declaration.core_names, (
            f"{dist} 只声明在 extras {sorted(declaration.extras)} 里，不在 [project] dependencies（core）。"
            "SKILL frontmatter 解析由库路径调用（Computer.boot_up），必须落 core —— 见 #224 / CHANGELOG。"
        )


def _find_gating_probe() -> tuple[str, frozenset[str], str, str] | None:
    """
    找一个可作 marker 不变量探针的发行包，返回 ``(发行包, 默认依赖名集, gated 依赖名, marker 文本)``。

    选定条件（**只看原始元数据**，不回落到被测函数，否则断言会同源而永真）：

    1. 该发行包**有**默认上下文会安装的依赖（正向腿用：实现若恒返回空集即被抓住）；
    2. 该发行包**有**默认上下文不安装的依赖，且该名字**不在**默认依赖集里 —— 这一条是关键：
       ``mcp`` 的 ``pydantic`` 同时出现在 ``python_version < '3.14'``（默认装）与
       ``python_version >= '3.14'``（默认不装）两侧，若不加此过滤，探针轮到 ``mcp`` 时会对
       **完全正确**的实现假红；而 ``importlib.metadata.distributions()`` 的枚举顺序来自
       ``os.listdir``（非排序、跨文件系统不保证），故必须从判据上根除而非赌顺序。
    3. 优先取 marker 文本**真含 ``extra ==``** 的候选 —— 否则选中的可能只是 env-marker
       （如 ``uvicorn`` 的 ``typing-extensions ; python_version < '3.11'``），那样「extra 语义」
       实际未被覆盖。
    """
    fallback: tuple[str, frozenset[str], str, str] | None = None
    for dist in distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        try:
            raw_requirements = requires(name) or []
        except Exception:  # noqa: BLE001 - 元数据不可读则跳过该发行包
            continue

        default_names: set[str] = set()
        gated: list[tuple[str, str, str]] = []
        for raw in raw_requirements:
            req = _parse_requirement(raw)
            if req is None:
                continue
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                default_names.add(_canonical(req.name))
            else:
                gated.append((_canonical(req.name), str(req.marker), raw))

        if not default_names:
            continue  # 无正向腿素材
        usable = [item for item in gated if item[0] not in default_names]
        if not usable:
            continue  # 无合法负向腿素材（mcp 即此形态）

        extra_gated = [item for item in usable if "extra" in item[1]]
        chosen = (extra_gated or usable)[0]
        candidate = (name, frozenset(default_names), chosen[0], chosen[1])
        if extra_gated:
            return candidate  # 最优：真 extra-gated，直接采用
        fallback = fallback or candidate

    return fallback


def test_marker_gated_requirements_are_neither_installed_nor_dropped() -> None:
    """
    marker 求值不变量 / Marker-evaluation invariant —— 本守卫的成败点，**双向**断言。

    负向腿：默认上下文不安装的 gated 依赖，不得出现在 :func:`_default_requires`。
    若实现退化成「忽略 marker」，守卫会顺着 ``mcp → starlette → pyyaml ; extra == "full"`` 与
    ``watchdog → PyYAML ; extra == "watchmedo"`` 认为 pyyaml 可达 —— 即对 #224 假绿。
    正向腿：默认上下文该装的依赖必须在（否则「恒返回空集」这种退化也能骗过负向腿）。

    探针**必然存在**（``watchdog`` 是 core 恒装依赖且带 ``PyYAML ; extra == 'watchmedo'``；
    ``mcp`` / ``starlette`` / ``pydantic`` 等亦带 extra-gated 条目），故「找不到探针」按**硬失败**处理，
    不设 ``pytest.skip`` 逃生口 —— 实现退化时探针恰好会消失，跳过即等于放行假绿。
    """
    probe = _find_gating_probe()
    assert probe is not None, (
        "环境内找不到可用的 gating 探针，无法验证 marker 不变量。"
        "core 依赖 watchdog 带 'PyYAML ; extra == \"watchmedo\"'，正常情况下必然命中 —— "
        "本用例刻意不 skip：跳过恰好会在实现退化时发生，那正是要抓的时刻。"
        "No gating probe found; refusing to skip (a skipped probe is how this invariant silently rots)."
    )

    dist_name, default_names, gated_name, gated_marker = probe
    actual = _default_requires(dist_name)

    missing = sorted(default_names - actual)
    assert not missing, (
        f"正向腿：{dist_name} 的默认依赖 {missing} 未出现在 _default_requires 中 —— "
        "实现可能退化成恒返回空集（那样负向腿会恒真而失去意义）。"
    )

    assert gated_name not in actual, (
        f"marker 未生效：{dist_name} 的 gated 依赖 {gated_name}（marker: {gated_marker}）"
        "被当成了默认安装项。忽略 marker 会让主守卫对 #224 假绿"
        "（会把 `pyyaml ; extra == 'full'` / `PyYAML ; extra == 'watchmedo'` 当成可达）。"
    )
