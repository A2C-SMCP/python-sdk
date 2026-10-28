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
from typing import Any

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
    _is_protected,
    _within_any,
    _within_any_real,
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


def _manual_path_payload(path: Path) -> dict[str, Any]:
    """按当前文件状态手工铸造 path 句柄 payload（供拉取期拒绝集 / 竞争测试）."""
    st = path.stat()
    return {
        "canonical_path": os.path.realpath(path),
        "total_size": st.st_size,
        "mtime": st.st_mtime_ns // 1_000_000,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mime": "text/plain",
        "dev": st.st_dev,
        "ino": st.st_ino,
    }


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

    def test_state_dir_any_file_forbidden(self, tmp_path: Path) -> None:
        """状态目录整目录保护：非 input-values.json 的文件也 forbidden."""
        root = tmp_path / "root"
        state_dir = root / "xdg-state" / "a2c"
        other = _write(state_dir / "other-state.bin", b"state")
        env = {
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
        }
        protected = build_protected_paths(env=env)

        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(other), _root(root), protected=protected)
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


class TestWindowsRejectionSetCasefold:
    """Windows 文件系统大小写不敏感：拒绝集 casefold，readRoots 围栏仍区分大小写."""

    def test_skillenv_case_insensitive_on_windows(self) -> None:
        assert _is_protected("/C:/root/.SKILLENV", ProtectedPaths(), "windows")
        assert not _is_protected("/C:/root/.SKILLENV", ProtectedPaths(), "posix")

    def test_protected_prefix_case_insensitive_on_windows(self) -> None:
        protected = ProtectedPaths(paths=("/C:/secret",))
        assert _is_protected("/C:/SECRET/file.txt", protected, "windows")
        assert not _is_protected("/C:/SECRET/file.txt", protected, "posix")

    def test_landing_part_case_insensitive_on_windows(self) -> None:
        protected = ProtectedPaths(landing_root="/C:/landing")
        assert _is_protected("/C:/LANDING/x.PART", protected, "windows")
        assert not _is_protected("/C:/LANDING/x.PART", protected, "posix")

    def test_read_roots_fence_stays_case_sensitive(self) -> None:
        """readRoots 围栏仍按协议区分大小写（casefold 只用于拒绝集）."""
        roots = [ReadRoot(configured="/C:/Data", real="/C:/Data")]
        assert _within_any("/C:/Data/f.txt", roots)
        assert not _within_any("/C:/data/f.txt", roots)
        assert _within_any_real("/C:/Data/f.txt", roots)
        assert not _within_any_real("/C:/data/f.txt", roots)


