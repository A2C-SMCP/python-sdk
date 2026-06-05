# -*- coding: utf-8 -*-
# filename: resolver.py
# @Author  : JQQ
# @Software: PyCharm
"""
``blob_handle`` 解析器接口 + 内置实现 / ``blob_handle`` resolver interface + built-ins.

设计依据 / Design source: ``docs/design-0.2.1-skill-computer-management.md`` §4.3「无状态可重解析」.

核心契约 / Core contract:
  - 每次 ``client:get_blob`` 独立确定性回源、幂等、可并行（不同 ``chunk_offset``）;
  - **重施铸造通道边界校验**（kind=skill → SKILL 沙箱；kind=toolspool → cid 白名单 + 路径围栏）；
  - 句柄内容**绝不**被直接信任（协议 ``blob-transfer.md`` §5.4）。

#39 接管 / Handed over by #39 (#66): ``SkillBlobResolver``（基于 Skill Registry + sandbox + 消费字节
视图）落地，取代早期 ``SkillBlobResolverPending`` 占位。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from a2c_smcp.computer.blob.handle import BlobHandleForbiddenError, BlobHandleGoneError
from a2c_smcp.computer.blob.toolspool import ToolspoolBlobStore
from a2c_smcp.computer.skills.registry import SkillRegistry
from a2c_smcp.computer.skills.resource import resolve_skill_view
from a2c_smcp.computer.skills.sandbox import SkillSandboxError

# 惰性切片闭包类型 / Lazy slice closure type: (offset, length) -> bytes
# Resolver 在 ``resolve()`` 中构造此闭包并注入 ResolvedBlob，handler 切片时按需回读。
# Constructed by resolver inside ``resolve()``; invoked lazily by handler at slice time.
_BlobSlicer = Callable[[int, int], bytes]


@dataclass(frozen=True, eq=False)
class ResolvedBlob:
    """``blob_handle`` 解析后的资源描述符 / Resolved blob descriptor.

    惰性切片视图（v0.2.1 #51）：``payload`` 不再驻留全量字节；``slice(offset, length)``
    按需经 ``_slicer`` 闭包从源 seek+read。``resolve()`` 期间 O(1) 内存。
    Lazy slice view (v0.2.1 #51): ``payload`` no longer holds full bytes; ``slice()`` reads
    on demand via ``_slicer`` closure (seek+read). ``resolve()`` is O(1) memory.

    Attributes:
        mime: 内容 MIME（handle-authoritative，由 resolver 从句柄载荷透传）。
            MIME from handle payload (authoritative).
        total_size: 资源总字节数。Total resource size in bytes.
        sha256: 全量 ``sha256`` 十六进制（kind=toolspool 即 cid）/ Full sha256 hex.
        _slicer: closure ``(offset, length) -> bytes``；resolver 提供的惰性读闭包。
            Resolver-supplied lazy-read closure; never None in valid construction.

    Notes:
        - ``frozen=True``：表达 "resolver 派发的不可变快照" 语义；handler MUST NOT mutate.
        - ``eq=False``：closure 字段不参与 hash/equality（resolver 每次 resolve 创建新实例，
          实例对比走 identity）。Closure field skipped in eq/hash; identity comparison.
    """

    mime: str
    total_size: int
    sha256: str
    _slicer: _BlobSlicer = field(repr=False)

    def slice(self, offset: int, length: int) -> bytes:
        """读取 ``[offset, offset+length)`` 字节区间，截断到 ``total_size``.
        Read bytes in ``[offset, offset+length)``, clamped to ``total_size``.

        边界语义 / Boundary semantics:
          - ``offset == total_size`` → ``b""``（EOF probe，**非** range error）
          - ``offset > total_size`` → ``ValueError``（调用方应先做范围校验）
          - ``offset < 0`` or ``length < 0`` → ``ValueError``
          - ``length == 0`` → ``b""``（无 I/O 触发）
          - ``length`` 超出剩余 → 自动截断为 ``total_size - offset``（不 raise）

        EOF probe 语义保留与 ``on_get_blob`` 严格 ``>`` 范围守卫一致，避免破坏 HTTP Range
        风格的"探测末尾"客户端行为。
        Preserves HTTP-Range-style probe semantics (offset == total_size returns empty,
        offset > total_size raises) aligned with ``on_get_blob``'s strict ``>`` guard.
        """
        if offset < 0 or length < 0:
            raise ValueError(f"offset/length must be non-negative: offset={offset}, length={length}")
        if offset > self.total_size:
            raise ValueError(f"offset {offset} > total_size {self.total_size}")
        if length == 0 or offset == self.total_size:
            return b""
        actual = min(length, self.total_size - offset)
        return self._slicer(offset, actual)

    @property
    def payload(self) -> bytes:
        """向后兼容只读视图：等价 ``slice(0, total_size)``，**会触发一次全量读**.

        新代码 SHOULD 改用 :meth:`slice` 避免内存放大；保留此属性仅为 v0.2.x 迁移期与
        现有单测断言。计划于 v0.3 移除。
        Back-compat shim; triggers a full read. New code SHOULD use :meth:`slice` to avoid
        memory amplification. Retained for the v0.2.x migration window; removed in v0.3.

        .. deprecated:: 0.2.1
            Use :meth:`slice` instead.
        """
        return self.slice(0, self.total_size)


@runtime_checkable
class BlobResolver(Protocol):
    """``blob_handle`` 解析器抽象接口 / Resolver protocol.

    实现者职责 / Implementer responsibility:
      - 解析 ``payload``（kind-specific dict，由 :func:`decode_blob_handle` 提供）→ 字节内容；
      - **重施铸造通道边界校验**；任何越权 / 不可达 / 范围错误均通过抛 :class:`BlobHandleError`
        子类传达（``invalid_handle`` / ``forbidden`` / ``gone``）;
      - 实现 **MUST** 是无副作用、确定性、幂等的——同一句柄可被任意并发 ``get_blob`` 调用解析。
    """

    def resolve(self, payload: dict[str, Any]) -> ResolvedBlob:
        """解析 kind-specific payload → :class:`ResolvedBlob`；越界 / 失败抛 :class:`BlobHandleError`。"""
        ...  # pragma: no cover — protocol stub


class ToolspoolBlobResolver:
    """``kind="toolspool"`` 解析器：按 ``cid`` 回查 ``.blobspool`` 暂存（惰性切片）.

    跨进程重启可解析：cid 在磁盘 → 成功；不在 → ``BlobHandleGoneError`` （协议 ``4018 gone``）。
    Survives Computer restarts: cid present on disk → success; absent → ``BlobHandleGoneError``.

    惰性切片（v0.2.1 #51）：``resolve()`` 仅读 metadata（``stat()``），不读 payload 字节；
    ``ResolvedBlob.slice(offset, length)`` 经 ``ToolspoolBlobStore.read_chunk`` 按需 seek+read。
    并行 ``concurrency=N`` 拉取大 blob 时单 chunk 入内存，峰值与 ``total_size`` 解耦。
    "无状态可重解析" 承诺不变，仅是内存峰值优化，非协议变更。
    Lazy slice (v0.2.1 #51): ``resolve()`` reads metadata only via ``stat()``, never payload bytes;
    ``ResolvedBlob.slice(offset, length)`` reads on demand via ``ToolspoolBlobStore.read_chunk``.
    Concurrent fetches see per-chunk memory peak, decoupled from ``total_size``.
    Stateless-reparseable contract unchanged.
    """

    def __init__(self, store: ToolspoolBlobStore) -> None:
        self._store = store

    def resolve(self, payload: dict[str, Any]) -> ResolvedBlob:
        cid = payload["cid"]
        mime_in_handle = payload["mime"]
        # stat 仅读元数据（size + sidecar mime）+ cid 白名单 + realpath 围栏 + GoneError/InvalidError 抛出
        # stat reads metadata only (size + sidecar mime) + cid whitelist + realpath fence
        info = self._store.stat(cid)
        # mime 以铸造时入参为准；磁盘旁路只作诊断，不参与协议返回
        # Handle-embedded mime is authoritative; disk sidecar is diagnostic only
        _ = info.mime
        # 闭包仅捕获 ``store + cid``（**不**捕获 self）：让 ``ResolvedBlob`` 反向引用最小化，
        # 避免间接钉住 ``ToolspoolBlobResolver`` 实例，便于未来 ``ResolvedBlob`` 被长时缓存
        # 场景下的 GC。每次 slice 调用都触发独立 seek+read（O(1) memory per call）.
        # Capture ``store + cid`` only (NOT self): keeps ``ResolvedBlob``'s back-references
        # minimal so it doesn't pin the ``ToolspoolBlobResolver`` instance — useful for
        # future cached scenarios. Each slice call triggers independent seek+read.
        store = self._store

        def _slicer(offset: int, length: int) -> bytes:
            return store.read_chunk(cid, offset, length)

        # cid 即 sha256，无需重算（stat 已 fail-fast 路径校验）
        # cid IS the sha256; no need to recompute (stat already path-validated).
        return ResolvedBlob(
            mime=mime_in_handle,
            total_size=info.size,
            sha256=cid,
            _slicer=_slicer,
        )


class SkillBlobResolver:
    """``kind="skill"`` 解析器：name→Registry 解析包根 → ``rel_path`` 重跑 §6 沙箱 → 惰性切片回源.

    无状态、确定性、幂等（设计 §4.3）：**忽略句柄内任何路径推导**（协议 ``blob-transfer.md`` §5.4
    「不信任句柄内容」），仅以 ``name`` 经 :class:`SkillRegistry` O(1) 解析包根，再对 ``rel_path``
    经 :func:`~a2c_smcp.computer.skills.resource.resolve_skill_view` **重跑沙箱**。``total_size`` /
    ``sha256`` 基于消费字节（SKILL.md→frontmatter 剥离后 body；其它→原始字节），与 ``client:get_skill``
    铸造期严格一致（设计 §4.4「资源字节三处一致」）。
    Stateless / deterministic / idempotent: ignores any path embedded in the handle, resolves the
    package root via ``name`` through the Registry, then re-runs the §6 sandbox on ``rel_path``.
    ``total_size`` / ``sha256`` match the get_skill mint exactly (shared consumed-bytes view).

    §9.2 name 寻址防越权 / anti-escalation：包根**仅**由 Registry 经 name 解析；``resolve_skill_view``
    只接受 ``root: Path``，绝不从 name/rel_path 推导 FS 路径。

    错误映射（→ 协议 4018 ``details.reason``）/ Error mapping:
      - Registry 未命中 / 孤儿 → :class:`BlobHandleGoneError`（``gone``：SKILL 已卸载 / 不可用）；
      - 沙箱 ``not_found``（源文件铸造后消失）→ ``gone``；
      - 沙箱 ``traversal`` / ``forbidden`` → :class:`BlobHandleForbiddenError`（``forbidden``）；
      - msgpack 格式 / kind 不识别由 :func:`decode_blob_handle` 在更上层映射 ``invalid_handle``（不达此）。
    """

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def resolve(self, payload: dict[str, Any]) -> ResolvedBlob:
        name = payload["name"]
        rel_path = payload["rel_path"]
        ref = self._registry.resolve(name)
        if ref is None:
            # name 未注册 / 已孤儿 → 铸造通道授权已撤销，源不可服务（gone）。
            raise BlobHandleGoneError(f"skill not resolvable: name={name!r} (unregistered or orphaned)")
        root = Path(ref["path"])
        try:
            # get_blob 不传 too_large_cap：协议 4018 无 too_large 语义，超大资源由分块传输自然承载。
            view = resolve_skill_view(root, rel_path)
        except SkillSandboxError as e:
            if e.reason == "not_found":
                raise BlobHandleGoneError(f"skill resource gone: name={name!r} rel_path={rel_path!r}") from e
            raise BlobHandleForbiddenError(
                f"skill resource forbidden: name={name!r} rel_path={rel_path!r} reason={e.reason}",
            ) from e
        # view.slice 已满足 ``(offset, length) -> bytes`` 切片契约（含截断/边界校验），直接作 _slicer。
        return ResolvedBlob(mime=view.mime, total_size=view.total_size, sha256=view.sha256, _slicer=view.slice)
