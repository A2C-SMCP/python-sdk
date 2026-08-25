# -*- coding: utf-8 -*-
# filename: validator.py
# @Time    : 2026/08/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
marketplace / plugin 配置独立校验器（#193）：零副作用 + 全量收集 / Side-effect-free manifest validator.

协议依据 / Protocol: tfrobot-marketplace docs/marketplace/protocol.md §3（marketplace.json 字段）/ §4（plugin
                      entry）/ §5（plugin source 5 类）/ §6（plugin.json）+ loading-behavior.md §1（plugin.json
                      × strict 组合矩阵）/ §2（缺失兜底）；mcp-servers 协议 §8（经 manifest 解析器间接）。
SDK 设计 / Design: python-sdk #193（用户裁决：全量收集 + 远程 source 跳过记 warning + 契约面含 SKILL 深检）。

**定位**：把「安装路径能容忍什么」翻译成「作者发布前该修什么」。与装载路径的关系：

- **规则单一权威在既有解析器**——:mod:`~a2c_smcp.computer.skills.manifest`（manifest / strict 冲突 /
  bundled server）、:mod:`~a2c_smcp.computer.skills.sources`（source 5 类）、
  :func:`~a2c_smcp.computer.skills.staging.parse_skill_frontmatter`（SKILL frontmatter）、
  :func:`a2c_smcp.computer.settings.schema.is_valid_marketplace_name`（kebab 判据）。本模块**只编排、不另立
  规则**（§3/§4/§6 的必填 / 类型表是装载器不校验的静态契约，此处按协议表补位）。
- **显式报出装载路径静默吞掉的面**（#193 核心价值）：``plugins[]`` 畸形条目（install 静默跳过）、损坏的
  ``plugin.json``（install WARN 后按 ``{}`` 兜底，loading-behavior §1 note 指出这会掩盖作者语法 bug）。
- **severity 对齐装载姿态**：装载 WARN / 静默跳过的 → warning；装载 ERROR / 硬抛的 → error。
  ``plugin.json`` 缺失在 loading-behavior §1 矩阵**全组合合法**→ warning（强烈建议保留，§2 最佳实践）。
  **显式例外**：作者**显式声明**的引用路径（``skills`` 覆写等）缺失 / 越界，装载侧虽只 WARNING 跳过，
  validator 记 **error**——「声明的静默丢失」属应修错误，非约定缺省的容忍范围（勿按上句原则「纠正」回去）。
- **字段面取舍**：§4.2/§6.2 的 ``author`` / ``keywords`` / ``tags``、§3.2 ``allowCrossMarketplaceDependenciesOn``
  等可选元数据的深检**不在本期范围**（验收②「至少覆盖」线内的显式取舍，装载路径亦不消费其类型）。

**零副作用**：不 clone、不写账本、不碰 settings / 物化状态；远程 source（url/github/cnb/git-subdir）**跳过
深检**、记 warning（仅校验 source 字段自身格式）——联网校验属未来 ``--fetch`` 选项，不在 #193 范围。

**全量收集**：单文件 JSON 语法错不终止整个 run（该文件后续检查跳过、其余文件继续）；单 plugin 深检失败不
阻断其余 entry。exit-码语义由 CLI 层（:mod:`~a2c_smcp.computer.cli.commands.validate`）赋予。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from a2c_smcp.computer.settings.schema import is_valid_marketplace_name
from a2c_smcp.computer.skills.manifest import (
    MARKETPLACE_MANIFEST,
    MARKETPLACE_MANIFEST_DIR,
    MCP_INPUTS_FILENAME,
    MCP_SERVERS_SUBDIR,
    PLUGIN_MANIFEST,
    PluginManifestError,
    check_strict_conflict,
    entry_is_strict,
    enumerate_bundled_server_files,
    parse_bundled_server,
    plugin_root_base,
    skill_override_raw_paths,
)
from a2c_smcp.computer.skills.naming import is_valid_skill_name
from a2c_smcp.computer.skills.sources import (
    GitCloneSpec,
    LocalPluginSource,
    SkillSourceError,
    resolve_plugin_source,
)
from a2c_smcp.computer.skills.staging import SKILL_MD, SKILLS_SUBDIR, parse_skill_frontmatter
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import is_within

