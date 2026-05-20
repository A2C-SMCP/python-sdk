# -*- coding: utf-8 -*-
# filename: handle.py
# @Author  : JQQ
# @Software: PyCharm
"""
``blob_handle`` 单层无状态编解码 / Single-layer stateless ``blob_handle`` codec.

形态 / Shape::

    blob_handle = base64url( msgpack({
        "v": 1,
        "kind": "skill" | "toolspool",
        # kind=skill:     {"name": <A2CSkillRef.name>, "rel_path": <POSIX rel>}
        # kind=toolspool: {"cid": <sha256 hex>, "mime": <mime>}
    }) )

为何无 MAC / Why no MAC:
    协议 ``blob-transfer.md`` §1/§5.4 把安全边界定在「Computer 解析时 **MUST** 重跑铸造通道
    边界校验，不信任句柄内容」。MAC 是冗余防御层（伪造句柄仍要过 Registry + 沙箱），不增加
    任何真安全，却引入 per-process secret 管理 / 轮转 / 测试 fixture 负担——故移除。业界对照：
    SFTP / OCI registry / gRPC / OpenAI Realtime 均不签内部传输 token。
    The protocol places the security boundary at the resolver: every ``get_blob`` MUST re-apply
    the minting channel's authorization, treating handle contents as untrusted input. A MAC is
    redundant — forged handles still have to pass Registry + sandbox — and adds secret-management
    cost, so we omit it. Industry parallels: SFTP / OCI / gRPC / OpenAI Realtime all skip signing.

不透明性 / Opacity:
    Agent **MUST NOT** 解析 / 拼接 / 伪造句柄（协议 §1 行为约束，由 Agent SDK 担保）。
    本模块只服务 Computer 侧：minter 生成、resolver 解析。
"""

from __future__ import annotations

import base64
from typing import Any, Literal, TypedDict

import msgpack

# 当前句柄版本号 / Current handle version
HANDLE_VERSION: int = 1

# kind 取值集合（开放枚举，未来非破坏新增）/ kind enum (open set; non-breaking additions allowed)
HANDLE_KIND_SKILL: Literal["skill"] = "skill"
HANDLE_KIND_TOOLSPOOL: Literal["toolspool"] = "toolspool"


class SkillHandlePayload(TypedDict):
    """``kind="skill"`` 句柄 payload。

    ``rel_path`` 由 Computer 在 ``get_blob`` 解析时**重跑沙箱**，绝不直接信任
    （协议 ``blob-transfer.md`` §5.4 + ``skill.md`` §9）。
    """

    name: str
    rel_path: str


class ToolspoolHandlePayload(TypedDict):
    """``kind="toolspool"`` 句柄 payload。

    ``cid`` = 内容寻址 ``sha256`` hex；``mime`` 回显铸造时的 MIME。
    """

    cid: str
    mime: str


# ── 异常体系 / Error taxonomy ────────────────────────────────────────────
# 对应协议 4018 ``details.reason`` 开放枚举（``invalid_handle`` / ``forbidden`` / ``gone`` /
# ``range``）。本模块只关心 codec 自身能判别的部分（``invalid_handle``）；``forbidden`` / ``gone``
# / ``range`` 由 resolver 在重施鉴权 / 回查物化 / 切片时各自抛出。
# Corresponds to protocol 4018 ``details.reason`` open enum. This module raises only what the
# codec itself can detect (``invalid_handle``); the rest is raised by the resolver layer.


class BlobHandleError(Exception):
    """``blob_handle`` 处理异常基类 / Base class for blob-handle errors.

    具体子类对齐协议 4018 ``details.reason``——SDK 上层据此构造 flat ``ErrorPayload``。
    Subclasses map 1:1 to protocol 4018 ``details.reason`` values for ``ErrorPayload`` mapping.
    """

    reason: str = "invalid_handle"  # 默认归类，子类覆盖 / default reason; subclasses override


class BlobHandleInvalidError(BlobHandleError):
    """格式非法 / 非本 Computer 铸造 / 无法识别 → ``4018 invalid_handle``。"""

    reason = "invalid_handle"


class BlobHandleForbiddenError(BlobHandleError):
    """重施铸造通道鉴权失败 → ``4018 forbidden``。

    句柄结构有效但解析时通道授权被撤销（如 SKILL 已 orphan、``.skillenv`` 等仍禁止）。
    """

    reason = "forbidden"


class BlobHandleGoneError(BlobHandleError):
    """源已不可达 → ``4018 gone``。

    SKILL 已卸载 / toolspool 文件被 GC / 内容已无法服务。
    """

    reason = "gone"


# ── 公开 API / Public API ────────────────────────────────────────────────


