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

#39 接管 / Handed over by #39: ``SkillBlobResolverPending`` 占位 → 被 ``SkillBlobResolver``
（基于 Skill Registry + sandbox）替换；本 PR 仅提供 toolspool 完整实现 + skill 占位。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from a2c_smcp.computer.blob.handle import BlobHandleError
from a2c_smcp.computer.blob.toolspool import ToolspoolBlobStore


@dataclass(frozen=True)
class ResolvedBlob:
    """``blob_handle`` 解析后的资源描述符 / Resolved blob descriptor.

    Attributes:
        payload: 完整字节内容（解析期一次性 / 切片由 handler 完成）。Full raw bytes.
        mime: 内容 MIME。
        total_size: 资源总字节数（= ``len(payload)``，显式字段便于审计与单测断言）。
        sha256: 全量 ``sha256`` 十六进制（由 resolver 自行算或回查既有 cid 值）。
    """

    payload: bytes
    mime: str
    total_size: int
    sha256: str


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
    """``kind="toolspool"`` 解析器：按 ``cid`` 回查 ``.blobspool`` 暂存。

    跨进程重启可解析：cid 在磁盘 → 成功；不在 → ``BlobHandleGoneError`` （协议 ``4018 gone``）。
    Survives Computer restarts: cid present on disk → success; absent → ``BlobHandleGoneError``.
    """

    def __init__(self, store: ToolspoolBlobStore) -> None:
        self._store = store

    def resolve(self, payload: dict[str, Any]) -> ResolvedBlob:
        cid = payload["cid"]
        mime_in_handle = payload["mime"]
        # ToolspoolBlobStore.get 已做 cid 白名单 + realpath 越界防御 + GoneError 抛出
        # store.get already validates cid + realpath + raises GoneError
        raw, mime_on_disk = self._store.get(cid)
        # cid 即 sha256，无需重算（store.get 已 fail-fast 路径校验）
        # cid IS the sha256; no need to recompute (store.get already path-validated)
        # mime 以铸造时入参为准；磁盘旁路只作诊断，不参与协议返回
        # Handle-embedded mime is authoritative; disk sidecar is diagnostic only
        _ = mime_on_disk
        return ResolvedBlob(payload=raw, mime=mime_in_handle, total_size=len(raw), sha256=cid)


class SkillBlobResolverPending:
    """``kind="skill"`` 解析器占位（PR #38 边界，#39 接管）.

    Placeholder for ``kind="skill"`` resolution; the concrete implementation lands in #39 (Computer
    SKILL subsystem) wiring up Skill Registry + sandbox. 本 PR 保留此占位是为了让 ``Computer``
    层默认即拥有完整 kind 派发，#39 仅替换实现而无须改 handler。

    Behavior: ``resolve`` 一律抛 :class:`BlobHandleError`（``forbidden`` reason），等同
    「resolver 未实现 → 视为铸造通道授权撤销」——保守且符合协议「不信任句柄」边界。
    Raises ``BlobHandleError(reason="forbidden")``: until #39 plugs in, the channel is treated as
    revoked — conservative and aligned with the "do not trust the handle" boundary.
    """

    def resolve(self, payload: dict[str, Any]) -> ResolvedBlob:
        # noqa 标注：故意保留参数签名以 satisfy BlobResolver Protocol（runtime_checkable）。
        # Signature preserved to satisfy the runtime_checkable BlobResolver Protocol.
        _ = payload
        err = BlobHandleError("skill resolver pending #39 — kind=skill not wired in this PR")
        err.reason = "forbidden"
        raise err