logger = get_logger(__name__)

# 校验模式 / Validation modes（路径自动识别：marketplace.json 在 → marketplace；否则按 plugin 形态）。
ValidateMode = Literal["marketplace", "plugin"]
Severity = Literal["error", "warning"]

# ── 诊断码（JSON 输出契约面，CLI / CI 消费；语义见各发射点）/ diagnostic codes ────────────────────
CODE_NOT_A_TARGET = "not-a-marketplace-or-plugin"  # 路径既非 marketplace 根也非 plugin 目录
CODE_UNREADABLE = "unreadable-file"  # 读盘失败（权限等；非「不存在」）
CODE_JSON_SYNTAX = "json-syntax"  # JSON 语法错（message 含行/列）
CODE_ROOT_NOT_OBJECT = "json-root-not-object"  # JSON 根不是 object
CODE_MISSING_FIELD = "missing-required-field"  # 协议必填字段缺失（§3.1/§4.1/§6.1/§3.3）
CODE_INVALID_TYPE = "invalid-type"  # 字段类型不符协议表（§3/§4/§6 静态契约补位）
CODE_INVALID_NAME = "invalid-name"  # 名称非严格 kebab（§2.1 字符集）
CODE_INVALID_SOURCE = "invalid-source"  # plugin source 5 类形态非法（§5，SkillSourceError）
CODE_PATH_MISSING = "path-missing"  # 引用路径不存在（本地 plugin 根 / 显式声明的 skills 覆写目录）
CODE_PATH_ESCAPE = "path-escape"  # 引用路径越出所属根（防穿越）
CODE_STRICT_CONFLICT = "strict-conflict"  # strict=false 且 plugin.json 声明组件（§4.4 硬错误）
CODE_INVALID_MCP_SERVER = "invalid-mcp-server"  # mcp-servers/<n>.json 校验失败 / 文件名≠name（§8）
CODE_SKILL_NAME_INVALID = "invalid-skill-name"  # skill 目录名非严格 kebab（装载 ERROR 跳过）
CODE_SKILL_DESC_MISSING = "skill-description-missing"  # SKILL.md frontmatter 缺 description（装载 ERROR 跳过）
CODE_SKILL_MD_UNREADABLE = "skill-md-unreadable"  # SKILL.md 读盘失败（装载 ERROR 跳过）
CODE_DUPLICATE_SKILL = "duplicate-skill-name"  # 同 plugin 内合成 skill 名重复（装载 keep-first ERROR）
# warnings（装载可容忍 / 合法但值得提示）/ warnings (tolerated at load time)
CODE_PLUGIN_MANIFEST_MISSING = "plugin-manifest-missing"  # plugin.json 缺失（合法；建议保留，loading §2）
CODE_REMOTE_SOURCE_SKIPPED = "remote-source-skipped"  # git 远程 source 跳过深检（#193 裁决）
CODE_NO_SKILL_CONTAINER = "no-skill-container"  # 无任何 SKILL 容器（MCP-only plugin 合法；装载 WARNING）
CODE_SKILL_MD_MISSING = "skill-md-missing"  # 容器内目录无 SKILL.md（装载静默跳过）
CODE_DUPLICATE_PLUGIN_NAME = "duplicate-plugin-name"  # plugins[] 同名 entry（装载 take-first 静默）


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """单条诊断 / One diagnostic（``file`` / ``location`` 双轴定位到配置内的具体位置）。

    :ivar code: 诊断码（本模块 ``CODE_*`` 常量；JSON 输出契约面）。
    :ivar file: 相对被校验根的 POSIX 路径（根自身用 ``"."``）。
    :ivar location: 文件内定位（JSON 路径如 ``plugins[2].source``；整文件级用 ``"."``）。
    :ivar message: 人类可读原因（含底层解析器原始 message）。
    :ivar severity: ``error``（装载会失败/降级 ERROR）/ ``warning``（装载容忍但作者应知晓）。
    """

    code: str
    file: str
    location: str
    message: str
    severity: Severity = "error"