class TestRaceAndFinalPathGuards:
    def test_identity_changed_between_lstat_and_open_forbidden(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        file = _write(root / "a.txt")
        roots = _root(root)  # 先构建根，避免受 lstat 补丁影响 / build roots before patching
        canonical_native = os.path.realpath(file)

        original_lstat = os.lstat

        def fake_lstat(path: str) -> os.stat_result:
            st = original_lstat(path)
            if path == canonical_native:
                # st_ino 为 stat_result 的第 2 个字段 / st_ino is the 2nd field
                return os.stat_result(
                    (
                        st.st_mode,
                        st.st_ino + 1,
                        st.st_dev,
                        st.st_nlink,
                        st.st_uid,
                        st.st_gid,
                        st.st_size,
                        st.st_atime,
                        st.st_mtime,
                        st.st_ctime,
                    ),
                )
            return st

        monkeypatch.setattr(os, "lstat", fake_lstat)
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(file), roots, protected=ProtectedPaths())
        assert _reason(exc_info) == "forbidden"
        assert "identity" in str(exc_info.value)  # 断言命中身份比对分支 / assert the identity branch

    def test_final_path_outside_forbidden(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        file = _write(root / "a.txt")
        outside = os.path.realpath(tmp_path / "outside.txt")
        monkeypatch.setattr(
            "a2c_smcp.computer.blob.path_resolver._fd_final_path",
            lambda fd, platform: outside,
        )
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(file), _root(root), protected=ProtectedPaths())
        assert _reason(exc_info) == "forbidden"
        assert "outside readRoots" in str(exc_info.value)  # 命中最终路径围栏 / final-path fence

    def test_final_path_protected_forbidden(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        file = _write(root / "a.txt")
        secret = _write(root / "secret.txt")
        protected = ProtectedPaths(paths=(os.path.realpath(secret),))
        monkeypatch.setattr(
            "a2c_smcp.computer.blob.path_resolver._fd_final_path",
            lambda fd, platform: os.path.realpath(secret),
        )
        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(file), _root(root), protected=protected)
        assert _reason(exc_info) == "forbidden"
        assert "rejection set" in str(exc_info.value)  # 命中最终路径拒绝集 / final-path rejection

    def test_final_path_none_allows(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        file = _write(root / "a.txt")
        monkeypatch.setattr(
            "a2c_smcp.computer.blob.path_resolver._fd_final_path",
            lambda fd, platform: None,
        )
        result = resolve_path(str(file), _root(root), protected=ProtectedPaths())
        assert result.total_size == len(_CONTENT)

    def test_pull_final_path_outside_forbidden(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        file = _write(root / "a.txt")
        roots = _root(root)
        minted = resolve_path(str(file), roots, protected=ProtectedPaths())
        _, payload = decode_blob_handle(minted.blob_handle)
        outside = os.path.realpath(tmp_path / "outside.txt")
        monkeypatch.setattr(
            "a2c_smcp.computer.blob.path_resolver._fd_final_path",
            lambda fd, platform: outside,
        )
        with pytest.raises(BlobHandleForbiddenError) as exc_info:
            resolve_for_pull(payload, roots, protected=ProtectedPaths())
        assert "outside readRoots" in str(exc_info.value)


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

    def test_pull_skillenv_forbidden(self, tmp_path: Path) -> None:
        """拉取期固定拒绝集：手工句柄指向 .skillenv → 4018 forbidden."""
        root = tmp_path / "root"
        root.mkdir()
        target = _write(root / "sub" / ".skillenv")
        with pytest.raises(BlobHandleForbiddenError):
            resolve_for_pull(_manual_path_payload(target), _root(root), protected=ProtectedPaths())

    def test_pull_protected_config_file_forbidden(self, tmp_path: Path) -> None:
        """拉取期固定拒绝集：手工句柄指向 SDK settings → 4018 forbidden."""
        root = tmp_path / "root"
        settings = _write(root / "xdg-config" / "a2c" / "settings.json")
        env = {
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
        }
        protected = build_protected_paths(env=env)
        with pytest.raises(BlobHandleForbiddenError):
            resolve_for_pull(_manual_path_payload(settings), _root(root), protected=protected)

    def test_pull_identity_mismatch_same_size_same_mtime_gone(self, tmp_path: Path) -> None:
        """句柄 dev/ino 身份比对：同尺寸 + 回填 mtime 的硬链接替换仍判 gone."""
        root = tmp_path / "root"
        root.mkdir()
        original = _write(root / "a.txt", b"AAAA")
        roots = _root(root)
        minted = resolve_path(str(original), roots, protected=ProtectedPaths())
        _, payload = decode_blob_handle(minted.blob_handle)

        replacement = _write(root / "b.txt", b"BBBB")
        st = original.stat()
        os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
        original.unlink()
        os.link(replacement, original)

        with pytest.raises(BlobHandleGoneError):
            resolve_for_pull(payload, roots, protected=ProtectedPaths())

    def test_component_prefix_not_bare_string(self, tmp_path: Path) -> None:
        """围栏按路径分量：/x/data 不包含 /x/database."""
        data = tmp_path / "data"
        data.mkdir()
        database = tmp_path / "database"
        database.mkdir()
        file = _write(database / "f.txt")
        roots = _root(data)

        with pytest.raises(PathResolveError) as exc_info:
            resolve_path(str(file), roots, protected=ProtectedPaths())
        assert _reason(exc_info) == "forbidden"

        with pytest.raises(BlobHandleForbiddenError):
            resolve_for_pull(_manual_path_payload(file), roots, protected=ProtectedPaths())

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
