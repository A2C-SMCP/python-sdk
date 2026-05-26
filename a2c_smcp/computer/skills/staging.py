# -*- coding: utf-8 -*-
# filename: staging.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL staging：mcp 源物化 + user 源 DropIn 就地发现（v0.2.1）
SKILL staging: mcp-source materialization + user-source in-place DropIn discovery (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §3（MCP source 模式 mounted/archive/
                      resources）、§4（包根目录名 = frontmatter.name）、§12（Computer 完整消费 cursor）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §5.2；
                   docs/design-0.2.1-cli-marketplace-ux.md §2.3 / §5.0（user 源 DropIn）。

本模块实现三源 staging：**mcp 源** 物化（#59）、**user 源** DropIn 发现（#60）、**marketplace git 源**
clone/refresh + plugin 扫描（#61，依 plugin source 5 类，见 :mod:`~a2c_smcp.computer.skills.sources`）。
This module implements three-source staging: **mcp-source** materialization (#59), **user-source**
DropIn discovery (#60), and **marketplace git-source** clone/refresh + plugin scan (#61).

marketplace 流程 / marketplace flow（对账编排 / additive-only diff 归 reconciler #62，本模块只提供「clone 单个
marketplace + 解析 plugin source + 扫描 skills + 注册」原语）：
1. 按 ``{type:"git", url}`` ``git clone --depth 1``（SSH→HTTPS 回退、``GIT_TERMINAL_PROMPT=0``、超时默认
   120s）到 ``<home>/marketplace/<mp>/``；已存在且 ``refresh`` → ``git pull`` 失败则全量重 clone；
2. 读 ``.tfrobot-plugin/marketplace.json`` 枚举 ``plugins[]``（``metadata.pluginRoot`` 默认 ``./plugins``）；
3. 每个 plugin：``resolve_plugin_source`` 定位 plugin 根（相对路径在 clone 内 / ``git-subdir`` sparse clone /
   ``url``·``github``·``cnb`` 独立 clone 到 ``<home>/marketplace/.plugins/<mp>/<plugin>/``）；
4. 扫 ``<plugin 根>/skills/<skill>/SKILL.md``，``name = <plugin>:<skill>``（``<plugin>`` = entry.name、
   ``<skill>`` = skill 目录 basename，frontmatter 仅作显示名、不改 ID）→ 注册进 :class:`SkillRegistry`；
5. 写 ``known_marketplaces.json`` 物化记录（installLocation / commitSha / lastUpdated / autoUpdate）。
``installed_plugins.json`` 写入、``enabledPlugins`` 过滤、bundled MCP server 注册**不在本模块**（归 #62 /
plugin install / #53）；``plugin_filter`` 形参供 #62 注入 ``enabledPlugins ∩ installed``。

**不在本模块（显式延后，见 #80）**：marketplace 条目 / plugin.json 的 ``skills`` 组件路径**覆写**
（protocol-v1 §4.3）与 **strict mode 冲突检测**（§4.4：``strict=false`` + plugin.json 声明组件 → 硬错误）。
本模块只按约定扫 ``<plugin 根>/skills/<skill>/SKILL.md``（主流场景），strict 语义随组件加载层（#53）跟进。

mcp 流程 / mcp flow：
1. 经 ``manager.list_skill_resources`` 完整消费 cursor 拿到 server 全量 ``skill://`` 资源；
2. 其中带 ``_meta.source∈{mounted,archive,resources}`` 者为 **SKILL 根**，按模式物化到
   ``<home>/mcp/<normalized-server>/<skill>/``（marketplace SKILL v1 §2 包结构）：
   - **mounted**：``_meta.mount_dir`` 本地目录 → 复制进 staging（自包含，避免符号链接绕过沙箱）；
   - **archive**：HTTP 拉 ``_meta.archive_uri`` → 校验 ``archive_sha256``（若有）+ 大小/解压/成员数上限（防 tar·zip bomb）
     → 安全解包（防穿越/拒符号链接）；
   - **resources**：枚举 ``skill://<root>/**`` 子资源，逐个 ``resources/read``，按相对路径安全写入 staging。
3. 读 staged ``SKILL.md`` 的 YAML frontmatter 作为元数据权威源（§3：不镜像进 ``_meta``）；
   包根目录名校正为 ``frontmatter.name``（§4）；
4. 合成 ``A2CSkillRef``（name = ``mcp:<normalized-server>:<frontmatter.name>``）→ 注册进 :class:`SkillRegistry`。

user 流程 / user flow（与 mcp 的关键差异）：**就地发现、不复制进 SKILL Home**。扫描发现根
``$A2C_SKILL_HOME/user/``（全局个人）+ **全部已登记工作目录** ``<workdir>/.tfrobot/skills/``（能力发现层、
跨目录全局并集、不随 active workdir 切换）；发现单元 ``<root>/<skill>/SKILL.md``（根下**一级**）。
- **name = 目录 basename**（单段裸名，§5.0）——就地目录不可改名，与 sandbox 的 name 寻址（S2）一致；
  ``frontmatter.name`` 仅参考（不一致记 DEBUG）；basename 非严格 kebab → 跳过。
- **优先级（低→高）**：``user/`` < 各 workdir（按登记序，**后者覆盖前者** + WARN）。
- 深于一级的 ``SKILL.md``（``<root>/a/b/SKILL.md``）→ 忽略 + DEBUG（user 源单段命名，不嵌套）。
- 重扫幂等：已注册 → ``update``（含孤儿恢复），否则 ``register``；磁盘删除项的孤儿标记交 reconciler（#62）/
  watcher（#67）按返回的发现 name 列表 diff，本函数不负责。

失败降级 / Failure isolation：任一 SKILL 物化/解析失败 → 记 ERROR、清理半成品、跳过该 SKILL，
**不**阻断其余、**不**抛给上层（skill.md §1.5：batch 接口对部分失败健壮）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import zipfile
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from mcp.types import BlobResourceContents, ReadResourceResult, Resource, TextResourceContents

from a2c_smcp.computer.settings.schema import is_valid_marketplace_name
from a2c_smcp.computer.skills.home import (
    SOURCE_MARKETPLACE,
    SOURCE_USER,
    marketplace_skill_dir,
    mcp_skill_dir,
    user_dropin_root,
    workdir_skill_root,
)
from a2c_smcp.computer.skills.naming import (
    SkillNameError,
    normalize_mcp_server_segment,
    synthesize_marketplace_name,
    synthesize_mcp_name,
    synthesize_user_name,
)
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.sources import (
    DEFAULT_PLUGIN_ROOT,
    GitCloneSpec,
    LocalPluginSource,
    SkillSourceError,
    marketplace_clone_url,
    resolve_plugin_source,
)
from a2c_smcp.smcp import A2CSkillRef
from a2c_smcp.utils.logger import get_logger
from a2c_smcp.utils.path import is_within

if TYPE_CHECKING:
    # settings.store 经 skills.home 反向依赖本包，运行时改用 _record_known_marketplace 内的惰性 import 破环；
    # 此处仅供类型标注（本模块 from __future__ import annotations，注解全惰性求值，不在运行时触发导入）。
    from a2c_smcp.computer.settings.store import KnownMarketplacesFile, MarketplaceRecord

logger = get_logger(__name__)

SKILL_MD = "SKILL.md"
SKILL_URI_PREFIX = "skill://"
_MCP_SOURCE_MODES = frozenset({"mounted", "archive", "resources"})

# marketplace 布局常量 / marketplace layout constants（协议 marketplace-v1 §2.1 / §3.1 / §6）。
MARKETPLACE_MANIFEST_DIR = ".tfrobot-plugin"  # marketplace.json / plugin.json 所在目录（嵌套，镜像 CC .claude-plugin）
MARKETPLACE_MANIFEST = "marketplace.json"  # 仓库级 manifest
PLUGIN_MANIFEST = "plugin.json"  # plugin 级 manifest
SKILLS_SUBDIR = "skills"  # plugin 内 SKILL 子树约定目录（SKILL 协议 §2）
# 独立 clone 的 plugin（url/github/cnb/git-subdir）落点命名空间——置于 marketplace/ 下以 "." 起首的目录，
# 与 catalog clone（<home>/marketplace/<mp>/）物理隔离、且不与 kebab marketplace 名冲突。
_EXTERNAL_PLUGINS_NS = ".plugins"

# git clone/pull 默认超时（秒）/ Default git clone/pull timeout (design §2.2: 默认 120s)。
DEFAULT_GIT_TIMEOUT = 120.0

# scp-like / ssh:// → https 回退的解析（SSH→HTTPS fallback，design §2.2）/ ssh→https rewrite patterns。
_SSH_SCHEME_RE = re.compile(r"^ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$")
_SSH_SCP_LIKE_RE = re.compile(r"^[\w.+-]+@([\w.-]+):(.+)$")

# 归档安全上界（防 tar/zip bomb OOM；可信源模型下为安全网，默认宽松，后续可配置化）。
# Archive safety bounds (bomb guard): generous defaults under the trusted-source model.
MAX_ARCHIVE_DOWNLOAD_BYTES = 64 * 1024 * 1024  # 压缩态下载上限 / compressed download cap (64 MiB)
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024  # 解压累计字节上限 / cumulative uncompressed cap (256 MiB)
MAX_ARCHIVE_MEMBERS = 10_000  # 成员数上限（防海量小文件 bomb）/ member-count cap

# 物化所需的最小 manager 协议（便于测试注入 fake）/ Minimal manager protocol for materialization (mockable).
ArchiveFetcher = Callable[[str], Awaitable[bytes]]


class SkillStagingError(Exception):
    """staging 物化失败（穿越/校验/格式等）/ Materialization failure (traversal / checksum / format)。"""


# ── frontmatter ───────────────────────────────────────────────────────────
def parse_skill_frontmatter(skill_md_text: str) -> dict[str, Any]:
    """
    解析 SKILL.md 头部 YAML frontmatter（``---`` 包裹）/ Parse the leading YAML frontmatter of SKILL.md。

    无 frontmatter 或解析失败 → 返回 ``{}``（调用方据缺字段判废）。
    """
    if not skill_md_text.startswith("---"):
        return {}
    # 以行级定位首尾 --- 之间的 YAML 块
    lines = skill_md_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            block = "\n".join(lines[1:i])
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError as e:
                logger.error("SKILL.md frontmatter YAML parse failed: %s", e)
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def strip_skill_frontmatter(skill_md_text: str) -> str:
    """
    剥离 SKILL.md 头部 YAML frontmatter，返回正文 body（Agent 最终消费内容）/ Strip leading YAML
    frontmatter from SKILL.md, returning the body the Agent ultimately consumes。

    与 :func:`parse_skill_frontmatter` 同源行级定位（首尾 ``---``）：无 frontmatter / 无闭合 ``---``
    → 原样返回。剥离后正文 = 闭合 ``---`` 行之后的全部内容（保留原行尾，不额外 trim），故
    ``client:get_skill`` 铸造期与 ``client:get_blob`` 解析期对 SKILL.md 的「消费字节」基准一致
    （协议 ``blob-transfer.md`` §3 / 设计 §4.4「资源字节三处一致」）。
    Same line-level fence detection as :func:`parse_skill_frontmatter`; no frontmatter / no closing
    fence → returned unchanged. Body = everything after the closing ``---`` line (original line
    endings preserved, no extra trimming), so the get_skill mint and the get_blob resolve agree on
    the consumed bytes for SKILL.md.
    """
    if not skill_md_text.startswith("---"):
        return skill_md_text
    lines = skill_md_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return skill_md_text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1 :])
    # 无闭合 fence → 视为无有效 frontmatter，原样返回（与 parse 一致）
    return skill_md_text


# ── 安全解包 / safe extraction ──────────────────────────────────────────────
def _resolved_member_target(dest: Path, member_name: str) -> Path:
    """
    归一化并校验归档成员落点仍在 ``dest`` 内（防 ``..`` / 绝对路径穿越）/ Anti-traversal member target。

    函数内对 ``dest`` 也 ``resolve()``，使不变量自洽——不依赖调用方传入已规范化路径（含 symlink 的 home 不误拒）。
    Resolves ``dest`` too so the invariant is self-contained (no reliance on a pre-canonicalized dest).
    """
    dest = dest.resolve()
    target = (dest / member_name).resolve()
    if target != dest and dest not in target.parents:
        raise SkillStagingError(f"archive member escapes staging dir: {member_name!r}")
    return target


def _copy_capped(src: Any, out: Any, already: int) -> int:
    """分块复制并累计校验解压上限（防 bomb）/ Chunked copy enforcing the cumulative extraction cap。"""
    total = already
    while True:
        chunk = src.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_EXTRACTED_BYTES:
            raise SkillStagingError(f"archive exceeds extracted size limit ({MAX_EXTRACTED_BYTES} bytes)")
        out.write(chunk)
    return total


def _extract_tar_gz(data: bytes, dest: Path) -> None:
    """安全解 ``tar.gz``：拒符号/硬链接、成员数上限、解压累计上限、逐成员校验落点 / Safe tar.gz extraction。"""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise SkillStagingError(f"archive has too many members ({len(members)} > {MAX_ARCHIVE_MEMBERS})")
        extracted_bytes = 0
        for member in members:
            if member.issym() or member.islnk():
                raise SkillStagingError(f"archive contains link member (rejected): {member.name!r}")
            target = _resolved_member_target(dest, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                with fobj as src, target.open("wb") as out:
                    extracted_bytes = _copy_capped(src, out, extracted_bytes)
            # 其它类型（设备/FIFO 等）一律忽略 / ignore device/fifo etc.


def _extract_zip(data: bytes, dest: Path) -> None:
    """
    安全解 ``zip``：成员数上限、解压累计上限、逐成员校验落点 / Safe zip extraction。

    注：Python ``zipfile`` **不**还原 zip 内的符号链接（写为普通文件），故无需 tar 那样的链接拒绝分支；
    落点校验已防穿越。Python zipfile does not restore in-zip symlinks, so no link-rejection branch is needed.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if len(names) > MAX_ARCHIVE_MEMBERS:
            raise SkillStagingError(f"archive has too many members ({len(names)} > {MAX_ARCHIVE_MEMBERS})")
        extracted_bytes = 0
        for name in names:
            target = _resolved_member_target(dest, name)
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as out:
                    extracted_bytes = _copy_capped(src, out, extracted_bytes)


def _reset_dir(dest: Path) -> None:
    """清空并新建目标目录（重物化幂等）/ Clear and recreate the dest dir (idempotent re-materialization)。"""
    if dest.exists() or dest.is_symlink():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)


# ── 三种 source 模式物化 / three materialization modes ──────────────────────
async def _default_archive_fetch(url: str) -> bytes:
    """默认归档拉取（aiohttp GET，流式累计上限防 OOM）/ Default archive fetch via aiohttp (size-capped stream)。"""
    import aiohttp

    chunks: list[bytes] = []
    total = 0
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(1 << 16):
            total += len(chunk)
            if total > MAX_ARCHIVE_DOWNLOAD_BYTES:
                raise SkillStagingError(f"archive exceeds download size limit ({MAX_ARCHIVE_DOWNLOAD_BYTES} bytes)")
            chunks.append(chunk)
    return b"".join(chunks)


def _materialize_mounted(meta: dict[str, Any], dest: Path) -> None:
    """mounted：复制 ``_meta.mount_dir`` 本地目录树进 staging（自包含，不留符号链接）。"""
    mount_dir = meta.get("mount_dir")
    if not mount_dir or not isinstance(mount_dir, str):
        raise SkillStagingError("mounted source missing 'mount_dir'")
    src = Path(mount_dir)
    if not src.is_dir():
        raise SkillStagingError(f"mounted source dir not found: {mount_dir!r}")
    _reset_dir(dest)
    # symlinks=False：复制内容而非符号链接，确保 staging 自包含、沙箱 realpath 不外逃
    shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=False)


async def _materialize_archive(meta: dict[str, Any], dest: Path, fetch: ArchiveFetcher) -> None:
    """archive：拉取 → 校验 sha256（若声明）→ 按格式安全解包。"""
    uri = meta.get("archive_uri")
    fmt = meta.get("archive_format")
    if not uri or not isinstance(uri, str):
        raise SkillStagingError("archive source missing 'archive_uri'")
    if fmt not in ("tar.gz", "zip"):
        raise SkillStagingError(f"unsupported archive_format: {fmt!r} (expect 'tar.gz' | 'zip')")
    data = await fetch(uri)
    if len(data) > MAX_ARCHIVE_DOWNLOAD_BYTES:
        raise SkillStagingError(f"archive exceeds download size limit ({len(data)} > {MAX_ARCHIVE_DOWNLOAD_BYTES} bytes)")
    expected_sha = meta.get("archive_sha256")
    if expected_sha:
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != str(expected_sha).lower():
            raise SkillStagingError(f"archive sha256 mismatch: expected {expected_sha}, got {actual}")
    _reset_dir(dest)
    if fmt == "tar.gz":
        _extract_tar_gz(data, dest)
    else:
        _extract_zip(data, dest)


def _content_bytes(content: TextResourceContents | BlobResourceContents) -> bytes:
    """把单个资源内容块还原为字节 / Decode a resource content item to bytes。"""
    if isinstance(content, TextResourceContents):
        return content.text.encode("utf-8")
    # BlobResourceContents.blob 为 base64 字符串
    return base64.b64decode(content.blob)


async def _materialize_resources(
    read_resource: Callable[[str], Awaitable[ReadResourceResult]],
    root_uri: str,
    sub_resources: Sequence[Resource],
    dest: Path,
) -> None:
    """resources：逐个 ``resources/read`` 子资源，按相对路径安全写入 staging。"""
    _reset_dir(dest)
    root_path = _uri_path(root_uri)
    wrote_any = False
    for res in sub_resources:
        sub_uri = str(res.uri)
        rel = _uri_path(sub_uri)[len(root_path) + 1 :]  # 去掉 "<root_path>/" 前缀
        if not rel:
            continue
        target = _resolved_member_target(dest, rel)
        result = await read_resource(sub_uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            for content in result.contents:
                out.write(_content_bytes(content))
        wrote_any = True
    if not wrote_any:
        raise SkillStagingError(f"resources-mode SKILL has no sub-resources under {root_uri!r}")


# ── URI 辅助 / uri helpers ──────────────────────────────────────────────────
def _uri_path(uri: str) -> str:
    """``skill://host/a/b`` → ``a/b``（host 之后的路径，无前导 ``/``）。"""
    rest = uri[len(SKILL_URI_PREFIX) :] if uri.startswith(SKILL_URI_PREFIX) else uri
    _, _, path = rest.partition("/")
    return path


def _uri_leaf(uri: str) -> str:
    """SKILL 根 URI 的首路径段（provisional staging 目录名）/ first path segment of a SKILL root URI。"""
    path = _uri_path(uri)
    return path.split("/", 1)[0] if path else ""


# ── A2CSkillRef 合成 / ref construction ─────────────────────────────────────
def _apply_frontmatter_optional_fields(ref: A2CSkillRef, frontmatter: dict[str, Any]) -> None:
    """
    把 frontmatter 派生的**可选**字段写入 ref（两源共用）/ Apply frontmatter-derived optional fields。

    ``license`` / ``compatibility`` / ``allowed-tools`` / ``metadata`` 的语义不分源；``name`` / ``source`` /
    ``path`` / ``description`` / ``version`` 由各源各自填（语义不同，见 :func:`_build_ref` / :func:`_build_user_ref`）。
    """
    if frontmatter.get("license") is not None:
        ref["license"] = str(frontmatter["license"])
    if frontmatter.get("compatibility") is not None:
        ref["compatibility"] = str(frontmatter["compatibility"])
    allowed = frontmatter.get("allowed-tools", frontmatter.get("allowed_tools"))
    if allowed is not None:
        ref["allowed_tools"] = [str(t) for t in allowed] if isinstance(allowed, (list, tuple)) else [str(allowed)]
    if isinstance(frontmatter.get("metadata"), dict):
        ref["skill_metadata"] = frontmatter["metadata"]


def _build_ref(
    name: str,
    normalized_server: str,
    frontmatter: dict[str, Any],
    meta: dict[str, Any],
    path: Path,
    uri: str,
) -> A2CSkillRef:
    """从已合成 ``name`` + frontmatter + ``_meta`` 组装 A2CSkillRef / Assemble A2CSkillRef from precomputed name + frontmatter。"""
    ref: A2CSkillRef = {
        "name": name,
        "source": f"mcp:{normalized_server}",
        "uri": uri,
        "path": str(path),
        "description": str(frontmatter["description"]),
    }
    _apply_frontmatter_optional_fields(ref, frontmatter)
    version = meta.get("version")
    if version is not None:
        ref["version"] = str(version)
    return ref


# ── 编排 / orchestration ────────────────────────────────────────────────────
async def stage_mcp_skills(
    manager: Any,
    registry: SkillRegistry,
    home: Path,
    *,
    server_name: str | None = None,
    archive_fetch: ArchiveFetcher | None = None,
) -> list[str]:
    """
    枚举并物化 mcp 源 SKILL，注册进 Registry / Enumerate, materialize and register mcp-source SKILLs。

    :param manager: 提供 ``list_skill_resources(server_name)`` 与 ``read_resource(server, uri)`` 的 MCP 管理器。
    :param registry: 目标 :class:`SkillRegistry`。
    :param home: SKILL Home 绝对根（见 :mod:`~a2c_smcp.computer.skills.home`）。
    :param server_name: 若提供仅物化该 server（ResourceListChanged 单 server 重物化）；否则全部活跃 server。
    :param archive_fetch: 归档拉取替身（默认 aiohttp）；便于测试注入。
    :return: 成功注册（或刷新）的 SKILL name 列表 / names successfully registered (or refreshed).
    """
    fetch = archive_fetch or _default_archive_fetch
    pairs: list[tuple[str, Resource]] = await manager.list_skill_resources(server_name)

    by_server: dict[str, list[Resource]] = defaultdict(list)
    for sname, res in pairs:
        by_server[sname].append(res)

    registered: list[str] = []
    seen_this_run: set[str] = set()  # 本 run 已处理的合成 name，用于真冲突检测（§1.5 保留先到者）
    for sname, resources in by_server.items():
        normalized_server = normalize_mcp_server_segment(sname)
        for res in resources:
            meta = dict(getattr(res, "meta", None) or {})
            mode = meta.get("source")
            if mode not in _MCP_SOURCE_MODES:
                continue  # 非 SKILL 根（子资源 / 未声明 source）→ 跳过

            root_uri = str(res.uri)
            leaf = _uri_leaf(root_uri)
            if not leaf:
                logger.error("skill root URI has no path segment, skipped: %s", root_uri)
                continue

            staged = mcp_skill_dir(home, normalized_server, leaf)
            try:
                if mode == "mounted":
                    _materialize_mounted(meta, staged)
                elif mode == "archive":
                    await _materialize_archive(meta, staged, fetch)
                else:  # resources
                    subs = [r for r in resources if str(r.uri).startswith(root_uri + "/")]
                    await _materialize_resources(partial(manager.read_resource, sname), root_uri, subs, staged)
            except Exception as e:
                logger.error("materialize failed for %s (mode=%s): %s", root_uri, mode, e, exc_info=True)
                shutil.rmtree(staged, ignore_errors=True)
                continue

            name = _finalize_and_register(sname, normalized_server, meta, staged, root_uri, home, registry, seen_this_run)
            if name is not None:
                registered.append(name)
    return registered


def _finalize_and_register(
    server_name: str,
    normalized_server: str,
    meta: dict[str, Any],
    staged: Path,
    root_uri: str,
    home: Path,
    registry: SkillRegistry,
    seen_this_run: set[str],
) -> str | None:
    """读 frontmatter → 真冲突拒绝（落盘前）→ 校正包根目录名 → register/update。失败返回 None。"""
    skill_md = staged / SKILL_MD
    if not skill_md.is_file():
        logger.error("staged SKILL missing %s, skipped: %s", SKILL_MD, root_uri)
        shutil.rmtree(staged, ignore_errors=True)
        return None

    frontmatter = parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
    if not frontmatter.get("name") or not frontmatter.get("description"):
        logger.error("SKILL.md frontmatter missing required 'name'/'description', skipped: %s", root_uri)
        shutil.rmtree(staged, ignore_errors=True)
        return None

    try:
        name = synthesize_mcp_name(server_name, str(frontmatter["name"]))
    except SkillNameError as e:
        logger.error("skill name synthesis failed, skipped: %s (%s)", root_uri, e.reason)
        shutil.rmtree(staged, ignore_errors=True)
        return None

    # 真冲突（本 run 内两个不同 SKILL 合成同 name）必须在 rename 落盘前拒绝——否则会覆盖先到者的磁盘文件。
    # §1.5「拒绝第二注册者、保留先到者」；区别于 register/update 分流：跨 run 既存同一 SKILL 才走 update（刷新/孤儿恢复）。
    if name in seen_this_run:
        logger.error("duplicate synthesized SKILL name within staging run (collision), keeping first: %s (%s)", name, root_uri)
        shutil.rmtree(staged, ignore_errors=True)
        return None
    seen_this_run.add(name)

    # 包根目录名校正为 frontmatter.name（skill.md §4）
    final = mcp_skill_dir(home, normalized_server, str(frontmatter["name"]))
    if final != staged:
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        staged.rename(final)

    ref = _build_ref(name, normalized_server, frontmatter, meta, final, root_uri)
    # 本 run 已 seen 去重；此处 name in registry 必为跨 run 既存的同一 SKILL → update（刷新/孤儿恢复）
    ok = registry.register_or_update(ref)
    return name if ok else None


# ── user 源 DropIn（就地发现，不 staging）/ user-source in-place DropIn ────────
def _user_dropin_roots(home: Path, workdirs: Sequence[Path]) -> list[Path]:
    """
    user 源 DropIn 发现根，按优先级**升序**（低→高，后者覆盖）+ 解析去重 / Ascending-priority deduped roots。

    顺序 = ``[<home>/user]`` + ``[<workdir>/.tfrobot/skills ...]``（登记序）。``resolve()`` 后按路径去重保序，
    避免同一目录被登记两次造成重复扫描 + 假 WARN（首次出现定其优先级槽位）。
    """
    ordered: list[Path] = [user_dropin_root(home).resolve()]
    ordered.extend(workdir_skill_root(wd).resolve() for wd in workdirs)
    seen: set[Path] = set()
    deduped: list[Path] = []
    for root in ordered:
        if root not in seen:
            seen.add(root)
            deduped.append(root)
    return deduped


def _iter_user_skill_dirs(root: Path) -> Iterator[Path]:
    """
    枚举发现根下的 SKILL 目录 / Yield ``<root>/<skill>/`` whose ``<skill>/SKILL.md`` exists（根下**一级**）。

    只扫根下一级（``iterdir``，O(顶层条目)）：子目录含直接 ``SKILL.md`` → 即一个 SKILL，**不再深入其包内**
    （包内 scripts/、assets/ 等附属文件不递归遍历——避免大包性能开销与 symlink 打转）。无直接 ``SKILL.md``
    的「wrapper 目录」（异常摆放）→ 仅对其补扫深层 ``SKILL.md`` 记 DEBUG 忽略（深于一级，user 源单段命名、
    不嵌套，对齐 watcher §8.3）。``sorted`` 保证同根内确定序（WARN 可复现）。
    """
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            if child.name == SKILL_MD:  # 根下直接的 <root>/SKILL.md（无 <skill>/ 包装）→ 非发现单元
                logger.debug("user DropIn ignoring SKILL.md directly under root (not in a skill dir): %s", child)
            continue
        if (child / SKILL_MD).is_file():
            yield child  # 一级 SKILL 包，不深入其内部
        else:
            # wrapper 目录无直接 SKILL.md：补扫深层 SKILL.md 仅为记 DEBUG（不进入有效包，故仅扫此异常分支）
            for deeper in sorted(child.rglob(SKILL_MD)):
                logger.debug("user DropIn ignoring SKILL.md not at one-level depth under %s: %s", root, deeper)


def _build_user_ref(name: str, skill_dir: Path) -> A2CSkillRef | None:
    """
    读就地 ``SKILL.md`` frontmatter 组装 user 源 A2CSkillRef / Build a user-source ref from in-place SKILL.md。

    ``source="user"``、**无 ``uri``**（协议表面 user 源不带 ``skill://``）；``path`` = 就地包根（**不复制**）。
    缺 ``description`` / SKILL.md 不可读 → 记 ERROR + ``None``（跳过该 SKILL）。``frontmatter.name`` 与
    目录 basename 不一致 → DEBUG（basename 权威，就地不可改名）。

    **无 ``version``**：SKILL.md frontmatter 恰好 6 字段（name/description/license/compatibility/metadata/
    allowed-tools），不含 version（协议 skill.md §6 / 设计 §1.2）；``version`` 仅来自 manifest——mcp 取自
    ``_meta``、marketplace 取自 plugin.json/entry，而 user 源无任何 manifest，故 user SKILL 不带 version。
    """
    skill_md = skill_dir / SKILL_MD
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as e:
        logger.error("user SKILL %r SKILL.md unreadable, skipped: %s (%s)", name, skill_md, e)
        return None
    frontmatter = parse_skill_frontmatter(text)
    if not frontmatter.get("description"):
        logger.error("user SKILL %r frontmatter missing required 'description', skipped: %s", name, skill_md)
        return None
    fm_name = frontmatter.get("name")
    if fm_name and str(fm_name) != name:
        logger.debug("user SKILL dir basename %r != frontmatter.name %r; basename is authoritative (in-place addressing)", name, fm_name)
    ref: A2CSkillRef = {
        "name": name,
        "source": SOURCE_USER,
        "path": str(skill_dir.resolve()),
        "description": str(frontmatter["description"]),
    }
    _apply_frontmatter_optional_fields(ref, frontmatter)
    return ref


def stage_user_skills(
    registry: SkillRegistry,
    home: Path,
    workdirs: Sequence[Path] = (),
) -> list[str]:
    """
    枚举 user 源 DropIn 并注册进 Registry（**就地发现、不复制**）/ Discover user-source DropIn skills in place。

    扫描 ``<home>/user/`` + 各 ``<workdir>/.tfrobot/skills/``（能力发现层全局并集，**不随 active workdir 切换**）；
    发现单元 ``<root>/<skill>/SKILL.md``（根下一级），name = 目录 basename（单段裸名）。同名按发现根优先级
    **后者覆盖前者**（``user/`` 最低 < 各 workdir 登记序），覆盖时记 WARN（便于诊断「为何我的 skill 不显示」）。

    :param registry: 目标 :class:`SkillRegistry`。
    :param home: SKILL Home 绝对根（见 :mod:`~a2c_smcp.computer.skills.home`）。
    :param workdirs: workspace **已登记工作目录**（按登记序；调用方提供，本函数不耦合 workspace 登记模块）。
    :return: 本次发现并成功注册/刷新的 SKILL name 列表 / discovered & registered names（供 reconciler/watcher diff 孤儿）。
    """
    winners: dict[str, tuple[A2CSkillRef, Path]] = {}  # name → (ref, 发现目录)；后者覆盖前者
    for root in _user_dropin_roots(home, workdirs):
        for skill_dir in _iter_user_skill_dirs(root):
            basename = skill_dir.name
            try:
                name = synthesize_user_name(basename)  # 校验严格 kebab（§1.5 失败不入册）
            except SkillNameError as e:
                logger.error("user DropIn skill dir name invalid, skipped: %s (%s)", skill_dir, e.reason)
                continue
            ref = _build_user_ref(name, skill_dir)
            if ref is None:
                continue
            prev = winners.get(name)
            if prev is not None:
                logger.warning(
                    "user SKILL %r at %s shadows earlier DropIn at %s (later root wins)",
                    name,
                    skill_dir,
                    prev[1],
                )
            winners[name] = (ref, skill_dir)

    registered: list[str] = []
    for name, (ref, _) in winners.items():
        # 跨 run 既存（active 或 orphan）→ update（刷新 / 孤儿恢复）；否则 register。
        if registry.register_or_update(ref):
            registered.append(name)
    return registered


# ── marketplace 源 git staging（#61）/ marketplace-source git staging ──────────
def _git_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """
    构造非交互 git 环境 / Build a non-interactive git environment（design §2.2）。

    强制 ``GIT_TERMINAL_PROMPT=0`` + ``GIT_ASKPASS=""`` 禁用凭证交互提示；``GIT_SSH_COMMAND`` 注入
    ``-oBatchMode=yes`` 让 SSH 在缺凭证时**立即失败**（而非挂起等待密码）——失败后由 :func:`_git_clone_with_fallback`
    走 SSH→HTTPS 回退。基于传入 ``env``（默认 ``os.environ``）派生，不污染调用方环境。
    """
    base = dict(os.environ if env is None else env)
    base["GIT_TERMINAL_PROMPT"] = "0"
    base["GIT_ASKPASS"] = ""
    base.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return base


async def _run_git(args: Sequence[str], *, timeout: float, env: Mapping[str, str] | None) -> str:
    """
    执行 ``git <args>`` 并返回 stdout / Run ``git`` capturing stdout（非零退出 / 超时 → :class:`SkillStagingError`）。

    用 ``create_subprocess_exec`` 显式 argv（**不**经 shell，杜绝 url 注入）；超时 ``kill`` 并回收，
    避免遗留僵尸进程。仿 :func:`a2c_smcp.computer.inputs.cli_io.arun_command` 的超时姿态。
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_git_env(env),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - 进程已退出
            pass
        await proc.wait()
        raise SkillStagingError(f"git {args[0] if args else ''} timed out after {timeout}s") from None
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore").strip()
        raise SkillStagingError(f"git {' '.join(args)} failed (rc={proc.returncode}): {err}")
    return stdout.decode(errors="ignore")


def _ssh_to_https(url: str) -> str | None:
    """
    SSH/scp-like URL → ``https://`` 回退候选 / Rewrite an ssh URL to its ``https`` fallback。

    ``ssh://[user@]host[:port]/path`` 与 scp-like ``user@host:path`` → ``https://host/path``；非 ssh 形态
    （已是 ``https`` / ``file://`` 等）→ ``None``（无回退）。design §2.2 SSH→HTTPS 回退。
    """
    u = url.strip()
    m = _SSH_SCHEME_RE.match(u)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    m = _SSH_SCP_LIKE_RE.match(u)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return None


async def _git_clone_with_fallback(
    clone_args: Sequence[str],
    url: str,
    dest: Path,
    *,
    timeout: float,
    env: Mapping[str, str] | None,
) -> None:
    """
    ``git clone`` 到 ``dest``（clone 与 url 之间插 ``clone_args`` flag），失败 SSH→HTTPS 回退 / Clone with fallback。

    ``dest`` 落盘前先 ``rmtree``（幂等、清半成品）；首次（原 url）失败且为 ssh 形态 → 改写 https 重试一次。
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _run_git(["clone", *clone_args, "--", url, str(dest)], timeout=timeout, env=env)
        return
    except SkillStagingError as first:
        https = _ssh_to_https(url)
        if https is None or https == url:
            raise
        logger.warning("git clone %s failed (%s); retrying via HTTPS %s", url, first, https)
        shutil.rmtree(dest, ignore_errors=True)
        await _run_git(["clone", *clone_args, "--", https, str(dest)], timeout=timeout, env=env)


async def _git_clone_marketplace(url: str, dest: Path, *, timeout: float, env: Mapping[str, str] | None) -> None:
    """marketplace catalog ``git clone --depth 1``（无 ref/sha，GitSource 仅 url）/ Shallow-clone the catalog。"""
    await _git_clone_with_fallback(["--depth", "1"], url, dest, timeout=timeout, env=env)


async def _git_refresh_marketplace(dest: Path, url: str, *, timeout: float, env: Mapping[str, str] | None) -> None:
    """原地 ``git pull --ff-only``；失败 → 全量重 clone / In-place pull, full re-clone on failure（design §2.2）。"""
    try:
        await _run_git(["-C", str(dest), "pull", "--ff-only"], timeout=timeout, env=env)
        return
    except SkillStagingError as e:
        logger.warning("git pull in %s failed (%s); full re-clone", dest, e)
    await _git_clone_marketplace(url, dest, timeout=timeout, env=env)


async def _git_clone_plugin(spec: GitCloneSpec, dest: Path, *, timeout: float, env: Mapping[str, str] | None) -> Path:
    """
    克隆独立 plugin 源（``url``/``github``/``cnb``/``git-subdir``）→ 返回 plugin 根 / Clone a standalone plugin source。

    - ``sha`` 锁版本：``--filter=blob:none --no-checkout`` 全 refs blobless clone 后 ``checkout <sha>``（浅克隆
      无法精确命中任意 sha）；
    - ``git-subdir``（无 sha）：``--filter=tree:0 --sparse --depth 1``（按 design §2.2 / CC pluginLoader 链路）
      后 ``sparse-checkout set <subdir>``，plugin 根 = ``<clone>/<subdir>``；
    - 普通（无 sha 无 subdir）：``--depth 1 [--branch <ref>]``。
    SSH→HTTPS 回退由 :func:`_git_clone_with_fallback` 承担。

    .. note::
       ``sha``（``--filter=blob:none``）与 ``git-subdir``（``--filter=tree:0``）路径依赖远端支持 partial clone
       （``uploadpack.allowFilter``）。不支持的自建 / 部分 CNB 服务器会 clone 失败——由上层
       :func:`stage_marketplace_skills` 走失败降级（记 ERROR、该 plugin 不入册、不阻断其余）；**暂无**「退回
       全量 clone」兜底（可按需后续增强）。
    """
    if spec.sha:
        clone_args: list[str] = ["--filter=blob:none", "--no-checkout"]
    elif spec.subdir:
        clone_args = ["--filter=tree:0", "--sparse", "--depth", "1"]
        if spec.ref:
            clone_args += ["--branch", spec.ref]
    else:
        clone_args = ["--depth", "1"]
        if spec.ref:
            clone_args += ["--branch", spec.ref]

    await _git_clone_with_fallback(clone_args, spec.url, dest, timeout=timeout, env=env)

    if spec.sha:
        # blobless/no-checkout：按 sha 落实工作树（subdir 仅作 plugin 根路径，无需 sparse）。
        await _run_git(["-C", str(dest), "checkout", spec.sha], timeout=timeout, env=env)
    elif spec.subdir:
        await _run_git(["-C", str(dest), "sparse-checkout", "set", spec.subdir], timeout=timeout, env=env)

    return (dest / spec.subdir) if spec.subdir else dest


async def _git_head_sha(dest: Path, *, timeout: float, env: Mapping[str, str] | None) -> str | None:
    """``git rev-parse HEAD``（取 commitSha 物化记录用）；失败 → ``None`` / HEAD sha, ``None`` on failure。"""
    try:
        return (await _run_git(["-C", str(dest), "rev-parse", "HEAD"], timeout=timeout, env=env)).strip() or None
    except SkillStagingError as e:
        logger.warning("git rev-parse HEAD in %s failed (%s)", dest, e)
        return None


# ── marketplace.json / plugin.json 解析 / manifest parsing ───────────────────
def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    """读 JSON 文件并要求根为对象 / Read a JSON file requiring an object root（失败 → :class:`SkillStagingError`）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SkillStagingError(f"{what} unreadable/invalid at {path}: {e}") from e
    if not isinstance(data, dict):
        raise SkillStagingError(f"{what} root is not an object: {path}")
    return data


def _read_marketplace_manifest(clone_dir: Path) -> dict[str, Any]:
    """读 ``<clone>/.tfrobot-plugin/marketplace.json``（仓库级 manifest）/ Read the marketplace manifest。"""
    path = clone_dir / MARKETPLACE_MANIFEST_DIR / MARKETPLACE_MANIFEST
    if not path.is_file():
        raise SkillStagingError(f"marketplace manifest not found: {path}")
    return _read_json_object(path, what="marketplace manifest")


def _read_plugin_manifest(plugin_root: Path) -> dict[str, Any]:
    """读 ``<plugin>/.tfrobot-plugin/plugin.json``（best-effort，缺失 → ``{}``）/ Read plugin.json best-effort。"""
    path = plugin_root / MARKETPLACE_MANIFEST_DIR / PLUGIN_MANIFEST
    if not path.is_file():
        return {}
    try:
        return _read_json_object(path, what="plugin manifest")
    except SkillStagingError as e:
        # plugin.json 仅供 version / 显示名兜底，损坏不致命（SKILL 由路径推导）。
        logger.warning("plugin manifest ignored (%s)", e)
        return {}


def _plugin_root_base(manifest: Mapping[str, Any]) -> str:
    """取 ``metadata.pluginRoot``（缺省 :data:`~a2c_smcp.computer.skills.sources.DEFAULT_PLUGIN_ROOT`）/ Resolve pluginRoot。"""
    md = manifest.get("metadata")
    if isinstance(md, Mapping):
        pr = md.get("pluginRoot")
        if isinstance(pr, str) and pr.strip():
            return pr.strip()
    return DEFAULT_PLUGIN_ROOT


def _entry_plugin_name(entry: Mapping[str, Any]) -> str:
    """plugin 条目 ID = entry.name（marketplace-v1 §4.1 必填；kebab 校验交 name 合成）/ The plugin entry name。"""
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillStagingError(f"plugin entry missing required 'name': {entry!r}")
    return name.strip()


def _external_plugin_dir(home: Path, marketplace: str, plugin: str) -> Path:
    """独立 clone plugin 的落点 ``<home>/marketplace/.plugins/<mp>/<plugin>/`` / External plugin clone dir。"""
    return home / SOURCE_MARKETPLACE / _EXTERNAL_PLUGINS_NS / marketplace / plugin


def _build_marketplace_ref(name: str, marketplace: str, frontmatter: dict[str, Any], version: str | None, skill_dir: Path) -> A2CSkillRef:
    """
    组装 marketplace 源 A2CSkillRef / Assemble a marketplace-source A2CSkillRef。

    ``source = "marketplace:<repo>"``（完整溯源，**不**进 name）；**无 ``uri``**（marketplace 源不带
    ``skill://``）；``path`` = SKILL 包根（就地 clone 树内，不复制）；``version`` 取自 entry/plugin.json/commitSha。
    """
    ref: A2CSkillRef = {
        "name": name,
        "source": f"{SOURCE_MARKETPLACE}:{marketplace}",
        "path": str(skill_dir.resolve()),
        "description": str(frontmatter["description"]),
    }
    _apply_frontmatter_optional_fields(ref, frontmatter)
    if version is not None:
        ref["version"] = str(version)
    return ref


def _scan_and_register_plugin_skills(
    marketplace: str,
    plugin_name: str,
    plugin_root: Path,
    version: str | None,
    registry: SkillRegistry,
    seen: set[str],
) -> list[str]:
    """
    扫 ``<plugin 根>/skills/<skill>/SKILL.md`` 并注册 / Scan a plugin's ``skills/`` and register each SKILL。

    ``name = <plugin>:<skill>``（``<plugin>`` = entry.name、``<skill>`` = skill 目录 basename，frontmatter
    仅作显示名、不改 ID，marketplace-v1 §2.1 防伪）。缺 frontmatter ``description`` / name 合成失败 / 本 run
    重名 → 记 ERROR、跳过、不入册（失败降级，不抛）。仅扫 ``skills/`` 下**一级**（``iterdir``），不递归包内。

    .. note::
       仅扫**约定** ``skills/`` 目录；``entry.skills`` 组件路径覆写与 strict mode 冲突检测显式延后（见 #80）。
    """
    skills_dir = plugin_root / SKILLS_SUBDIR
    if not skills_dir.is_dir():
        logger.warning(
            "marketplace %r plugin %r has no %s/ dir at %s; no SKILLs registered",
            marketplace,
            plugin_name,
            SKILLS_SUBDIR,
            skills_dir,
        )
        return []

    registered: list[str] = []
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / SKILL_MD
        if not skill_md.is_file():
            continue
        try:
            frontmatter = parse_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError as e:
            logger.error("marketplace %r plugin %r skill %r SKILL.md unreadable, skipped: %s", marketplace, plugin_name, skill_dir.name, e)
            continue
        if not frontmatter.get("description"):
            logger.error(
                "marketplace %r plugin %r skill %r missing frontmatter 'description', skipped",
                marketplace,
                plugin_name,
                skill_dir.name,
            )
            continue
        try:
            name = synthesize_marketplace_name(plugin_name, skill_dir.name)
        except SkillNameError as e:
            logger.error(
                "marketplace %r plugin %r skill name synthesis failed, skipped: %s (%s)",
                marketplace,
                plugin_name,
                skill_dir.name,
                e.reason,
            )
            continue
        if name in seen:
            logger.error("duplicate marketplace SKILL name within staging run, keeping first: %s (%s)", name, skill_dir)
            continue
        seen.add(name)
        ref = _build_marketplace_ref(name, marketplace, frontmatter, version, skill_dir)
        if registry.register_or_update(ref):
            registered.append(name)
    return registered


async def locate_plugin_root(
    marketplace: str,
    plugin_name: str,
    entry: Mapping[str, Any],
    catalog_dir: Path,
    plugin_root_base: str,
    home: Path,
    catalog_sha: str | None,
    *,
    refresh: bool = False,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str | None]:
    """
    解析 plugin source 并 clone/定位其根，返回 ``(plugin_root, version_fallback_sha)`` / Resolve & clone/locate a plugin's root。

    plugin install（:mod:`a2c_smcp.computer.settings.installer`）与 :func:`_stage_one_plugin` 共用本原语，
    避免 plugin source 5 类解析（相对路径就地 / ``git-subdir`` sparse / ``url``·``github``·``cnb`` 独立 clone）
    重复实现。**不**扫描 / 注册 SKILL、**不**读 plugin.json——纯定位（install 需先拿 plugin 根再读
    ``mcp-servers/`` 做冲突预检，故定位须与 skill 扫描解耦）。

    - 相对路径（:class:`LocalPluginSource`）：就地在 catalog clone 内，越界保护（``is_within`` 词法判定）；
      ``version_fallback = catalog_sha``。
    - 独立 clone（:class:`GitCloneSpec`）：落 ``<home>/marketplace/.plugins/<mp>/<plugin>/``；复用条件——
      sha 锁版本既有 HEAD==pin 则复用、pin 变更则重 clone；非 sha 仅 ``refresh=False`` 复用。
      ``version_fallback = HEAD``。

    clone / source 解析失败 → :class:`SkillStagingError` / :class:`SkillSourceError`（上抛，调用方据失败降级）。
    """
    raw_source = entry.get("source")
    if raw_source is None:
        raise SkillSourceError(dict(entry), "plugin entry missing required 'source'")
    resolved = resolve_plugin_source(raw_source, plugin_root=plugin_root_base)

    if isinstance(resolved, LocalPluginSource):
        # 相对路径：就地在 catalog clone 内（不独立 clone）；越界保护（is_within 词法判定，两侧均已 resolve）。
        plugin_root = (catalog_dir / resolved.rel_path).resolve()
        if not is_within(plugin_root, catalog_dir.resolve()):
            raise SkillStagingError(f"relative plugin source escapes marketplace clone: {resolved.rel_path!r}")
        version_fallback = catalog_sha
    else:  # GitCloneSpec：独立 clone（git-subdir sparse / sha 锁版本等）。
        ext_dir = _external_plugin_dir(home, marketplace, plugin_name)
        head = await _git_head_sha(ext_dir, timeout=timeout, env=env) if ext_dir.exists() else None
        # 复用既有 clone 的条件（否则重 clone）：
        #  - sha 锁版本：既有 HEAD 已等于 pin → 复用（重 clone 无收益）；pin 变更（A→B）→ 重 clone 重 pin（守住
        #    refresh 契约，杜绝静默忽略新 sha）；
        #  - 非 sha：仅 refresh=False 复用；refresh=True 重 clone 拉最新。
        reuse = ext_dir.exists() and (head == resolved.sha if resolved.sha else not refresh)
        if not reuse:
            await _git_clone_plugin(resolved, ext_dir, timeout=timeout, env=env)
            head = await _git_head_sha(ext_dir, timeout=timeout, env=env)
        plugin_root = (ext_dir / resolved.subdir) if resolved.subdir else ext_dir
        version_fallback = head

    if not plugin_root.is_dir():
        raise SkillStagingError(f"plugin root not found after resolve: {plugin_root}")
    return plugin_root, version_fallback


async def _stage_one_plugin(
    marketplace: str,
    plugin_name: str,
    entry: Mapping[str, Any],
    catalog_dir: Path,
    plugin_root_base: str,
    home: Path,
    registry: SkillRegistry,
    seen: set[str],
    catalog_sha: str | None,
    *,
    refresh: bool,
    timeout: float,
    env: Mapping[str, str] | None,
) -> list[str]:
    """解析 plugin source → 定位 plugin 根 → 扫描注册其 SKILL / Resolve source, locate root, scan & register。"""
    plugin_root, version_fallback = await locate_plugin_root(
        marketplace,
        plugin_name,
        entry,
        catalog_dir,
        plugin_root_base,
        home,
        catalog_sha,
        refresh=refresh,
        timeout=timeout,
        env=env,
    )
    plugin_manifest = _read_plugin_manifest(plugin_root)
    version = _resolve_plugin_version(entry, plugin_manifest, version_fallback)
    return _scan_and_register_plugin_skills(marketplace, plugin_name, plugin_root, version, registry, seen)


def _resolve_plugin_version(entry: Mapping[str, Any], plugin_manifest: Mapping[str, Any], fallback_sha: str | None) -> str | None:
    """version 优先级：entry.version > plugin.json.version > git commit SHA（marketplace-v1 §4.2）/ Resolve plugin version。"""
    for src in (entry, plugin_manifest):
        v = src.get("version")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return fallback_sha


def _record_known_marketplace(
    name: str,
    source: Mapping[str, Any],
    clone_dir: Path,
    commit_sha: str | None,
    auto_update: bool,
    home: Path,
    env: Mapping[str, str] | None,
    *,
    changed: bool,
) -> None:
    """
    写 ``known_marketplaces.json`` 物化记录（持锁原子 RMW）/ Record into known_marketplaces.json（§6.1）。

    记 ``source`` / ``installLocation``（+ 可选 ``commitSha`` / ``autoUpdate``）。``lastUpdated`` 仅在
    ``changed``（本次实际 clone/pull）或无既有记录时刷为当下；复用既有 clone（``changed=False``）则**保留**原
    ``lastUpdated``——使该字段语义为「最后更新时间」而非「最后扫描时间」（§6.1）。失败（锁不可得 / I/O）仅记
    ERROR、**不**中断 staging（物化记录是诊断/对账元数据，丢失靠下次 reconcile 重建）。

    settings.store 经 skills.home 反向依赖本包，故在此**惰性 import** 破除模块级环依赖（store 仅在本函数调用时
    才被加载，彼时所有模块已初始化完毕）。
    """
    from a2c_smcp.computer.settings.store import update_known_marketplaces

    def _mutate(current: KnownMarketplacesFile) -> None:
        prior = current["marketplaces"].get(name)
        record: MarketplaceRecord = {
            "source": dict(source),  # type: ignore[typeddict-item]  # GitSource {type, url}
            "installLocation": str(clone_dir.resolve()),
        }
        # lastUpdated：实际 clone/pull 或首次记录 → 刷新；纯复用 → 保留既有值。
        prior_last = prior.get("lastUpdated") if prior else None
        record["lastUpdated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if (changed or not prior_last) else prior_last
        if commit_sha:
            record["commitSha"] = commit_sha
        if auto_update:
            record["autoUpdate"] = True
        current["marketplaces"][name] = record
        return None

    try:
        update_known_marketplaces(_mutate, home, env)
    except Exception as e:  # 锁不可得 / I/O 失败 → 不阻断 staging
        logger.error("failed to record known_marketplaces.json for %r: %s", name, e)


async def stage_marketplace_skills(
    name: str,
    source: Mapping[str, Any],
    registry: SkillRegistry,
    home: Path,
    *,
    plugin_filter: set[str] | None = None,
    auto_update: bool = False,
    refresh: bool = False,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """
    clone/refresh **单个** marketplace 并物化其 plugin SKILL → 注册进 Registry / Stage one marketplace's SKILLs。

    对账编排（declared∖materialized additive-only diff）、``enabledPlugins`` 过滤、孤儿清理归 reconciler（#62）；
    本函数只「clone 一个 marketplace + 解析 plugin source + 扫描 skills + 注册」。失败降级铁律（design §2.2 /
    工单 §8）：clone/pull/解析/物化失败 → 记 ERROR、该源 / 该 plugin 不入 Registry、**不**抛、**不**阻断其余。

    :param name: marketplace 名（``known_marketplaces.json`` key + ``<home>/marketplace/<name>/`` clone 目录段）。
    :param source: marketplace git 源 ``{type:"git", url}``（:class:`~a2c_smcp.computer.settings.schema.GitSource`）。
    :param registry: 目标 :class:`SkillRegistry`。
    :param home: SKILL Home 绝对根。
    :param plugin_filter: 仅物化这些 plugin 名（``None`` = marketplace.json 全部）；#62 注入 ``enabledPlugins ∩ installed``。
    :param auto_update: 写入物化记录的 ``autoUpdate`` 旗（仅记录，本函数不据此自动刷新）。
    :param refresh: ``True`` → 已存在 catalog clone 走 ``git pull``（失败重 clone）+ 独立 plugin 重 clone
        （sha 锁版本例外：仅在既有 HEAD ≠ pin 时重 clone，等于 pin 则复用）；``False`` → 缺失才 clone、
        已存在则就地复用（重扫）。

    .. note::
       本函数经 :func:`_record_known_marketplace` 写 ``known_marketplaces.json`` 时持**同步阻塞**文件锁
       （:func:`~a2c_smcp.computer.settings.store.file_lock`，竞争时 ``time.sleep`` 退避）。当前单 marketplace
       串行调用无碍；若编排方（#62）以 ``asyncio.gather`` 并发 stage 多个 marketplace，须串行化或用
       ``loop.run_in_executor`` 包裹，避免阻塞事件循环（store.py 同步设计的固有约束）。
    :param timeout: 单次 git 操作超时（秒）。
    :param env: git 子进程环境（默认 ``os.environ``；便于测试注入）。
    :return: 本次成功注册 / 刷新的 SKILL name 列表（供 reconciler / watcher diff 孤儿）。
    """
    registered: list[str] = []

    # name 直接作 <home>/marketplace/<name>/ 路径段——本函数已进公开 API（__all__），加防御纵深：
    # 仅接受严格 kebab marketplace 名（与 settings load 的 is_valid_marketplace_name 同契约），天然拒
    # ``..`` / ``/`` 等路径穿越向量（上游 settings 已校验，此处兜底未来非 settings 调用方）。
    if not is_valid_marketplace_name(name):
        logger.error("marketplace name %r is invalid (must be strict-kebab, 1-64), skipped (not registered)", name)
        return registered

    try:
        url = marketplace_clone_url(source)
    except SkillSourceError as e:
        logger.error("marketplace %r has invalid source, skipped (not registered): %s", name, e)
        return registered

    # home 由调用方保证存在（与 stage_mcp_skills / stage_user_skills 一致）；clone 目录父级由
    # _git_clone_with_fallback 落盘前 mkdir。
    clone_dir = marketplace_skill_dir(home, name)
    # changed：本次是否实际发生 clone/pull——决定 known_marketplaces.json 的 lastUpdated 是否刷新
    # （复用既有 clone 不算更新，避免 lastUpdated 退化为「最后扫描时间」）。
    changed = False
    try:
        if clone_dir.exists():
            if refresh:
                await _git_refresh_marketplace(clone_dir, url, timeout=timeout, env=env)
                changed = True
        else:
            await _git_clone_marketplace(url, clone_dir, timeout=timeout, env=env)
            changed = True
    except SkillStagingError as e:
        logger.error("marketplace %r clone/refresh failed, skipped (not registered): %s", name, e)
        return registered

    commit_sha = await _git_head_sha(clone_dir, timeout=timeout, env=env)
    _record_known_marketplace(name, source, clone_dir, commit_sha, auto_update, home, env, changed=changed)

    try:
        manifest = _read_marketplace_manifest(clone_dir)
    except SkillStagingError as e:
        logger.error("marketplace %r manifest invalid, skipped (clone kept): %s", name, e)
        return registered

    plugins = manifest.get("plugins")
    if not isinstance(plugins, list):
        logger.error("marketplace %r manifest 'plugins' is not an array, skipped: %s", name, type(plugins).__name__)
        return registered

    plugin_root_base = _plugin_root_base(manifest)
    seen: set[str] = set()
    for entry in plugins:
        if not isinstance(entry, Mapping):
            logger.error("marketplace %r has a non-object plugin entry, skipped: %r", name, entry)
            continue
        try:
            plugin_name = _entry_plugin_name(entry)
        except SkillStagingError as e:
            logger.error("marketplace %r plugin entry invalid, skipped: %s", name, e)
            continue
        if plugin_filter is not None and plugin_name not in plugin_filter:
            continue
        try:
            names = await _stage_one_plugin(
                name,
                plugin_name,
                entry,
                clone_dir,
                plugin_root_base,
                home,
                registry,
                seen,
                commit_sha,
                refresh=refresh,
                timeout=timeout,
                env=env,
            )
            registered.extend(names)
        except (SkillSourceError, SkillStagingError) as e:
            logger.error("marketplace %r plugin %r staging failed, skipped: %s", name, plugin_name, e)
        except Exception as e:  # 失败降级铁律：单 plugin 任意异常不阻断其余
            logger.error("marketplace %r plugin %r unexpected staging error, skipped: %s", name, plugin_name, e, exc_info=True)
    return registered
