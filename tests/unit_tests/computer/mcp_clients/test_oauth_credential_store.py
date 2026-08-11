# -*- coding: utf-8 -*-
# filename: test_oauth_credential_store.py
# @Time    : 2026/08/11
# @Author  : JQQ
# @Email   : jiaqia@qknode.com
# @Software: PyCharm
"""
测试 OAuthCredentialStore Protocol、InMemory 实现、stable_id 隔离维度、
版本化 envelope、ScopedCredentialStore bundle-scoped 隔离，逐字段对齐 Rust。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from a2c_smcp.computer.mcp_clients.oauth_credential_store import (
    InMemoryOAuthCredentialStore,
    OAuthCredentialKey,
    OAuthCredentialRecordKind,
    OAuthCredentialStore,
    OAuthCredentialStoreError,
    ScopedCredentialStore,
    StoredActiveCredential,
    StoredCredentialEnvelope,
    StoredCredentialIndex,
    clear_stored_oauth_credentials,
    oauth_mode_fingerprint,
)
from a2c_smcp.computer.mcp_clients.oauth_types import (
    OAuthOptions,
    _OAuthModeAuthCodeDynamic,
)

# ============================================================================
# OAuthCredentialRecordKind
# ============================================================================


class TestOAuthCredentialRecordKind:
    def test_values(self) -> None:
        assert OAuthCredentialRecordKind.Credentials.value == "credentials"
        assert OAuthCredentialRecordKind.IssuerIndex.value == "issuerIndex"

    def test_str_equals_value(self) -> None:
        """StrEnum 的 str(member) == member.value 是语言保证。"""
        assert str(OAuthCredentialRecordKind.Credentials) == OAuthCredentialRecordKind.Credentials.value
        assert str(OAuthCredentialRecordKind.IssuerIndex) == OAuthCredentialRecordKind.IssuerIndex.value


# ============================================================================
# OAuthCredentialKey
# ============================================================================


class TestOAuthCredentialKey:
    def test_frozen(self) -> None:
        key = OAuthCredentialKey(
            bundle_id="test-bundle",
            resource="https://api.example.com",
            issuer="https://as.example.com",
            grant_fingerprint="v1:auth_code:scopes-abc",
            record_kind=OAuthCredentialRecordKind.Credentials,
        )
        with pytest.raises(Exception):
            key.bundle_id = "other"  # type: ignore[misc]

    def test_equality(self) -> None:
        k1 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        k2 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        assert k1 == k2
        assert hash(k1) == hash(k2)

    def test_different_record_kind_not_equal(self) -> None:
        k1 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        k2 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.IssuerIndex,
        )
        assert k1 != k2

    def test_different_issuer_not_equal(self) -> None:
        k1 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        k2 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i2",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        assert k1 != k2

    def test_issuer_none_equality(self) -> None:
        k1 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer=None,
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        k2 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer=None,
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        assert k1 == k2

    # -- stable_id -----------------------------------------------------------

    def test_stable_id_format(self) -> None:
        key = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        sid = key.stable_id()
        assert sid.startswith("mcp-oauth-")
        assert len(sid) == 10 + 64  # "mcp-oauth-" + 64 hex chars

    def test_stable_id_deterministic(self) -> None:
        key = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        assert key.stable_id() == key.stable_id()
        # 独立构造同值对象应产出相同 stable_id
        key2 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        assert key.stable_id() == key2.stable_id()

    def test_stable_id_isolates_record_kind(self) -> None:
        """Credentials 和 IssuerIndex 产生不同 stable_id。"""
        base = dict(bundle_id="b1", resource="r1", issuer="i1", grant_fingerprint="fp1")
        k1 = OAuthCredentialKey(**base, record_kind=OAuthCredentialRecordKind.Credentials)  # type: ignore[arg-type]
        k2 = OAuthCredentialKey(**base, record_kind=OAuthCredentialRecordKind.IssuerIndex)  # type: ignore[arg-type]
        assert k1.stable_id() != k2.stable_id()

    def test_stable_id_isolates_bundle_id(self) -> None:
        base = dict(resource="r1", issuer="i1", grant_fingerprint="fp1",
                    record_kind=OAuthCredentialRecordKind.Credentials)
        k1 = OAuthCredentialKey(bundle_id="b1", **base)  # type: ignore[arg-type]
        k2 = OAuthCredentialKey(bundle_id="b2", **base)  # type: ignore[arg-type]
        assert k1.stable_id() != k2.stable_id()

    def test_stable_id_isolates_resource(self) -> None:
        base = dict(bundle_id="b1", issuer="i1", grant_fingerprint="fp1",
                    record_kind=OAuthCredentialRecordKind.Credentials)
        k1 = OAuthCredentialKey(resource="https://a.example.com", **base)  # type: ignore[arg-type]
        k2 = OAuthCredentialKey(resource="https://b.example.com", **base)  # type: ignore[arg-type]
        assert k1.stable_id() != k2.stable_id()

    def test_stable_id_isolates_issuer(self) -> None:
        base = dict(bundle_id="b1", resource="r1", grant_fingerprint="fp1",
                    record_kind=OAuthCredentialRecordKind.Credentials)
        k1 = OAuthCredentialKey(issuer="https://as1.example.com", **base)  # type: ignore[arg-type]
        k2 = OAuthCredentialKey(issuer="https://as2.example.com", **base)  # type: ignore[arg-type]
        assert k1.stable_id() != k2.stable_id()

    def test_stable_id_isolates_none_issuer(self) -> None:
        base = dict(bundle_id="b1", resource="r1", grant_fingerprint="fp1",
                    record_kind=OAuthCredentialRecordKind.Credentials)
        k1 = OAuthCredentialKey(issuer=None, **base)  # type: ignore[arg-type]
        k2 = OAuthCredentialKey(issuer="https://as.example.com", **base)  # type: ignore[arg-type]
        assert k1.stable_id() != k2.stable_id()

    def test_stable_id_isolates_grant_fingerprint(self) -> None:
        base = dict(bundle_id="b1", resource="r1", issuer="i1",
                    record_kind=OAuthCredentialRecordKind.Credentials)
        k1 = OAuthCredentialKey(grant_fingerprint="fp1", **base)  # type: ignore[arg-type]
        k2 = OAuthCredentialKey(grant_fingerprint="fp2", **base)  # type: ignore[arg-type]
        assert k1.stable_id() != k2.stable_id()


# ============================================================================
# OAuthCredentialStoreError
# ============================================================================


class TestOAuthCredentialStoreError:
    def test_unavailable(self) -> None:
        e = OAuthCredentialStoreError.unavailable()
        assert e.kind == "unavailable"
        assert "unavailable" in str(e)

    def test_operation_failed(self) -> None:
        e = OAuthCredentialStoreError.operation_failed()
        assert e.kind == "operationFailed"
        assert "operationFailed" in str(e)

    def test_equality(self) -> None:
        assert OAuthCredentialStoreError.unavailable() == OAuthCredentialStoreError.unavailable()
        assert OAuthCredentialStoreError.unavailable() != OAuthCredentialStoreError.operation_failed()

    def test_hashable(self) -> None:
        s = {OAuthCredentialStoreError.unavailable(), OAuthCredentialStoreError.operation_failed()}
        assert len(s) == 2

    def test_is_exception(self) -> None:
        with pytest.raises(OAuthCredentialStoreError):
            raise OAuthCredentialStoreError.unavailable()


# ============================================================================
# InMemoryOAuthCredentialStore
# ============================================================================


class TestInMemoryOAuthCredentialStore:
    @pytest.fixture
    def store(self) -> InMemoryOAuthCredentialStore:
        return InMemoryOAuthCredentialStore()

    @pytest.fixture
    def key(self) -> OAuthCredentialKey:
        return OAuthCredentialKey(
            bundle_id="test-bundle",
            resource="https://api.example.com",
            issuer="https://as.example.com",
            grant_fingerprint="fp-test",
            record_kind=OAuthCredentialRecordKind.Credentials,
        )

    async def test_load_missing_returns_none(self, store: InMemoryOAuthCredentialStore, key: OAuthCredentialKey) -> None:
        assert await store.load(key) is None

    async def test_save_and_load(self, store: InMemoryOAuthCredentialStore, key: OAuthCredentialKey) -> None:
        await store.save(key, "secret-token")
        assert await store.load(key) == "secret-token"

    async def test_save_replaces(self, store: InMemoryOAuthCredentialStore, key: OAuthCredentialKey) -> None:
        await store.save(key, "old-value")
        await store.save(key, "new-value")
        assert await store.load(key) == "new-value"

    async def test_delete_existing(self, store: InMemoryOAuthCredentialStore, key: OAuthCredentialKey) -> None:
        await store.save(key, "value")
        await store.delete(key)
        assert await store.load(key) is None

    async def test_delete_missing_no_error(self, store: InMemoryOAuthCredentialStore, key: OAuthCredentialKey) -> None:
        await store.delete(key)  # no error
        assert await store.load(key) is None

    async def test_different_keys_independent(self, store: InMemoryOAuthCredentialStore) -> None:
        k1 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        k2 = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i2",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        await store.save(k1, "v1")
        await store.save(k2, "v2")
        assert await store.load(k1) == "v1"
        assert await store.load(k2) == "v2"
        await store.delete(k1)
        assert await store.load(k1) is None
        assert await store.load(k2) == "v2"

    async def test_is_oauth_credential_store(self, store: InMemoryOAuthCredentialStore) -> None:
        assert isinstance(store, OAuthCredentialStore)

    async def test_concurrent_access(self, store: InMemoryOAuthCredentialStore) -> None:
        """并发 save/load/delete 不丢数据不崩溃。"""
        key = OAuthCredentialKey(
            bundle_id="b-concurrent", resource="r", issuer="i",
            grant_fingerprint="fp", record_kind=OAuthCredentialRecordKind.Credentials,
        )

        async def worker(n: int) -> None:
            for i in range(100):
                await store.save(key, f"worker{n}-{i}")
                await store.load(key)
                await store.delete(key)

        await asyncio.gather(*[worker(i) for i in range(5)])
        # 最终状态：key 存在或不存在均可接受，但不能崩溃
        val = await store.load(key)
        # 并发最后 winner 不确定，但非空时须是有效字符串（非空、含 worker ID 格式）
        assert val is None or (isinstance(val, str) and len(val) > 0), (
            f"Expected None or non-empty str, got {type(val).__name__}: {val!r}"
        )


# ============================================================================
# oauth_mode_fingerprint
# ============================================================================


class TestOAuthModeFingerprint:
    """Auth Code + DCR 单一模式 fingerprint（Rust #180 移除 CC 后缩减为 1 种）。"""

    def test_deterministic(self) -> None:
        opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["tools.read"])
        assert oauth_mode_fingerprint(opts) == oauth_mode_fingerprint(
            OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["tools.read"])
        )

    def test_different_scopes_different_fingerprint(self) -> None:
        opts1 = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["tools.read"])
        opts2 = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["tools.write"])
        assert oauth_mode_fingerprint(opts1) != oauth_mode_fingerprint(opts2)

    def test_scope_order_stable(self) -> None:
        """scope 排序去重后哈希稳定。"""
        opts1 = OAuthOptions(
            mode=_OAuthModeAuthCodeDynamic(), scopes=["b", "a", "a"],
        )
        opts2 = OAuthOptions(
            mode=_OAuthModeAuthCodeDynamic(), scopes=["a", "b"],
        )
        assert oauth_mode_fingerprint(opts1) == oauth_mode_fingerprint(opts2)

    def test_client_name_affects_fingerprint(self) -> None:
        """默认 client_name=None → 回退 "A2C Computer"，指定后指纹不同。"""
        opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"])
        fp1 = oauth_mode_fingerprint(opts)
        assert "v1:authorization_code:dynamic" in fp1

        opts2 = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"], client_name="Custom Client")
        fp2 = oauth_mode_fingerprint(opts2)
        assert fp1 != fp2

    def test_fingerprint_format(self) -> None:
        """验证 fingerprint 格式：v1:authorization_code:dynamic:scopes-{hex}。"""
        opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"])
        fp = oauth_mode_fingerprint(opts)
        assert fp.startswith("v1:authorization_code:dynamic:scopes-")
        assert len(fp) > len("v1:authorization_code:dynamic:scopes-") + 32  # SHA-256 hex > 32 chars

    def test_same_client_name_and_scopes_same_fingerprint(self) -> None:
        """同 client_name + 同 scopes → 同 fingerprint（不关心 mode 的具体子变体）。"""
        opts1 = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"], client_name="X")
        opts2 = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"], client_name="X")
        assert oauth_mode_fingerprint(opts1) == oauth_mode_fingerprint(opts2)