@dataclass(slots=True)
class ValidationResult:
    """一次 :func:`validate_path` 的完整结果 / Full result of one validation run.

    :ivar root: 被校验路径（resolve 后绝对路径；JSON 输出用）。
    :ivar mode: 识别出的模式；路径不可识别时为 ``None``（仅 ``not-a-marketplace-or-plugin`` 一条 error）。
    :ivar issues: 全量诊断（error + warning，按发现顺序）。
    :ivar checked: 实际检查过且**可读**的文件（相对 root 的 POSIX 路径；语法错文件不在内——它没被消费）。
    """

    root: Path
    mode: ValidateMode | None
    issues: list[ValidationIssue] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """无 error 即 valid（warnings 不影响）/ Valid iff no error-severity issues."""
        return not self.errors

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


class _Collector:
    """result 的过程态封装（rel 路径归一 + issue 追加）/ Mutable accumulator over ValidationResult."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.result = ValidationResult(root=self.root, mode=None)

    def rel(self, p: Path) -> str:
        """绝对路径 → 相对 root 的 POSIX 串（越界时退回绝对串，防 crash）/ Absolute → root-relative POSIX."""
        try:
            return p.relative_to(self.root).as_posix() or "."
        except ValueError:
            return p.as_posix()

    def add(self, code: str, file: str, location: str, message: str, *, severity: Severity = "error") -> None:
        self.result.issues.append(ValidationIssue(code=code, file=file, location=location, message=message, severity=severity))

    def checked_file(self, p: Path) -> None:
        rel = self.rel(p)
        if rel not in self.result.checked:
            self.result.checked.append(rel)


# ── JSON 读取（strict：语法/根类型错记 issue 后返回 None）/ strict JSON object read ──────────────
def _load_json_object(path: Path, c: _Collector) -> dict[str, Any] | None:
    """读 JSON 要求 object 根；失败记 issue（语法/根型/读盘）并返回 ``None``（调用方跳过后续检查）。"""
    rel = c.rel(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        c.add(CODE_UNREADABLE, rel, ".", f"cannot read {rel}: {e}")
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        c.add(CODE_JSON_SYNTAX, rel, ".", f"invalid JSON in {rel}: {e}")
        return None
    if not isinstance(data, dict):
        c.add(CODE_ROOT_NOT_OBJECT, rel, ".", f"root of {rel} is {type(data).__name__}, not an object")
        return None
    c.checked_file(path)
    return data


def _require_str_field(
    data: Mapping[str, Any],
    key: str,
    file: str,
    c: _Collector,
    *,
    location: str | None = None,
    kebab: bool = False,
) -> str | None:
    """必填字符串字段检查（缺失/非串 → 2 类 issue；``kebab=True`` 追加命名规则检查）/ Required-string check.

    返回合法值（否则 ``None``，调用方降级继续）。location 缺省 = ``key``（可传 ``plugins[2].name`` 等前缀）。
    """
    loc = location or key
    if key not in data or data.get(key) is None:
        c.add(CODE_MISSING_FIELD, file, loc, f"required field {key!r} is missing")
        return None
    v = data.get(key)
    if not isinstance(v, str) or not v.strip():
        c.add(CODE_INVALID_TYPE, file, loc, f"field {key!r} must be a non-empty string (got {type(v).__name__})")
        return None
    if kebab and not is_valid_marketplace_name(v.strip()):
        c.add(CODE_INVALID_NAME, file, loc, f"{key} {v!r} must be strict kebab-case ([a-z0-9-], 1-64, no leading/trailing/double '-')")
        return None
    return v.strip()


def _optional_str_field(data: Mapping[str, Any], key: str, file: str, c: _Collector, *, location: str | None = None) -> None:
    """可选字段存在时的类型检查（协议 §3.2/§4.2/§6.2 静态契约补位）/ Optional-field type check."""
    if key in data and data.get(key) is not None and not isinstance(data.get(key), str):
        c.add(CODE_INVALID_TYPE, file, location or key, f"optional field {key!r} must be a string (got {type(data.get(key)).__name__})")


# ── 入口：路径自动识别 / entry point with mode detection ──────────────────────────────────────
def validate_path(root: Path) -> ValidationResult:
    """校验本地 marketplace 根或 plugin 目录（自动识别）/ Validate a local marketplace root or plugin dir.

    识别顺序（先到先得）/ Detection order:
    1. ``<root>/.tfrobot-plugin/marketplace.json`` 存在 → marketplace 模式（目录同时是 plugin 时 marketplace 优先）；
    2. ``<root>/.tfrobot-plugin/plugin.json`` 存在，或 ``skills/`` / ``mcp-servers/`` 任一存在 → plugin 模式
       （无 plugin.json 时记 warning——loading-behavior §1 矩阵全组合合法）；
    3. 否则一条 ``not-a-marketplace-or-plugin`` error。

    :param root: 待校验目录（不存在 / 非目录 → 同上 error）。
    """
    c = _Collector(Path(root))
    if not c.root.is_dir():
        c.add(CODE_NOT_A_TARGET, ".", ".", f"path {c.root} is not an existing directory")
        return c.result

    mpath = c.root / MARKETPLACE_MANIFEST_DIR / MARKETPLACE_MANIFEST
    ppath = c.root / MARKETPLACE_MANIFEST_DIR / PLUGIN_MANIFEST
    if mpath.is_file():
        c.result.mode = "marketplace"
        _validate_marketplace(c)
    elif ppath.is_file() or (c.root / SKILLS_SUBDIR).is_dir() or (c.root / MCP_SERVERS_SUBDIR).is_dir():
        c.result.mode = "plugin"
        _validate_plugin(c, c.root, entry=None, entry_loc=None, plugin_name=None)
    else:
        c.add(
            CODE_NOT_A_TARGET, ".", ".",
            f"{c.root} has no .tfrobot-plugin/marketplace.json, .tfrobot-plugin/plugin.json, "
            "skills/ or mcp-servers/ — not a marketplace root or plugin dir",
        )
    return c.result


# ── marketplace 模式（§3 marketplace.json + §4 entries 下钻）/ marketplace mode ─────────────────
def _validate_marketplace(c: _Collector) -> None:
    mpath = c.root / MARKETPLACE_MANIFEST_DIR / MARKETPLACE_MANIFEST
    mfile = c.rel(mpath)
    manifest = _load_json_object(mpath, c)
    if manifest is None:
        return

    # §3.1 必填：name（kebab）/ owner / plugins；§3.3 owner.name；§3.2 可选类型补位。
    _require_str_field(manifest, "name", mfile, c, kebab=True)
    _optional_str_field(manifest, "description", mfile, c)
    _optional_str_field(manifest, "version", mfile, c)
    owner = manifest.get("owner")
    if owner is None:
        c.add(CODE_MISSING_FIELD, mfile, "owner", "required field 'owner' is missing")
    elif not isinstance(owner, Mapping):
        c.add(CODE_INVALID_TYPE, mfile, "owner", f"field 'owner' must be an object (got {type(owner).__name__})")
    else:
        _require_str_field(owner, "name", mfile, c, location="owner.name")
    metadata = manifest.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        c.add(CODE_INVALID_TYPE, mfile, "metadata", f"field 'metadata' must be an object (got {type(metadata).__name__})")
    elif isinstance(metadata, Mapping):
        pr = metadata.get("pluginRoot")
        if pr is not None and (not isinstance(pr, str) or not pr.strip()):
            c.add(CODE_INVALID_TYPE, mfile, "metadata.pluginRoot", "field 'pluginRoot' must be a non-empty string")

    plugins = manifest.get("plugins")
    if plugins is None:
        c.add(CODE_MISSING_FIELD, mfile, "plugins", "required field 'plugins' is missing")
        return
    if not isinstance(plugins, list):
        c.add(CODE_INVALID_TYPE, mfile, "plugins", f"field 'plugins' must be an array (got {type(plugins).__name__})")
        return

    base = plugin_root_base(manifest)  # 归一容错（非法 pluginRoot 已按上表记 issue；缺省 ./plugins）
    seen_names: set[str] = set()
    for i, entry in enumerate(plugins):
        loc = f"plugins[{i}]"
        if not isinstance(entry, Mapping):
            # install 路径 iter_plugin_entries 静默跳过非对象条目——validator 显式报出（#193 核心价值）。
            c.add(
                CODE_INVALID_TYPE, mfile, loc,
                f"plugin entry must be an object (got {type(entry).__name__}); silently skipped at install time",
            )
            continue
        name = _require_str_field(entry, "name", mfile, c, location=f"{loc}.name", kebab=True)
        if name is not None and name in seen_names:
            # find_plugin_entry take-first：后续同名 entry 永不可达。
            c.add(
                CODE_DUPLICATE_PLUGIN_NAME, mfile, f"{loc}.name",
                f"duplicate plugin name {name!r} (install resolves the first matching entry only)",
                severity="warning",
            )
        if name is not None:
            seen_names.add(name)
        strict_val = entry.get("strict")
        if strict_val is not None and not isinstance(strict_val, bool):
            c.add(CODE_INVALID_TYPE, mfile, f"{loc}.strict", f"field 'strict' must be a boolean (got {type(strict_val).__name__})")
        _optional_str_field(entry, "description", mfile, c, location=f"{loc}.description")
        _optional_str_field(entry, "version", mfile, c, location=f"{loc}.version")
        _validate_entry_source(c, entry, loc, mfile, name, base)


def _validate_entry_source(c: _Collector, entry: Mapping[str, Any], loc: str, mfile: str, name: str | None, base: str) -> None:
    """单个 plugin 条目的 source 解析与下钻（§4.1/§5）/ Resolve one entry's source and dispatch deep checks."""
    if "source" not in entry or entry.get("source") is None:
        c.add(CODE_MISSING_FIELD, mfile, f"{loc}.source", "required field 'source' is missing")
        return
    try:
        resolved = resolve_plugin_source(entry.get("source"), plugin_root=base)
    except SkillSourceError as e:
        c.add(CODE_INVALID_SOURCE, mfile, f"{loc}.source", str(e))
        return

    if isinstance(resolved, GitCloneSpec):
        # 远程 source：仅 source 字段自身已校验；plugin 本体不在本地，跳过深检（#193 裁决）。
        c.add(
            CODE_REMOTE_SOURCE_SKIPPED, mfile, f"{loc}.source",
            f"remote plugin source {resolved.url!r} skipped (local-only validation; source field format checked)",
            severity="warning",
        )
        return

    assert isinstance(resolved, LocalPluginSource)
    plugin_root = (c.root / resolved.rel_path).resolve()
    if not is_within(plugin_root, c.root):
        # resolve_plugin_source 已禁 '..'，此处与 locate_plugin_root 同款防御（belt + suspenders）。
        c.add(CODE_PATH_ESCAPE, mfile, f"{loc}.source", f"relative plugin source {resolved.rel_path!r} escapes the marketplace root")
        return
    if not plugin_root.is_dir():
        c.add(
            CODE_PATH_MISSING, mfile, f"{loc}.source",
            f"local plugin directory not found: {resolved.rel_path!r} (resolved to {plugin_root})",
        )
        return
    _validate_plugin(c, plugin_root, entry=entry, entry_loc=loc, plugin_name=name)


