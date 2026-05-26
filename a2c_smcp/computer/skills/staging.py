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

本模块实现 **mcp 源** 物化（#59）与 **user 源** DropIn 发现（#60）；marketplace git（#61）另见对应模块。
This module implements **mcp-source** materialization (#59) and **user-source** DropIn discovery (#60).

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

import base64
import hashlib
import io
import shutil
import tarfile
import zipfile
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator, Sequence
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from mcp.types import BlobResourceContents, ReadResourceResult, Resource, TextResourceContents

from a2c_smcp.computer.skills.home import SOURCE_USER, mcp_skill_dir, user_dropin_root, workdir_skill_root
from a2c_smcp.computer.skills.naming import (
    SkillNameError,
    normalize_mcp_server_segment,
    synthesize_mcp_name,
    synthesize_user_name,
)
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