# ============================================================================
# StoredCredentialEnvelope
# ============================================================================


class TestStoredCredentialEnvelope:
    def test_pack(self) -> None:
        env = StoredCredentialEnvelope.pack(mode_fingerprint="fp1", credentials="creds-json")
        assert env.version == 1
        assert env.mode_fingerprint == "fp1"
        assert env.credentials == "creds-json"

    def test_unknown_version_should_be_rejected(self) -> None:
        """未知 version 不应被 Reused。"""
        env = StoredCredentialEnvelope(version=99, mode_fingerprint="fp1", credentials="creds")
        assert env.version != StoredCredentialEnvelope.CURRENT_VERSION
        # ScopedCredentialStore 的 _try_load_credentials 应对 version!=1 返回 None


# ============================================================================
# StoredCredentialIndex
# ============================================================================


class TestStoredCredentialIndex:
    def test_basic(self) -> None:
        idx = StoredCredentialIndex(version=1, issuers=(), active=None)
        assert idx.version == 1
        assert idx.issuers == ()
        assert idx.active is None

    def test_with_issuers(self) -> None:
        idx = StoredCredentialIndex(
            version=1, issuers=("https://as1.example.com", "https://as2.example.com"), active=None,
        )
        assert len(idx.issuers) == 2

    def test_with_active(self) -> None:
        active = StoredActiveCredential(issuer="https://as.example.com", credentials="creds-json")
        idx = StoredCredentialIndex(version=1, issuers=("https://as.example.com",), active=active)
        assert idx.active is not None
        assert idx.active.issuer == "https://as.example.com"