# ── plugin 深检（§6 plugin.json + §8 mcp-servers + SKILL 深检；两种模式共用）/ plugin deep check ──
def _validate_plugin(
    c: _Collector,
    plugin_root: Path,
    *,
    entry: Mapping[str, Any] | None,
    entry_loc: str | None,
    plugin_name: str | None,
) -> None:
    """校验单个 plugin 目录：plugin.json（§6）+ strict 冲突（§4.4）+ mcp-servers（§8）+ skills 深检.

    ``entry=None`` → 独立 plugin 模式（无条目上下文：不做 strict 冲突、无 entry.skills 覆写；strict 恒
    true → plugin.json.skills 覆写仍生效）；``plugin_name`` 用于 skill 合成名去重（marketplace 模式 =
    entry.name；独立模式回退 plugin.json.name，再缺则按裸名去重）。
    """
    ppath = plugin_root / MARKETPLACE_MANIFEST_DIR / PLUGIN_MANIFEST
    pfile = c.rel(ppath)
    plugin_manifest: dict[str, Any] = {}
    manifest_name: str | None = None
    if not ppath.is_file():
        # loading-behavior §1：plugin.json 缺失全组合合法装载（§2 兜底）→ warning（§2 最佳实践：强烈建议保留）。
        c.add(
            CODE_PLUGIN_MANIFEST_MISSING, pfile, ".",
            f"{pfile} not found (loads with a minimal fallback manifest; keeping one is strongly recommended)",
            severity="warning",
        )
    else:
        parsed = _load_json_object(ppath, c)
        if parsed is not None:
            # §6.1 name 必填（plugin.json 存在时——**空对象 {} 同样须报**，`or {}` 会把「写了文件没写内容」
            # 折叠成缺失合法）；损坏的 plugin.json 装载路径 WARN 后按 {} 兜底——此处显式报出。
            plugin_manifest = parsed
            manifest_name = _require_str_field(plugin_manifest, "name", pfile, c, kebab=True)
            _optional_str_field(plugin_manifest, "description", pfile, c)
            _optional_str_field(plugin_manifest, "version", pfile, c)

    effective_name = plugin_name or manifest_name

    # strict 冲突（§4.4，硬错误；install 在写账本前硬抛）。独立模式无 entry → 无从冲突，跳过。
    if entry is not None:
        try:
            check_strict_conflict(entry, plugin_manifest)
        except PluginManifestError as e:
            src_file = mfile_of_entry(c, entry_loc)
            c.add(CODE_STRICT_CONFLICT, src_file, f"{entry_loc}.strict", str(e))

    # skills 容器集：约定 skills/（装载恒扫，staging.py:1079 同源）+ 显式覆写（§4.3）。
    strict = entry_is_strict(entry) if entry is not None else True
    containers: list[Path] = [plugin_root / SKILLS_SUBDIR]
    raw_overrides = skill_override_raw_paths(entry or {}, plugin_manifest, strict=strict)
    override_loc = f"{entry_loc}.skills" if entry_loc else "skills"
    override_src = mfile_of_entry(c, entry_loc) if entry_loc else c.rel(ppath)
    plugin_root_resolved = plugin_root.resolve()
    for raw in raw_overrides:
        cand = (plugin_root / raw).resolve()
        if not is_within(cand, plugin_root_resolved):
            # 装载路径 resolve_skill_override_dirs 记 ERROR 跳过——validator 同级 error。
            c.add(CODE_PATH_ESCAPE, override_src, override_loc, f"skills override path {raw!r} escapes plugin root {pfile}")
        elif not cand.is_dir():
            c.add(
                CODE_PATH_MISSING, override_src, override_loc,
                f"skills override path {raw!r} is not an existing directory under the plugin",
            )
        elif cand not in containers:
            containers.append(cand)

    # mcp-servers/*.json（§8）：pydantic 校验 + 文件名==name；inputs.json 仅语法/object 根（入池语义归 #65）。
    for sf in enumerate_bundled_server_files(plugin_root):
        sfile = c.rel(sf)
        try:
            parse_bundled_server(sf)
            c.checked_file(sf)
        except PluginManifestError as e:
            c.add(CODE_INVALID_MCP_SERVER, sfile, ".", str(e))
    inputs_path = plugin_root / MCP_SERVERS_SUBDIR / MCP_INPUTS_FILENAME
    if inputs_path.is_file():
        _load_json_object(inputs_path, c)  # 语法/根型错即报；语义（入池）不在 #193 范围

    _validate_skills(c, plugin_root, containers, effective_name)


