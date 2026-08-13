# -*- coding: utf-8 -*-
# filename: oauth_credential_store.py
# @Time    : 2026/08/11
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
可注入 OAuth 凭据存储抽象，逐字段对齐 rust-sdk ``crates/smcp-computer/src/oauth.rs``。

协议归属：SDK 层（不涉及 A2C-SMCP 协议变更）。
父 Epic：#176；本 Sub：#180（OAuthCredentialStore + 隔离维度）。

.. note::
    - ``OAuthCredentialStore`` Protocol 供宿主注入持久化后端（Keychain / Vault）。
    - 默认 ``InMemoryOAuthCredentialStore`` 进程内存储，SDK **不主动探测 OS keyring**。
    - 显式注入 store 后失败不静默降级到内存。
    - ``StoredCredentialEnvelope`` / ``StoredCredentialIndex`` 版本化以兼容升级。
    - mcp>=1.15 ``TokenStorage`` 不绑定 issuer ⇒ Python 自建 issuer 校验（envelope 层）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from a2c_smcp.computer.mcp_clients.oauth_types import OAuthOptions

# ============================================================================
# OAuthCredentialRecordKind
# ============================================================================


class OAuthCredentialRecordKind(StrEnum):
    """凭据存储的记录类型（对齐 Rust ``OAuthCredentialRecordKind``）。

    三种记录由同一个 ``OAuthCredentialStore`` 承载：
    - ``Credentials``：序列化的 token 凭据
    - ``IssuerIndex``：Core 拥有的 issuer 索引 + 活跃凭据快照
    - ``ClientRegistration``：DCR 注册信息（client_id / client_secret / redirect_uris）
    """

    Credentials = "credentials"
    """序列化的 token 凭据。"""
    IssuerIndex = "issuerIndex"
    """Core 拥有的 issuer 索引和活跃凭据快照。

    将活跃快照保留在此单一记录中，宿主可原子替换凭据集，同时保留 issuer 列表
    用于 network-free 清理。
    """
    ClientRegistration = "clientRegistration"
    """DCR 客户端注册信息（client_id / client_secret / redirect_uris）。

    token 交换前 DCR 即完成，注册信息需独立存储以避免与 token envelope
    产生 key 冲突（两者共享 bundle_id + resource + issuer + grant_fingerprint
    组合，仅 record_kind 不同）。
    """


# ============================================================================
# OAuthCredentialKey
# ============================================================================


@dataclass(frozen=True)
class OAuthCredentialKey:
    """稳定的、bundle 感知的凭据存储键（对齐 Rust ``OAuthCredentialKey``）。

    按 5 维隔离：
    1. ``bundle_id`` — MCP server bundle 身份
    2. ``resource`` — 规范化的受保护资源标识
    3. ``issuer`` — 授权服务器 issuer（可选；``None`` 表示 <none>）
    4. ``grant_fingerprint`` — grant / client 指纹（mode + client_id + scopes）
    5. ``record_kind`` — 记录类型（credentials | issuerIndex）

    ``tenant`` / ``principal`` 属于宿主运行时上下文：多租户宿主的 store 可在
    ``stable_id()`` 外层追加 namespace，而不将部署身份放入可序列化的 MCP 配置。
    """

    bundle_id: str
    """MCP server bundle 身份（``normalize_name(name)`` 或显式 ``bundleId``）。"""
    resource: str
    """规范化的受保护资源（URL 或 URI，如 ``https://api.example.com``）。"""
    issuer: str | None
    """授权服务器 issuer（``None`` 视为 ``<none>``）。"""
    grant_fingerprint: str
    """grant/client 指纹：``{mode}:scopes-{scope_hash}``。"""
    record_kind: OAuthCredentialRecordKind
    """记录类型：credentials 或 issuerIndex。"""

    def stable_id(self) -> str:
        """确定性的、非秘密的持久化 locator（对齐 Rust ``OAuthCredentialKey::stable_id``）。

        对 5 维隔离字段做以 ``\\0`` 分隔的 SHA-256 摘要，产出 ``mcp-oauth-{hex}`` 格式的
        stable identifier。多租户宿主应在外层用可信 tenant/principal 上下文做 namespace——
        callback 参数和序列化后的 MCP 配置**不是**可信来源。

        .. code-block:: text

            SHA-256(bundle_id + "\\0" + resource + "\\0" + issuer_or_<none> + "\\0"
                    + grant_fingerprint + "\\0" + "credentials"|"issuer-index")
        """
        data = bytearray()
        data.extend(self.bundle_id.encode("utf-8"))
        data.extend(b"\0")
        data.extend(self.resource.encode("utf-8"))
        data.extend(b"\0")
        data.extend((self.issuer if self.issuer is not None else "<none>").encode("utf-8"))
        data.extend(b"\0")
        data.extend(self.grant_fingerprint.encode("utf-8"))
        data.extend(b"\0")
        data.extend(self.record_kind.value.encode("utf-8"))
        digest = hashlib.sha256(data).hexdigest()
        return f"mcp-oauth-{digest}"