def encode_skill_handle(name: str, rel_path: str) -> str:
    """铸造 ``kind=skill`` 不透明句柄 / Mint an opaque ``kind="skill"`` handle.

    Args:
        name: ``A2CSkillRef.name``（含 source prefix 的合成全局唯一名）。
        rel_path: SKILL 包根 POSIX 相对路径；Computer 解析时**重跑沙箱**，本编码不做校验。

    Returns:
        base64url 字符串（无填充 ``=``，URL 安全）/ base64url string (no padding, URL safe).
    """
    payload: dict[str, Any] = {"v": HANDLE_VERSION, "kind": HANDLE_KIND_SKILL, "name": name, "rel_path": rel_path}
    return _encode(payload)


def encode_toolspool_handle(cid: str, mime: str) -> str:
    """铸造 ``kind=toolspool`` 不透明句柄 / Mint an opaque ``kind="toolspool"`` handle.

    Args:
        cid: 内容寻址 id（``sha256`` 十六进制）—— 已由暂存写盘流程产生。
        mime: 内容 MIME（如 ``image/png``），回显给 Agent。

    Returns:
        base64url 字符串。
    """
    payload: dict[str, Any] = {"v": HANDLE_VERSION, "kind": HANDLE_KIND_TOOLSPOOL, "cid": cid, "mime": mime}
    return _encode(payload)


def decode_blob_handle(handle: str) -> tuple[Literal["skill", "toolspool"], dict[str, Any]]:
    """解码 ``blob_handle`` → ``(kind, payload_dict)``。

    解码失败的所有路径（非 ASCII / 非 base64url / 非 msgpack / 缺字段 / ``v`` 未知 /
    kind 未知）一律抛 :class:`BlobHandleInvalidError`，由调用方映射到协议
    ``4018 invalid_handle``。

    All failure paths (non-ASCII, non-base64url, non-msgpack, missing fields, unknown ``v``,
    unknown ``kind``) raise :class:`BlobHandleInvalidError`; callers map to protocol
    ``4018 invalid_handle``.

    协议依据 / Protocol: ``blob-transfer.md`` §5.4「不信任句柄内容」/ "do not trust handle contents".
    """
    raw = _decode_bytes(handle)
    try:
        payload = msgpack.unpackb(raw, raw=False, strict_map_key=True)
    except (msgpack.UnpackException, ValueError, TypeError) as e:
        raise BlobHandleInvalidError(f"msgpack decode failed: {e}") from e
    if not isinstance(payload, dict):
        raise BlobHandleInvalidError(f"handle payload must be a mapping, got {type(payload).__name__}")
    v = payload.get("v")
    if v != HANDLE_VERSION:
        raise BlobHandleInvalidError(f"unsupported handle version: {v!r}")
    kind = payload.get("kind")
    if kind == HANDLE_KIND_SKILL:
        name = payload.get("name")
        rel_path = payload.get("rel_path")
        if not isinstance(name, str) or not isinstance(rel_path, str):
            raise BlobHandleInvalidError("skill handle missing name / rel_path")
        return HANDLE_KIND_SKILL, {"name": name, "rel_path": rel_path}
    if kind == HANDLE_KIND_TOOLSPOOL:
        cid = payload.get("cid")
        mime = payload.get("mime")
        if not isinstance(cid, str) or not isinstance(mime, str):
            raise BlobHandleInvalidError("toolspool handle missing cid / mime")
        return HANDLE_KIND_TOOLSPOOL, {"cid": cid, "mime": mime}
    raise BlobHandleInvalidError(f"unknown handle kind: {kind!r}")


# ── 内部辅助 / Internal helpers ──────────────────────────────────────────


def _encode(payload: dict[str, Any]) -> str:
    packed = msgpack.packb(payload, use_bin_type=True)
    if packed is None:  # pragma: no cover — msgpack.packb returns None only on type errors prior to 1.0
        raise BlobHandleInvalidError("msgpack.packb returned None")
    return base64.urlsafe_b64encode(packed).rstrip(b"=").decode("ascii")


def _decode_bytes(handle: str) -> bytes:
    if not isinstance(handle, str):
        raise BlobHandleInvalidError(f"handle must be str, got {type(handle).__name__}")
    if not handle:
        raise BlobHandleInvalidError("empty handle")
    # base64url 解码，补齐 padding（spec 上 base64url 通常 strip ``=``）
    # base64url decode, re-pad (spec base64url typically strips ``=``)
    pad = (-len(handle)) % 4
    try:
        return base64.urlsafe_b64decode(handle + ("=" * pad))
    except (ValueError, TypeError, base64.binascii.Error) as e:  # type: ignore[attr-defined]
        raise BlobHandleInvalidError(f"base64url decode failed: {e}") from e
