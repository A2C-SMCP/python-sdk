# -*- coding: utf-8 -*-
# filename: test_secret_store.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``SecretStore`` 单元测试（v0.2.1 #65）/ Secret store unit tests（§9.3）。

不硬依赖真实 keyring（可选依赖）：用 fake backend monkeypatch ``_load_keyring`` / ``keyring_available``。

测试意图 / Test intentions:
- keyring 可用：get/set/delete 往返；
- keyring 不可用（缺失或 fail backend）：get→None、set→False、delete→False（安全降级，绝不落明文）；
- 可用性探测：fail.* backend 判定不可用。
"""

from __future__ import annotations

import pytest

import a2c_smcp.computer.inputs.secret_store as ss
from a2c_smcp.computer.inputs.secret_store import SecretStore, keyring_available


class _FakeKeyring:
    """内存 fake keyring 后端模块 / In-memory fake keyring backend module。"""

    def __init__(self, backend_module: str = "keyring.backends.fake") -> None:
        self._store: dict[tuple[str, str], str] = {}
        self._backend_module = backend_module

    def get_keyring(self):  # type: ignore[no-untyped-def]
        backend = type("Backend", (), {})
        backend.__module__ = self._backend_module
        return backend()

    def get_password(self, service: str, key: str) -> str | None:
        return self._store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self._store[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        if (service, key) not in self._store:
            raise RuntimeError("PasswordDeleteError")
        del self._store[(service, key)]


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    ss._available = None
    yield
    ss._available = None


def test_available_with_real_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "_load_keyring", lambda: _FakeKeyring("keyring.backends.macOS"))
    assert keyring_available(force_recheck=True) is True


def test_unavailable_when_keyring_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "_load_keyring", lambda: None)
    assert keyring_available(force_recheck=True) is False
    store = SecretStore()
    assert store.get("x") is None
    assert store.set("x", "secret") is False
    assert store.delete("x") is False


def test_unavailable_when_fail_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # fail.Keyring → 不可用（容器/CI/无 Secret Service）
    monkeypatch.setattr(ss, "_load_keyring", lambda: _FakeKeyring("keyring.backends.fail"))
    assert keyring_available(force_recheck=True) is False


def test_get_set_delete_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKeyring("keyring.backends.SecretService")
    monkeypatch.setattr(ss, "_load_keyring", lambda: fake)
    store = SecretStore()
    assert store.get("token") is None
    assert store.set("token", "s3cr3t") is True
    assert store.get("token") == "s3cr3t"
    # 落进 fake 的是 (service, key) / stored under (service, key)
    assert fake._store[("a2c-computer", "token")] == "s3cr3t"
    assert store.delete("token") is True
    assert store.get("token") is None
    assert store.delete("token") is False  # 已删 → no-op


def test_probe_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "_load_keyring", lambda: _FakeKeyring("keyring.backends.macOS"))
    assert keyring_available(force_recheck=True) is True
    # 缓存命中：即便切到缺失也仍返回缓存值（除非 force_recheck）
    monkeypatch.setattr(ss, "_load_keyring", lambda: None)
    assert keyring_available() is True
    assert keyring_available(force_recheck=True) is False