# ============================================================================
# OAuthCredentialStoreError
# ============================================================================


class OAuthCredentialStoreError(Exception):
    """宿主 OAuth 凭据存储失败（对齐 Rust ``OAuthCredentialStoreError``）。

    变体有意不携带后端 payload——防止 vault 错误意外回显凭据、租户标识或
    provider 响应到 SDK 诊断中。
    """

    def __init__(self, kind: str) -> None:
        super().__init__(f"OAuth credential store {kind}")
        self.kind: str = kind
        """错误类别：``"unavailable"`` 或 ``"operationFailed"``。"""

    @classmethod
    def unavailable(cls) -> OAuthCredentialStoreError:
        """凭据存储不可用。"""
        return cls("unavailable")

    @classmethod
    def operation_failed(cls) -> OAuthCredentialStoreError:
        """凭据存储操作失败。"""
        return cls("operationFailed")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OAuthCredentialStoreError):
            return NotImplemented
        return self.kind == other.kind

    def __hash__(self) -> int:
        return hash(self.kind)


# ============================================================================
# OAuthCredentialStore（Protocol，宿主可注入）
# ============================================================================


@runtime_checkable
class OAuthCredentialStore(Protocol):
    """宿主注入的凭据存储协议（对齐 Rust ``OAuthCredentialStore`` trait）。

    Value 是不透明字符串（序列化的 token envelope）；持久化实现**必须静态加密**。
    SDK 默认 ``InMemoryOAuthCredentialStore``，**不主动探测 OS keyring**。

    单个 store 实例可服务一个 ``Computer`` 下全部 OAuth MCP，方法可被并发调用——
    实现须自备同步。Store **不得**记录或暴露 ``value``；配置中的 client secret 和
    private key 仍由 ``SecretValueResolver`` 解析，**不进入**存储 envelope。

    Pending PKCE/CSRF state 与宿主 callback 路由属独立关注点：本协议仅存储授权
    **完成后的凭据**。

    ``save``：**必须**原子替换一个 key——返回错误时，调用前已存在的任何值仍须可读。
    协调器依赖此单 key 保证防止重授权提交失败时覆盖已授权凭据。
    """

    async def load(self, key: OAuthCredentialKey) -> str | None:
        """加载指定 key 的凭据。没有返回 ``None``。"""
        ...

    async def save(self, key: OAuthCredentialKey, value: str) -> None:
        """原子保存（替换）指定 key 的凭据。失败抛 ``OAuthCredentialStoreError``。"""
        ...

    async def delete(self, key: OAuthCredentialKey) -> None:
        """删除指定 key 的凭据。失败抛 ``OAuthCredentialStoreError``。"""
        ...


# ============================================================================
# InMemoryOAuthCredentialStore
# ============================================================================