def mfile_of_entry(c: _Collector, entry_loc: str | None) -> str:
    """条目定位回落到 marketplace.json 的文件轴（issue 的 file 轴）/ File axis for entry-level issues."""
    return c.rel(c.root / MARKETPLACE_MANIFEST_DIR / MARKETPLACE_MANIFEST) if entry_loc else c.rel(c.root)


def _validate_skills(c: _Collector, plugin_root: Path, containers: list[Path], plugin_name: str | None) -> None:
    """SKILL 深检（容器去重 → 逐 skill 目录）/ Deep SKILL checks.

    severity 对齐装载姿态（staging._scan_and_register_plugin_skills）：SKILL.md 缺失 → 装载**静默** continue
    → warning；description 缺失 / 名字非法 / 跨容器重名 → 装载 ERROR 跳过 → error。容器内**文件**（非目录）
    装载忽略 → validator 亦忽略。容器全缺 → 装载 WARNING（MCP-only plugin 合法）→ warning。
    """
    seen_dirs: set[Path] = set()
    unique: list[Path] = []
    for d in containers:  # 与装载同款 resolve 去重保序（约定径与覆写径同径只查一次）。
        rp = d.resolve()
        if rp in seen_dirs:
            continue
        seen_dirs.add(rp)
        unique.append(d)

    existing = [d for d in unique if d.is_dir()]
    if not existing:
        c.add(
            CODE_NO_SKILL_CONTAINER, c.rel(plugin_root), ".",
            f"no SKILL container dir found (convention {SKILLS_SUBDIR}/ + declared overrides all absent; "
            "MCP-only plugins are legal)",
            severity="warning",
        )
        return

    seen_skill_names: dict[str, str] = {}
    for container in existing:
        try:
            children = sorted(p for p in container.iterdir() if p.is_dir())
        except OSError as e:
            # 容器存在但不可列（权限等）：记 issue 继续，不 traceback 中断全量收集（collect-all 承诺）。
            c.add(CODE_UNREADABLE, c.rel(container), ".", f"cannot list {c.rel(container)}: {e}")
            continue
        for skill_dir in children:
            skill_md = skill_dir / SKILL_MD
            if not skill_md.is_file():
                c.add(
                    CODE_SKILL_MD_MISSING, c.rel(skill_dir), ".",
                    f"skill dir has no {SKILL_MD} (silently skipped at load time; auxiliary dirs are tolerable)",
                    severity="warning",
                )
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError as e:
                c.add(CODE_SKILL_MD_UNREADABLE, c.rel(skill_md), ".", f"cannot read {c.rel(skill_md)}: {e}")
                continue
            c.checked_file(skill_md)
            frontmatter = parse_skill_frontmatter(text)
            if not frontmatter.get("description"):
                c.add(
                    CODE_SKILL_DESC_MISSING, c.rel(skill_md), "frontmatter.description",
                    "SKILL.md frontmatter is missing a non-empty 'description' (skill would be skipped at load time)",
                )
            leaf = skill_dir.name
            if not is_valid_skill_name(leaf):
                # 1 段形态判据 = naming §1.4 严格 kebab（与 synthesize_marketplace_name 的 leaf 段同字符集）。
                c.add(
                    CODE_SKILL_NAME_INVALID, c.rel(skill_dir), ".",
                    f"skill dir name {leaf!r} is not strict kebab-case (skill name synthesis fails at load time)",
                )
                continue
            synth = f"{plugin_name}:{leaf}" if plugin_name else leaf
            if synth in seen_skill_names:
                first_at = seen_skill_names[synth]
                c.add(
                    CODE_DUPLICATE_SKILL, c.rel(skill_dir), ".",
                    f"duplicate skill name {synth!r} (first seen at {first_at!r}; later ones are dropped at load time)",
                )
            else:
                seen_skill_names[synth] = c.rel(skill_dir)