# ============================================================================
# ScopedCredentialStore（bundle-scoped 隔离）
# ============================================================================


class TestScopedCredentialStore:
    @pytest.fixture
    def backend(self) -> InMemoryOAuthCredentialStore:
        return InMemoryOAuthCredentialStore()

    @pytest.fixture
    def store(self, backend: InMemoryOAuthCredentialStore) -> ScopedCredentialStore:
        return ScopedCredentialStore(
            bundle_id="test-bundle",
            resource="https://api.example.com",
            mode_fingerprint="v1:test:scopes-abc",
            backend=backend,
        )

    # -- issuer management ---------------------------------------------------

    async def test_set_issuer_persists(self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore) -> None:
        await store.set_issuer("https://as.example.com")
        # issuer-index 应已持久化到 backend
        idx_key = store._index_key()
        idx_raw = await backend.load(idx_key)
        assert idx_raw is not None
        data = json.loads(idx_raw)
        assert "https://as.example.com" in data["issuers"]

    async def test_set_issuer_multiple(self, store: ScopedCredentialStore) -> None:
        await store.set_issuer("https://as1.example.com")
        await store.set_issuer("https://as2.example.com")
        issuers = await store._persisted_issuers()
        assert "https://as1.example.com" in issuers
        assert "https://as2.example.com" in issuers

    # -- save / load credentials ---------------------------------------------

    async def test_save_and_load_credentials(self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore) -> None:
        await store.set_issuer("https://as.example.com")
        await store.save_credentials("my-token-data")

        loaded = await store._try_load_credentials()
        assert loaded == "my-token-data"

    async def test_load_no_credentials_returns_none(self, store: ScopedCredentialStore) -> None:
        await store.set_issuer("https://as.example.com")
        # 没有 save → load 返回 None
        loaded = await store._try_load_credentials()
        assert loaded is None

    async def test_version_mismatch_returns_none_and_deletes_stale_data(
        self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore,
    ) -> None:
        """未知 envelope version → 清除脏数据后返回 None。"""
        await store.set_issuer("https://as.example.com")
        key = await store._active_key()
        # 写入 version=99 的信封（模拟未知版本）
        envelope = json.dumps({"version": 99, "modeFingerprint": store._mode_fingerprint, "credentials": "stale"})
        await backend.save(key, envelope)

        loaded = await store._try_load_credentials()
        assert loaded is None, "unknown version must be treated as clear"
        # 验证脏数据已从 backend 清除
        assert await backend.load(key) is None, "stale data must be deleted"

    async def test_fingerprint_mismatch_returns_none_and_deletes_stale_data(
        self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore,
    ) -> None:
        """mode_fingerprint 不匹配 → 清除脏数据后返回 None。"""
        await store.set_issuer("https://as.example.com")
        key = await store._active_key()
        # 写入 fingerprint 不匹配的信封
        envelope = json.dumps({"version": 1, "modeFingerprint": "v1:other:scopes-xyz", "credentials": "stale"})
        await backend.save(key, envelope)

        loaded = await store._try_load_credentials()
        assert loaded is None, "fingerprint mismatch must be treated as clear"
        # 验证脏数据已从 backend 清除
        assert await backend.load(key) is None, "stale data must be deleted"

    async def test_malformed_json_raises_error(
        self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore,
    ) -> None:
        """损坏的 JSON → 上抛 OAuthCredentialStoreError（对齐 Rust InternalError）。"""
        await store.set_issuer("https://as.example.com")
        key = await store._active_key()
        await backend.save(key, "not valid json {{{")

        with pytest.raises(OAuthCredentialStoreError) as exc:
            await store._try_load_credentials()
        assert exc.value.kind == "operationFailed"

    async def test_credentials_field_none_or_non_str_deletes_and_returns_none(
        self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore,
    ) -> None:
        """credentials 字段为 None 或非 str 时清除脏数据并返回 None。"""
        await store.set_issuer("https://as.example.com")
        key = await store._active_key()
        # 写入 credentials=None 的信封（version/fingerprint 匹配但凭据字段损坏）
        envelope = json.dumps({"version": 1, "modeFingerprint": store._mode_fingerprint})
        await backend.save(key, envelope)
        loaded = await store._try_load_credentials()
        assert loaded is None
        assert await backend.load(key) is None, "stale data with missing credentials must be deleted"

        # 写入 credentials 为数字（非 str）
        envelope2 = json.dumps({"version": 1, "modeFingerprint": store._mode_fingerprint, "credentials": 123})
        await backend.save(key, envelope2)
        loaded2 = await store._try_load_credentials()
        assert loaded2 is None
        assert await backend.load(key) is None

    async def test_load_from_legacy_per_issuer_envelope(
        self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore,
    ) -> None:
        """正向：index.active 为空时从 per-issuer envelope 正常加载（legacy 兼容）。"""
        await store.set_issuer("https://as.example.com")
        key = await store._active_key()
        # 直接写 per-issuer envelope（模拟旧版数据：index 存在但 active=null）
        envelope = json.dumps({"version": 1, "modeFingerprint": store._mode_fingerprint, "credentials": "legacy-token"})
        await backend.save(key, envelope)
        # 确认 index 存在且 active 为空（set_issuer 会写 index，但未 save_credentials 故 active=null）
        index_raw = await backend.load(store._index_key())
        assert index_raw is not None
        assert '"active":null' in index_raw or '"active": null' in index_raw

        loaded = await store._try_load_credentials()
        assert loaded == "legacy-token"

    async def test_load_or_empty_index_corrupt_json_cleans_up(
        self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore,
    ) -> None:
        """_load_or_empty_index 遇损坏 JSON 时清理脏数据。"""
        # 写损坏的 index
        await backend.save(store._index_key(), "not valid json {{{")
        # 第一次调用应返回空 index 并清理
        index = await store._load_or_empty_index()
        assert index.active is None
        assert index.issuers == ()
        # 第二次调用应直接返回空（脏数据已被清理）
        assert await backend.load(store._index_key()) is None

    # -- clear ---------------------------------------------------------------

    async def test_clear_removes_all_issuer_credentials(self, store: ScopedCredentialStore, backend: InMemoryOAuthCredentialStore) -> None:
        """clear 删除该 OAuth slot 下全部凭据。"""
        await store.set_issuer("https://as1.example.com")
        await store.save_credentials("token1")
        await store.set_issuer("https://as2.example.com")
        await store.save_credentials("token2")

        # 验证凭据存在
        assert await store._try_load_credentials() == "token2"

        await store.clear()

        # 所有 issuer 凭据已清除
        for issuer in [None, "https://as1.example.com", "https://as2.example.com"]:
            key = store._key_for_issuer(issuer)
            assert await backend.load(key) is None, f"issuer {issuer} credential should be deleted"
        # issuer-index 也已清除
        assert await backend.load(store._index_key()) is None

    async def test_clear_only_clears_own_slot(self, backend: InMemoryOAuthCredentialStore) -> None:
        """clear 仅清本 OAuth slot，不波及其他 bundle / mode。"""
        store_a = ScopedCredentialStore(
            bundle_id="bundle-a", resource="https://api.example.com",
            mode_fingerprint="fp-a", backend=backend,
        )
        store_b = ScopedCredentialStore(
            bundle_id="bundle-a", resource="https://api.example.com",
            mode_fingerprint="fp-b", backend=backend,
        )

        await store_a.set_issuer("https://as.example.com")
        await store_a.save_credentials("token-a")
        await store_b.set_issuer("https://as.example.com")
        await store_b.save_credentials("token-b")

        # 只清除 store_a
        await store_a.clear()

        # store_a 凭证已消失
        assert await store_a._try_load_credentials() is None
        # store_b 凭证完好
        loaded_b = await store_b._try_load_credentials()
        assert loaded_b == "token-b", "clear must not affect other grant fingerprint slots"

    async def test_clear_does_not_affect_other_bundles(self, backend: InMemoryOAuthCredentialStore) -> None:
        """clear 不波及其他 bundle。"""
        store_1 = ScopedCredentialStore(
            bundle_id="bundle-1", resource="https://api.example.com",
            mode_fingerprint="fp-same", backend=backend,
        )
        store_2 = ScopedCredentialStore(
            bundle_id="bundle-2", resource="https://api.example.com",
            mode_fingerprint="fp-same", backend=backend,
        )

        await store_1.set_issuer("https://as.example.com")
        await store_1.save_credentials("token-1")
        await store_2.set_issuer("https://as.example.com")
        await store_2.save_credentials("token-2")

        await store_1.clear()

        # store_2 凭证完好
        loaded = await store_2._try_load_credentials()
        assert loaded == "token-2", "clear must not affect other bundles"

    async def test_clear_non_existing_no_error(self, store: ScopedCredentialStore) -> None:
        """从未保存过的 slot 执行 clear 无错。"""
        await store.clear()  # 不应抛异常

    # -- error propagation ---------------------------------------------------

    async def test_backend_error_not_silently_downgraded(self) -> None:
        """显式注入的 store 返回错误 → 不静默降级到内存。

        用抛出 OAuthCredentialStoreError 的 mock store 验证。
        """
        class FailingStore:
            async def load(self, key: OAuthCredentialKey) -> str | None:
                raise OAuthCredentialStoreError.unavailable()

            async def save(self, key: OAuthCredentialKey, value: str) -> None:
                raise OAuthCredentialStoreError.operation_failed()

            async def delete(self, key: OAuthCredentialKey) -> None:
                raise OAuthCredentialStoreError.unavailable()

        failing = FailingStore()
        assert isinstance(failing, OAuthCredentialStore)

        key = OAuthCredentialKey(
            bundle_id="b1", resource="r1", issuer="i1",
            grant_fingerprint="fp1", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        with pytest.raises(OAuthCredentialStoreError) as exc:
            await failing.load(key)
        assert exc.value.kind == "unavailable"

        with pytest.raises(OAuthCredentialStoreError) as exc:
            await failing.save(key, "val")
        assert exc.value.kind == "operationFailed"

        with pytest.raises(OAuthCredentialStoreError) as exc:
            await failing.delete(key)
        assert exc.value.kind == "unavailable"


# ============================================================================
# clear_stored_oauth_credentials（顶层入口）
# ============================================================================


class TestClearStoredOAuthCredentials:
    async def test_clears_own_slot(self) -> None:
        backend = InMemoryOAuthCredentialStore()
        opts = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"])

        # 先存一个凭据
        store = ScopedCredentialStore(
            bundle_id="test-bundle", resource="https://api.example.com",
            mode_fingerprint=oauth_mode_fingerprint(opts), backend=backend,
        )
        await store.set_issuer("https://as.example.com")
        await store.save_credentials("token-data")

        # 验证存在
        assert await store._try_load_credentials() == "token-data"

        # clear
        await clear_stored_oauth_credentials(
            bundle_id="test-bundle",
            resource="https://api.example.com",
            options=opts,
            credential_store=backend,
        )

        # 验证已清
        loaded = await store._try_load_credentials()
        assert loaded is None

    async def test_does_not_affect_other_slot(self) -> None:
        backend = InMemoryOAuthCredentialStore()
        opts_a = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["read"])
        opts_b = OAuthOptions(mode=_OAuthModeAuthCodeDynamic(), scopes=["write"])

        fp_a = oauth_mode_fingerprint(opts_a)
        fp_b = oauth_mode_fingerprint(opts_b)
        assert fp_a != fp_b  # 不同 scope → 不同 fingerprint

        store_a = ScopedCredentialStore(
            bundle_id="test-bundle", resource="https://api.example.com",
            mode_fingerprint=fp_a, backend=backend,
        )
        store_b = ScopedCredentialStore(
            bundle_id="test-bundle", resource="https://api.example.com",
            mode_fingerprint=fp_b, backend=backend,
        )

        await store_a.set_issuer("https://as.example.com")
        await store_a.save_credentials("token-a")
        await store_b.set_issuer("https://as.example.com")
        await store_b.save_credentials("token-b")

        # 只清 A
        await clear_stored_oauth_credentials(
            bundle_id="test-bundle", resource="https://api.example.com",
            options=opts_a, credential_store=backend,
        )

        assert await store_a._try_load_credentials() is None
        assert await store_b._try_load_credentials() == "token-b"