class InMemoryOAuthCredentialStore:
    """默认进程内凭据存储（对齐 Rust ``InMemoryOAuthCredentialStore``）。

    ``OAuthCredentialKey`` → 不透明字符串，受 ``asyncio.Lock`` 保护。
    进程退出后凭据消失；跨进程恢复须宿主注入持久化 store。
    """

    def __init__(self) -> None:
        self._entries: dict[OAuthCredentialKey, str] = {}
        self._lock = asyncio.Lock()

    async def load(self, key: OAuthCredentialKey) -> str | None:
        async with self._lock:
            return self._entries.get(key)

    async def save(self, key: OAuthCredentialKey, value: str) -> None:
        async with self._lock:
            self._entries[key] = value

    async def delete(self, key: OAuthCredentialKey) -> None:
        async with self._lock:
            self._entries.pop(key, None)


# ============================================================================
# StoredCredentialEnvelope（版本化凭据封装）
# ============================================================================


@dataclass(frozen=True)
class StoredCredentialEnvelope:
    """版本化的凭据信封（对齐 Rust ``StoredCredentialEnvelope``）。

    封装序列化的 token credentials 与 version / mode_fingerprint 校验信息。
    """

    version: int
    """信封版本（当前 = 1）。未知版本 → 按清除处理。"""
    mode_fingerprint: str
    """保存时的 mode fingerprint。加载时不匹配 → 按清除处理。"""
    credentials: str
    """序列化的 token credentials（不透明字符串）。"""

    CURRENT_VERSION: int = 1
    """当前已知版本号。"""

    @classmethod
    def pack(cls, mode_fingerprint: str, credentials: str) -> StoredCredentialEnvelope:
        """打包凭据为当前版本的信封。"""
        return cls(version=cls.CURRENT_VERSION, mode_fingerprint=mode_fingerprint, credentials=credentials)


# ============================================================================
# StoredCredentialIndex（issuer 索引）
# ============================================================================


@dataclass(frozen=True)
class StoredActiveCredential:
    """活跃凭据快照（对齐 Rust ``StoredActiveCredential``）。"""

    issuer: str | None
    credentials: str


@dataclass(frozen=True)
class StoredCredentialIndex:
    """Core 拥有的 issuer 索引（对齐 Rust ``StoredCredentialIndex``）。

    将活跃快照保留在此单一记录中，宿主可原子替换凭据集，同时保留 issuer 列表
    用于 network-free 清理。
    """

    version: int
    """索引版本（当前 = 1）。"""
    issuers: tuple[str | None, ...]
    """已知 issuer 列表（含 ``None``）。"""
    active: StoredActiveCredential | None
    """可选活跃凭据快照。"""

    CURRENT_VERSION: int = 1
    """当前已知版本号。"""


# ============================================================================
# oauth_mode_fingerprint（对齐 Rust）
# ============================================================================


def oauth_mode_fingerprint(options: OAuthOptions) -> str:
    """计算 grant/client 指纹（Auth Code + DCR 单一模式，对齐 Rust #180）。

    指纹 = ``v1:authorization_code:dynamic:scopes-{SHA-256(client_slot + \\0 + sorted_scope + \\0 ...)}``

    Rust #180 移除 Client Credentials 配置入口后，自动路径只用 Auth Code + PKCE + DCR，
    因此不再需要区分 preregistered / CIMD / CC secret / CC private_key_jwt 的 fingerprint。
    ``client_slot`` = ``client_name`` 或其默认 ``"A2C Computer"``，scope 排序去重后逐项 ``\\0`` 分隔。
    """
    client_slot = options.client_name if options.client_name else "A2C Computer"
    scopes = sorted(set(options.scopes))
    digest = hashlib.sha256()
    digest.update(client_slot.encode("utf-8"))
    digest.update(b"\0")
    for scope in scopes:
        digest.update(scope.encode("utf-8"))
        digest.update(b"\0")
    return f"v1:authorization_code:dynamic:scopes-{digest.hexdigest()}"


# ============================================================================
# ScopedCredentialStore（bundle-scoped 隔离层）
# ============================================================================


