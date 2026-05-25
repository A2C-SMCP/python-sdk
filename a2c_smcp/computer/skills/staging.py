# -*- coding: utf-8 -*-
# filename: staging.py
# @Time    : 2026/05/24
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL staging：mcp 源物化（v0.2.1）
SKILL staging: mcp-source materialization (v0.2.1)

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §3（MCP source 模式 mounted/archive/
                      resources）、§4（包根目录名 = frontmatter.name）、§12（Computer 完整消费 cursor）。
SDK 设计 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §5.2。

本模块只实现 **mcp 源** 物化（#59）；marketplace git（#61）/ user DropIn（#60）另见对应模块。
This module implements **mcp-source** materialization only (#59).

流程 / Flow：
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

失败降级 / Failure isolation：任一 SKILL 物化/解析失败 → 记 ERROR、清理半成品、跳过该 SKILL，
**不**阻断其余、**不**抛给上层（skill.md §1.5：batch 接口对部分失败健壮）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import shutil
import tarfile
import zipfile
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from mcp.types import BlobResourceContents, ReadResourceResult, Resource, TextResourceContents

from a2c_smcp.computer.skills.home import mcp_skill_dir
from a2c_smcp.computer.skills.naming import SkillNameError, normalize_mcp_server_segment, synthesize_mcp_name
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.smcp import A2CSkillRef
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

SKILL_MD = "SKILL.md"
SKILL_URI_PREFIX = "skill://"
_MCP_SOURCE_MODES = frozenset({"mounted", "archive", "resources"})

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
    if frontmatter.get("license") is not None:
        ref["license"] = str(frontmatter["license"])
    if frontmatter.get("compatibility") is not None:
        ref["compatibility"] = str(frontmatter["compatibility"])
    allowed = frontmatter.get("allowed-tools", frontmatter.get("allowed_tools"))
    if allowed is not None:
        ref["allowed_tools"] = [str(t) for t in allowed] if isinstance(allowed, (list, tuple)) else [str(allowed)]
    if isinstance(frontmatter.get("metadata"), dict):
        ref["skill_metadata"] = frontmatter["metadata"]
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
    ok = registry.update(ref) if name in registry else registry.register(ref)
    return name if ok else None
