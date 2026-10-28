# -*- coding: utf-8 -*-
# filename: test_path_resolver.py
# @Author  : JQQ
# @Software: PyCharm

"""
path core 九步管线 / 拉取期复核测试 / Path core pipeline & pull-time revalidation tests.

协议依据 / Protocol: a2c-smcp-protocol ``blob-transfer.md`` §生产者三（protocol#65）+
``computer-management/conformance-tests.md`` §4.5。
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from a2c_smcp.computer.blob.handle import (
    HANDLE_KIND_PATH,
    BlobHandleForbiddenError,
    BlobHandleGoneError,
    BlobHandleInvalidError,
    decode_blob_handle,
    encode_path_handle,
)
from a2c_smcp.computer.blob.path_resolver import (
    PathResolveError,
    ProtectedPaths,
    ReadRoot,
    build_protected_paths,
    build_read_roots,
    resolve_for_pull,
    resolve_path,
)

_CONTENT = b"hello path blob\n"


def _root(*paths: Path) -> list[ReadRoot]:
    return build_read_roots([str(p) for p in paths])


def _write(path: Path, content: bytes = _CONTENT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _reason(exc: pytest.ExceptionInfo[PathResolveError]) -> str:
    return exc.value.reason


class TestBuildReadRoots:
    def test_invalid_items_are_dropped(self) -> None:
        roots = build_read_roots(["relative/path", "", "/valid/absolute"])
        assert [r.configured for r in roots] == ["/valid/absolute"]
        assert roots[0].real is not None

    def test_realpath_failure_keeps_config_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(path: str, platform: str | None = None) -> str:
            raise OSError("realpath failed")

        monkeypatch.setattr("a2c_smcp.computer.blob.path_resolver.realpath_nonstrict", boom)
        roots = build_read_roots(["/valid/absolute"])
        assert roots[0].configured == "/valid/absolute"
        assert roots[0].real is None


class TestResolvePath:
    def test_success_metadata_and_handle(self, tmp_path: Path) -> None:
        file = _write(tmp_path / "a.txt")
        result = resolve_path(str(file), _root(tmp_path))
        st = file.stat()

        assert result.canonical_path == os.path.realpath(file)
        assert result.total_size == len(_CONTENT)
        assert result.sha256 == hashlib.sha256(_CONTENT).hexdigest()
        assert result.mime_type == "text/plain"
        assert result.mtime == st.st_mtime_ns // 1_000_000

        kind, payload = decode_blob_handle(result.blob_handle)
        assert kind == HANDLE_KIND_PATH
        assert payload["canonical_path"] == result.canonical_path
        assert payload["total_size"] == result.total_size
        assert payload["mtime"] == result.mtime
        assert payload["sha256"] == result.sha256
        assert payload["mime"] == result.mime_type
        assert payload["dev"] == st.st_dev
        assert payload["ino"] == st.st_ino

    def test_empty_read_roots_forbidden(self, tmp_path: Path) -> None:
        file = _write(tmp_path / "a.txt")
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(file), [])
        assert _reason(exc_info) == "forbidden"

    def test_outside_existing_and_missing_both_forbidden_without_fs_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        roots = _root(root)
        outside = _write(tmp_path / "outside.txt")
        missing_outside = tmp_path / "missing.txt"

        original_lstat = os.lstat
        calls: list[str] = []

        def guarded_lstat(path: str) -> os.stat_result:
            calls.append(path)
            return original_lstat(path)

        monkeypatch.setattr(os, "lstat", guarded_lstat)
        for target in (outside, missing_outside):
            with pytest.raises(PathResolveError) as exc_info:
                resolve_path(str(target), roots, protected=ProtectedPaths())
            assert _reason(exc_info) == "forbidden"
        assert calls == [], "lexical fence must run before any filesystem probe"

    def test_relative_path_invalid(self, tmp_path: Path) -> None:
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path("relative/a.txt", _root(tmp_path))
        assert _reason(exc_info) == "invalid_path"

    def test_inside_missing_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(tmp_path / "missing.txt"), _root(tmp_path))
        assert _reason(exc_info) == "not_found"

    def test_directory_not_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "dir").mkdir()
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(tmp_path / "dir"), _root(tmp_path))
        assert _reason(exc_info) == "not_a_file"

    def test_fifo_not_a_file_and_does_not_block(self, tmp_path: Path) -> None:
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        outcome: dict[str, str] = {}

        def run() -> None:
            try:
                resolve_path(str(fifo), _root(tmp_path))
            except PathResolveError as e:
                outcome["reason"] = e.reason

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(2.0)
        assert not thread.is_alive(), "resolve_path must not block on a FIFO"
        assert outcome["reason"] == "not_a_file"

    def test_symlink_escape_forbidden(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = _write(tmp_path / "outside.txt")
        link = root / "link"
        link.symlink_to(outside)

        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(link), _root(root))
        assert _reason(exc_info) == "forbidden"

    def test_dangling_symlink_outside_forbidden_even_after_target_created(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = tmp_path / "outside" / "missing.txt"
        link = root / "link"
        link.symlink_to(target)

        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(link), _root(root))
        assert _reason(exc_info) == "forbidden"

        _write(target)
        with pytest.raises(PathResolveError) as exc_info_after:
            resolve_path(str(link), _root(root))
        assert _reason(exc_info_after) == "forbidden"

    def test_dotdot_is_lexical_before_symlink(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        deep = root / "sub" / "deep"
        deep.mkdir(parents=True)
        _write(root / "a.txt")
        (root / "link").symlink_to(deep)

        result = resolve_path(str(root / "link" / ".." / "a.txt"), _root(root))
        assert result.canonical_path == os.path.realpath(root / "a.txt")

    def test_root_configured_via_symlink(self, tmp_path: Path) -> None:
        real_root = tmp_path / "real-root"
        real_root.mkdir()
        _write(real_root / "a.txt")
        link_root = tmp_path / "link-root"
        link_root.symlink_to(real_root)

        result = resolve_path(str(link_root / "a.txt"), _root(link_root))
        assert result.canonical_path == os.path.realpath(real_root / "a.txt")

    def test_canonical_roundtrip(self, tmp_path: Path) -> None:
        file = _write(tmp_path / "a.txt")
        first = resolve_path(str(file), _root(tmp_path))
        second = resolve_path(first.canonical_path, _root(tmp_path))
        assert second.canonical_path == first.canonical_path
        assert second.sha256 == first.sha256


class TestFixedRejectionSet:
    def test_skillenv_anywhere_forbidden(self, tmp_path: Path) -> None:
        _write(tmp_path / ".skillenv")
        _write(tmp_path / "sub" / ".skillenv")
        for target in (tmp_path / ".skillenv", tmp_path / "sub" / ".skillenv"):
            with pytest.raises(PathResolveError) as exc_info:
                resolve_path(str(target), _root(tmp_path), protected=ProtectedPaths())
            assert _reason(exc_info) == "forbidden"

    def test_sdk_config_and_state_dirs_forbidden(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        config_dir = root / "xdg-config" / "a2c"
        state_dir = root / "xdg-state" / "a2c"
        settings = _write(config_dir / "settings.json")
        computer_id = _write(config_dir / "computer-id")
        values = _write(state_dir / "input-values.json")
        env = {
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
        }
        protected = build_protected_paths(env=env)

        for target in (settings, computer_id, values):
            with pytest.raises(PathResolveError) as exc_info:
                resolve_path(str(target), _root(root), protected=protected)
            assert _reason(exc_info) == "forbidden"

    def test_part_only_inside_landing_root_forbidden(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        landing = root / "landing"
        part = _write(landing / ".a2c-upload" / "upload.part", b"partial")
        outside_part = _write(root / "other" / "x.part", b"not landing")

        protected = build_protected_paths(landing_root=str(landing))
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(part), _root(root), protected=protected)
        assert _reason(exc_info) == "forbidden"

        # landing root 之外的 .part 不在拒绝集 / ``.part`` outside landing root is allowed.
        result = resolve_path(str(outside_part), _root(root), protected=protected)
        assert result.total_size == len(b"not landing")

    def test_hardlink_to_protected_file_forbidden(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        protected_file = _write(root / "config" / "settings.json")
        alias = root / "alias.txt"
        try:
            os.link(protected_file, alias)
        except OSError:
            pytest.skip("hard links are not supported on this platform/filesystem")

        protected = build_protected_paths(extra_paths=[str(protected_file)])
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(alias), _root(root), protected=protected)
        assert _reason(exc_info) == "forbidden"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
    def test_unsearchable_parent_forbidden(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        locked = root / "locked"
        locked.mkdir(parents=True)
        file = _write(locked / "a.txt")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("running as root: permission bits are not enforced")

        locked.chmod(0o000)
        try:
            with pytest.raises(PathResolveError) as exc_info:
                resolve_path(str(file), _root(root), protected=ProtectedPaths())
            assert _reason(exc_info) == "forbidden"
        finally:
            locked.chmod(0o755)


class TestResolveForPull:
    def _mint(self, tmp_path: Path) -> tuple[str, dict[str, object], Path, list[ReadRoot]]:
        file = _write(tmp_path / "a.txt")
        roots = _root(tmp_path)
        result = resolve_path(str(file), roots)
        _, payload = decode_blob_handle(result.blob_handle)
        return result.blob_handle, payload, file, roots

    def test_pull_success_and_slice(self, tmp_path: Path) -> None:
        _, payload, _, roots = self._mint(tmp_path)
        pull = resolve_for_pull(payload, roots)
        try:
            assert pull.canonical_path == payload["canonical_path"]
            assert pull.total_size == len(_CONTENT)
            assert pull.sha256 == hashlib.sha256(_CONTENT).hexdigest()
            assert pull.slice(0, 5) == _CONTENT[:5]
            assert pull.slice(5, 1000) == _CONTENT[5:]
            assert pull.slice(len(_CONTENT), 10) == b""  # EOF probe
            assert pull.slice(0, 0) == b""
            with pytest.raises(ValueError):
                pull.slice(-1, 1)
            with pytest.raises(ValueError):
                pull.slice(len(_CONTENT) + 1, 1)
        finally:
            pull.close()
            pull.close()  # idempotent

        with pytest.raises(ValueError, match="closed"):
            pull.slice(0, 1)

    def test_pull_after_delete_gone(self, tmp_path: Path) -> None:
        _, payload, file, roots = self._mint(tmp_path)
        file.unlink()
        with pytest.raises(BlobHandleGoneError):
            resolve_for_pull(payload, roots)

    def test_pull_after_rewrite_gone(self, tmp_path: Path) -> None:
        _, payload, file, roots = self._mint(tmp_path)
        file.write_bytes(b"different length content")
        with pytest.raises(BlobHandleGoneError):
            resolve_for_pull(payload, roots)

    def test_pull_after_mtime_change_gone(self, tmp_path: Path) -> None:
        _, payload, file, roots = self._mint(tmp_path)
        st = file.stat()
        file.write_bytes(_CONTENT)  # same size
        os.utime(file, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
        with pytest.raises(BlobHandleGoneError):
            resolve_for_pull(payload, roots)

    def test_pull_after_revoke_forbidden(self, tmp_path: Path) -> None:
        _, payload, _, _ = self._mint(tmp_path)
        with pytest.raises(BlobHandleForbiddenError):
            resolve_for_pull(payload, [])

    def test_pull_outside_root_forbidden(self, tmp_path: Path) -> None:
        _, payload, _, _ = self._mint(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(BlobHandleForbiddenError):
            resolve_for_pull(payload, _root(other))

    def test_pull_directory_gone(self, tmp_path: Path) -> None:
        _, payload, file, roots = self._mint(tmp_path)
        file.unlink()
        file.mkdir()
        with pytest.raises(BlobHandleGoneError):
            resolve_for_pull(payload, roots)

    def test_invalid_payloads_rejected(self, tmp_path: Path) -> None:
        _, payload, _, roots = self._mint(tmp_path)
        missing_ino = {key: value for key, value in payload.items() if key != "ino"}
        bad_cases = [
            {**payload, "canonical_path": str(tmp_path) + "//a.txt"},
            {**payload, "sha256": "ABC"},
            {**payload, "total_size": -1},
            {**payload, "mime": ""},
            missing_ino,  # 只有 dev 没有 ino / dev without ino
        ]
        for bad in bad_cases:
            with pytest.raises(BlobHandleInvalidError):
                resolve_for_pull(bad, roots)


class TestPathHandleCodec:
    def test_round_trip(self) -> None:
        handle = encode_path_handle(
            "/home/u/a.txt",
            total_size=3,
            mtime=1_700_000_000_123,
            sha256="a" * 64,
            mime="text/plain",
            dev=1,
            ino=2,
        )
        kind, payload = decode_blob_handle(handle)
        assert kind == HANDLE_KIND_PATH
        assert payload == {
            "canonical_path": "/home/u/a.txt",
            "total_size": 3,
            "mtime": 1_700_000_000_123,
            "sha256": "a" * 64,
            "mime": "text/plain",
            "dev": 1,
            "ino": 2,
        }

    def test_round_trip_without_file_identity(self) -> None:
        handle = encode_path_handle("/home/u/a.txt", total_size=0, mtime=0, sha256="b" * 64, mime="text/plain")
        _, payload = decode_blob_handle(handle)
        assert "dev" not in payload
        assert "ino" not in payload

    def test_encode_rejects_partial_file_identity(self) -> None:
        with pytest.raises(ValueError):
            encode_path_handle("/home/u/a.txt", total_size=0, mtime=0, sha256="b" * 64, mime="text/plain", dev=1)