class ScopedCredentialStore:
    """Bundle-scoped 凭据存储适配层（对齐 Rust ``ScopedCredentialStore``）。

    **有意不实现 ``OAuthCredentialStore`` Protocol**——它是 backend 上的 adapter/lens，
    方法签名不同（``save_credentials`` vs ``save``），封装 bundle_id / resource /
    mode_fingerprint 维度的 key 派生与 issuer-index 管理。

    单个 ``ScopedCredentialStore`` 实例对应一个 (bundle, resource, mode) slot，
    通过 ``OAuthCredentialKey`` 隔离不同 issuer / record_kind 的凭据。
    """

    def __init__(
        self,
        bundle_id: str,
        resource: str,
        mode_fingerprint: str,
        backend: OAuthCredentialStore,
    ) -> None:
        self._bundle_id = bundle_id
        self._resource = resource
        self._mode_fingerprint = mode_fingerprint
        self._backend = backend
        self._issuer: str | None = None
        self._known_issuers: set[str | None] = {None}
        self._lock = asyncio.Lock()

    # -- key construction ----------------------------------------------------

    def _key_for_issuer(self, issuer: str | None) -> OAuthCredentialKey:
        return OAuthCredentialKey(
            bundle_id=self._bundle_id,
            resource=self._resource,
            issuer=issuer,
            grant_fingerprint=self._mode_fingerprint,
            record_kind=OAuthCredentialRecordKind.Credentials,
        )

    def _index_key(self) -> OAuthCredentialKey:
        return OAuthCredentialKey(
            bundle_id=self._bundle_id,
            resource=self._resource,
            issuer=None,
            grant_fingerprint=self._mode_fingerprint,
            record_kind=OAuthCredentialRecordKind.IssuerIndex,
        )

    async def _active_key(self) -> OAuthCredentialKey:
        return self._key_for_issuer(self._issuer)

    # -- envelope load / validate --------------------------------------------

    async def _try_load_credentials(self) -> str | None:
        """加载当前 issuer 的凭据，对齐 Rust ``ScopedCredentialStore::load()``。

        加载路径（两阶段，与 Rust 一致）：
        1. 先查 index.active：issuer 匹配时直接返回，跳过 envelope 解析
        2. fallback 到 per-issuer envelope key（legacy 兼容）

        校验规则：
        - index.active 命中 → 直接返回 active.credentials
        - per-issuer envelope 不存在 → 返回 None
        - JSON 解析失败 → 上抛 ``OAuthCredentialStoreError``（不静默）
        - version / fingerprint 不匹配 → 调 ``backend.delete`` 清除脏数据后返回 None

        ``self._lock`` 保护 index RMW + per-issuer envelope delete 序列不被并发插入写入。
        """
        async with self._lock:
            # Step 1: 先查 index.active（对齐 Rust:1428-1431）
            index = await self._load_or_empty_index()
            if index.active is not None and index.active.issuer == self._issuer:
                return index.active.credentials

            # Step 2: fallback 到 per-issuer envelope key
            key = await self._active_key()
            encoded = await self._backend.load(key)
            if encoded is None:
                return None
            try:
                data = json.loads(encoded)
            except (json.JSONDecodeError, TypeError):
                raise OAuthCredentialStoreError.operation_failed() from None
            version = data.get("version")
            if version != StoredCredentialEnvelope.CURRENT_VERSION:
                await self._backend.delete(key)
                return None
            # 同时兼容 "modeFingerprint"（camelCase，Python / Rust 主流）与 "mode_fingerprint"
            # （snake_case，预留跨 SDK / 旧版本兼容；当前两端序列化均为 camelCase，此分支暂不触发）
            fingerprint = data.get("modeFingerprint", data.get("mode_fingerprint"))
            if fingerprint != self._mode_fingerprint:
                await self._backend.delete(key)
                return None
            credentials = data.get("credentials")
            if credentials is None or not isinstance(credentials, str):
                await self._backend.delete(key)
                return None
            return cast(str, credentials)

    async def try_load_credentials(self) -> str | None:
        """公开入口：加载当前 issuer 的凭据 / Public entry: load credentials for current issuer.

        适配层（如 ``TokenStorageAdapter``）使用此公开方法，而非直接访问
        ``_try_load_credentials`` 私有方法。行为与 :meth:`_try_load_credentials` 一致。
        """
        return await self._try_load_credentials()

    # -- key construction (for cooperating adapters) ------------------------

    def make_key(self, record_kind: OAuthCredentialRecordKind) -> OAuthCredentialKey:
        """为适配层提供由 store 封装的 key 构造，无需访问私有属性。

        ``TokenStorageAdapter`` 等适配层通过此方法派生 client-info / token 等
        不同 ``record_kind`` 的 key，避免跨类访问 ``_bundle_id`` / ``_resource`` /
        ``_issuer`` / ``_mode_fingerprint``。
        """
        return OAuthCredentialKey(
            bundle_id=self._bundle_id,
            resource=self._resource,
            issuer=self._issuer,
            grant_fingerprint=self._mode_fingerprint,
            record_kind=record_kind,
        )

    # -- raw backend access (for cooperating adapters) -----------------------

    async def load_raw(self, key: OAuthCredentialKey) -> str | None:
        """公开的原始 key 读取入口，供适配层（如 ``TokenStorageAdapter``）在不访问
        ``_backend`` 私有属性的前提下读写凭据存储。
        """
        return await self._backend.load(key)

    async def save_raw(self, key: OAuthCredentialKey, value: str) -> None:
        """公开的原始 key 写入入口，供适配层存储非凭据类数据（如 DCR 注册信息）。"""
        await self._backend.save(key, value)

    # -- issuer management ---------------------------------------------------

    async def set_issuer(self, issuer: str | None) -> None:
        """持久化 issuer 到 index 并更新活跃 issuer。

        对齐 Rust ``ScopedCredentialStore::set_issuer()``。
        """
        async with self._lock:
            await self._persist_issuer_index_with(issuer)
            self._known_issuers.add(issuer)
            self._issuer = issuer

    async def adopt_persisted_issuer(self) -> str | None:
        """采纳持久化 index 的 active issuer（restore 路径，#179）。

        新 coordinator 实例（进程重启 / 重建）的 ``_issuer`` 默认 ``None``，而
        ``_try_load_credentials`` 要求 ``index.active.issuer == self._issuer`` 才命中——
        非 ``None`` issuer 的凭据不采纳即永远恢复失败。本方法读取 index 并把
        ``active.issuer``（非 None 时）设为当前 issuer。

        Returns:
            采纳后的当前 issuer（无持久化 active 时为 ``None``）。
        """
        async with self._lock:
            index = await self._load_or_empty_index()
            if index.active is not None and index.active.issuer is not None:
                self._issuer = index.active.issuer
                self._known_issuers.add(index.active.issuer)
            return self._issuer

    async def _persist_issuer_index_with(self, issuer: str | None) -> None:
        """写入 issuer-index 记录。调用方须持有 ``self._lock``。"""
        index = await self._load_or_empty_index()
        issuers: set[str | None] = set(index.issuers)
        issuers.add(issuer)
        sorted_issuers: list[str | None] = sorted(
            issuers,
            key=lambda x: (x if x is not None else ""),
        )
        active = index.active
        new_index = StoredCredentialIndex(
            version=StoredCredentialIndex.CURRENT_VERSION,
            issuers=tuple(sorted_issuers),
            active=active,
        )
        encoded = json.dumps(
            {
                "version": new_index.version,
                "issuers": [i for i in new_index.issuers],
                "active": (
                    {
                        "issuer": new_index.active.issuer,
                        "credentials": new_index.active.credentials,
                    }
                    if new_index.active
                    else None
                ),
            },
            separators=(",", ":"),
        )
        await self._backend.save(self._index_key(), encoded)

    async def _load_or_empty_index(self) -> StoredCredentialIndex:
        encoded = await self._backend.load(self._index_key())
        if encoded is None:
            return StoredCredentialIndex(
                version=StoredCredentialIndex.CURRENT_VERSION,
                issuers=(),
                active=None,
            )
        try:
            data = json.loads(encoded)
        except (json.JSONDecodeError, TypeError):
            # 清理损坏数据避免永久循环
            await self._backend.delete(self._index_key())
            return StoredCredentialIndex(
                version=StoredCredentialIndex.CURRENT_VERSION,
                issuers=(),
                active=None,
            )
        issuers_raw = data.get("issuers", [])
        issuers: tuple[str | None, ...] = tuple(issuers_raw)
        active_raw = data.get("active")
        active: StoredActiveCredential | None = None
        if isinstance(active_raw, dict):
            active = StoredActiveCredential(
                issuer=active_raw.get("issuer"),
                credentials=active_raw.get("credentials", ""),
            )
        return StoredCredentialIndex(
            version=data.get("version", StoredCredentialIndex.CURRENT_VERSION),
            issuers=issuers,
            active=active,
        )

    async def _persisted_issuers(self) -> set[str | None]:
        index = await self._load_or_empty_index()
        return set(index.issuers)

    # -- save ----------------------------------------------------------------

    async def save_credentials(self, credentials: str) -> None:
        """保存当前 issuer 的凭据到 issuer-index（对齐 Rust ``ScopedCredentialStore::save()``）。

        Rust 仅写入 index（含 active 字段），不写独立 per-issuer envelope。
        ``_try_load_credentials`` 优先读 index.active，per-issuer envelope 仅作 legacy fallback。

        ``self._lock`` 保护 index RMW 不被并发写入覆盖。
        """
        async with self._lock:
            index = await self._load_or_empty_index()
            issuers: set[str | None] = set(index.issuers)
            issuers.add(self._issuer)
            sorted_issuers: list[str | None] = sorted(
                issuers,
                key=lambda x: (x if x is not None else ""),
            )
            encoded = json.dumps(
                {
                    "version": StoredCredentialIndex.CURRENT_VERSION,
                    "issuers": [i for i in sorted_issuers],
                    "active": {
                        "issuer": self._issuer,
                        "credentials": credentials,
                    },
                },
                separators=(",", ":"),
            )
            await self._backend.save(self._index_key(), encoded)

    # -- clear ---------------------------------------------------------------

    async def clear(self) -> None:
        """清除此 OAuth slot 下的全部凭据（不波及其他 bundle / mode）。

        遍历所有已知 issuer（运行时内存 + 持久化 issuer-index），逐条 delete，
        最后删除 issuer-index 自身。**不通过 bundle_id 直接清除 store 中全部数据**——
        仅清除当前 ``resource + mode_fingerprint`` 范围的凭据。

        对齐 Rust ``ScopedCredentialStore::clear()``。
        ``self._lock`` 保护 issuer 枚举 + delete 序列不被并发 save 插入新凭据。
        """
        async with self._lock:
            issuers = self._known_issuers.copy()
            issuers.update(await self._persisted_issuers())
            # Delete per-issuer Credentials + ClientRegistration keys in one pass
            for issuer in issuers:
                await self._backend.delete(self._key_for_issuer(issuer))
                await self._backend.delete(
                    OAuthCredentialKey(
                        bundle_id=self._bundle_id,
                        resource=self._resource,
                        issuer=issuer,
                        grant_fingerprint=self._mode_fingerprint,
                        record_kind=OAuthCredentialRecordKind.ClientRegistration,
                    )
                )
            await self._backend.delete(self._index_key())


# ============================================================================
# clear_stored_oauth_credentials（顶层清洁入口）
# ============================================================================


async def clear_stored_oauth_credentials(
    bundle_id: str,
    resource: str,
    options: OAuthOptions,
    credential_store: OAuthCredentialStore,
) -> None:
    """清除指定 bundle / resource / mode 的全部 OAuth 凭据。

    ``clear_oauth`` 仅清本 OAuth slot，不波及其他 bundle / mode。
    对齐 Rust ``clear_stored_oauth_credentials()``。
    """
    store = ScopedCredentialStore(
        bundle_id=bundle_id,
        resource=resource,
        mode_fingerprint=oauth_mode_fingerprint(options),
        backend=credential_store,
    )
    await store.clear()


__all__ = [
    "InMemoryOAuthCredentialStore",
    "OAuthCredentialKey",
    "OAuthCredentialRecordKind",
    "OAuthCredentialStore",
    "OAuthCredentialStoreError",
    "ScopedCredentialStore",
    "StoredActiveCredential",
    "StoredCredentialEnvelope",
    "StoredCredentialIndex",
    "clear_stored_oauth_credentials",
    "oauth_mode_fingerprint",
]