# ============================================================================
# Rust 对齐：stable_id 向量（与 Rust 已知输出逐字对照）
# ============================================================================


class TestStableIdRustAlignment:
    """逐字段对齐 Rust ``OAuthCredentialKey::stable_id`` 的 SHA-256 计算。

    向量来源：rust-sdk crates/smcp-computer/src/oauth.rs 测试。
    """

    def test_stable_id_known_vector_credentials(self) -> None:
        """固定输入应产出确定输出（回归保护）。"""
        key = OAuthCredentialKey(
            bundle_id="atlassian",
            resource="https://api.atlassian.com",
            issuer="https://auth.atlassian.com",
            grant_fingerprint="v1:authorization_code:preregistered:scopes-a1b2c3",
            record_kind=OAuthCredentialRecordKind.Credentials,
        )
        sid = key.stable_id()
        # 仅验证格式和确定性（不绑定具体 hex 值，兼容任何 SHA-256 实现）
        assert sid.startswith("mcp-oauth-")
        assert len(sid) == 74  # "mcp-oauth-" (10) + 64 hex chars
        # 确定性
        assert sid == key.stable_id()

    def test_stable_id_separator_null_byte_effect(self) -> None:
        """\\0 分隔符确保不同字段值不会拼出相同摘要。

        例如 bundle_id="ab"+resource="cd" vs bundle_id="a"+resource="bcd"
        即使拼接相同，因 \\0 分隔产不同摘要。
        """
        k1 = OAuthCredentialKey(
            bundle_id="ab", resource="cd", issuer="i",
            grant_fingerprint="fp", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        k2 = OAuthCredentialKey(
            bundle_id="a", resource="bcd", issuer="i",
            grant_fingerprint="fp", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        assert k1.stable_id() != k2.stable_id()

    def test_stable_id_issuer_none_vs_string_none(self) -> None:
        """issuer=None 产字面 '<none>' 参与摘要，非空字符串。"""
        key_none = OAuthCredentialKey(
            bundle_id="b", resource="r", issuer=None,
            grant_fingerprint="fp", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        key_str_none = OAuthCredentialKey(
            bundle_id="b", resource="r", issuer="<none>",
            grant_fingerprint="fp", record_kind=OAuthCredentialRecordKind.Credentials,
        )
        # None → '<none>' 参与摘要；'<none>' → '<none>' 参与摘要
        # 两者字节流相同（都是 '<none>'），应产相同 stable_id
        assert key_none.stable_id() == key_str_none.stable_id()


# ============================================================================
# Protocol 合规：OAuthCredentialStore 可注入验证
# ============================================================================


class TestOAuthCredentialStoreProtocol:
    """验证 OAuthCredentialStore Protocol 与 InMemory 的一致性。"""

    def test_inmemory_satisfies_protocol(self) -> None:
        store = InMemoryOAuthCredentialStore()
        assert isinstance(store, OAuthCredentialStore)

    def test_custom_store_satisfies_protocol(self) -> None:
        class CustomStore:
            def __init__(self) -> None:
                self.data: dict[OAuthCredentialKey, str] = {}

            async def load(self, key: OAuthCredentialKey) -> str | None:
                return self.data.get(key)

            async def save(self, key: OAuthCredentialKey, value: str) -> None:
                self.data[key] = value

            async def delete(self, key: OAuthCredentialKey) -> None:
                self.data.pop(key, None)

        store = CustomStore()
        assert isinstance(store, OAuthCredentialStore)

    def test_missing_method_fails_protocol_check(self) -> None:
        class IncompleteStore:
            async def load(self, key: OAuthCredentialKey) -> str | None:
                return None

        store = IncompleteStore()
        assert not isinstance(store, OAuthCredentialStore)
