# -*- coding: utf-8 -*-
# filename: test_on_get_skill.py
# @Time    : 2026/05/25
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
``client:get_skills`` / ``client:get_skill`` / ``emit_update_skills`` handler 单元测试（v0.2.1 #66）
Unit tests for the SKILL channel handlers on the Computer Socket.IO client.

协议依据 / Protocol: a2c-smcp-protocol events.md#client:get_skill[s] / §server:update_skills；
                      error-handling.md §4014 / §4016 / §4017；skill.md §6 / §9。
设计依据 / Design: python-sdk docs/design-0.2.1-skill-computer-management.md §4.2 / §5.1 / §5.3。

覆盖 / Coverage:
  - on_get_skills：活跃集（排除孤儿）、发现序、office 隔离
  - on_get_skill：4016 / 4014 / 4017(traversal/forbidden/not_found/too_large) / inline body / blob_handle / 二进制铸句柄
  - too_large 不铸句柄（flat ErrorPayload）
  - emit_update_skills：office_id 守卫
  - 安全反例（§5.3）：穿越→traversal / .skillenv→forbidden（存在仍 forbidden）/ too_large 零字节
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from a2c_smcp.computer.blob import BlobThresholds, decode_blob_handle
from a2c_smcp.computer.computer import Computer
from a2c_smcp.computer.socketio.client import SMCPComputerClient
from a2c_smcp.exceptions import SMCPNamespaceError
from a2c_smcp.smcp import UPDATE_SKILLS_EVENT, A2CSkillRef, ErrorCode

_SKILL_MD = "---\nname: demo\nversion: 1.0.0\n---\n# Demo\n\nshort body\n"
_SKILL_BODY = "# Demo\n\nshort body\n"  # frontmatter 剥离后正文


def _make_pkg(root: Path) -> Path:
    pkg = root / "mcp" / "srv" / "demo"
    (pkg / "references").mkdir(parents=True)
    (pkg / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (pkg / "references" / "note.txt").write_text("note content\n", encoding="utf-8")
    (pkg / ".skillenv").write_text("SECRET=1\n", encoding="utf-8")
    (pkg / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    return pkg


def _computer(tmp_path: Path, *, thresholds: BlobThresholds | None = None, name: str = "comp-test") -> Computer:
    return Computer(
        name=name,
        blob_cache_root=tmp_path / "blobspool",
        blob_thresholds=thresholds,
        auto_connect=False,
        auto_reconnect=False,
    )


def _register_demo(computer: Computer, pkg: Path) -> None:
    ref: A2CSkillRef = {"name": "mcp:srv:demo", "source": "mcp:srv", "path": str(pkg)}
    assert computer.skill_registry.register(ref) is True


def _skill_req(name: str, *, rel_path: str | None = None, computer_name: str = "comp-test") -> dict[str, Any]:
    req: dict[str, Any] = {"agent": "agent-1", "req_id": "r-1", "computer": computer_name, "name": name}
    if rel_path is not None:
        req["rel_path"] = rel_path
    return req


# ===========================================================================
# on_get_skills
# ===========================================================================
class TestOnGetSkills:
    @pytest.mark.asyncio
    async def test_excludes_orphans_discovery_order(self, tmp_path: Path) -> None:
        comp = _computer(tmp_path)
        pkg = _make_pkg(tmp_path / "home")
        # 发现序：demo → helper（孤儿 gone 不计入）
        comp.skill_registry.register({"name": "mcp:srv:demo", "source": "mcp:srv", "path": str(pkg)})
        comp.skill_registry.register({"name": "my-helper", "source": "user", "path": str(pkg)})
        comp.skill_registry.register({"name": "mcp:srv:gone", "source": "mcp:srv", "path": str(pkg)})
        comp.skill_registry.mark_orphan("mcp:srv:gone")

        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skills(_skill_req("ignored-name"))  # type: ignore[arg-type]
        names = [s["name"] for s in ret["skills"]]
        assert names == ["mcp:srv:demo", "my-helper"]  # 排除孤儿 + 保留发现序
        assert ret["req_id"] == "r-1"

    @pytest.mark.asyncio
    async def test_empty_registry(self, tmp_path: Path) -> None:
        client = SMCPComputerClient(computer=_computer(tmp_path))
        ret = await client.on_get_skills(_skill_req("x"))  # type: ignore[arg-type]
        assert ret["skills"] == []

    @pytest.mark.asyncio
    async def test_computer_mismatch_raises(self, tmp_path: Path) -> None:
        client = SMCPComputerClient(computer=_computer(tmp_path))
        with pytest.raises(SMCPNamespaceError):
            await client.on_get_skills(_skill_req("x", computer_name="other"))  # type: ignore[arg-type]


# ===========================================================================
# on_get_skill —— 错误码分支
# ===========================================================================
class TestOnGetSkillErrors:
    @pytest.mark.asyncio
    async def test_4016_invalid_name(self, tmp_path: Path) -> None:
        client = SMCPComputerClient(computer=_computer(tmp_path))
        ret = await client.on_get_skill(_skill_req("MySkill"))  # type: ignore[arg-type]  # 大写非法
        assert ret["code"] == int(ErrorCode.SKILL_NAME_INVALID)
        assert ret["details"]["name"] == "MySkill"
        assert "message" in ret

    @pytest.mark.asyncio
    async def test_4014_valid_name_not_registered(self, tmp_path: Path) -> None:
        client = SMCPComputerClient(computer=_computer(tmp_path))
        ret = await client.on_get_skill(_skill_req("mcp:srv:absent"))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.MCP_SERVER_NOT_FOUND)  # 4014 复用
        assert ret["details"]["name"] == "mcp:srv:absent"

    @pytest.mark.asyncio
    async def test_4014_orphaned(self, tmp_path: Path) -> None:
        comp = _computer(tmp_path)
        pkg = _make_pkg(tmp_path / "home")
        _register_demo(comp, pkg)
        comp.skill_registry.mark_orphan("mcp:srv:demo")
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo"))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.MCP_SERVER_NOT_FOUND)

    @pytest.mark.parametrize("rel_path", ["../escape", "../../etc/passwd", "references/../../out"])
    @pytest.mark.asyncio
    async def test_4017_traversal(self, tmp_path: Path, rel_path: str) -> None:
        comp = _computer(tmp_path)
        pkg = _make_pkg(tmp_path / "home")
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo", rel_path=rel_path))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE)
        assert ret["details"]["reason"] == "traversal"

    @pytest.mark.asyncio
    async def test_4017_skillenv_forbidden_even_when_exists(self, tmp_path: Path) -> None:
        """安全反例：.skillenv 真实存在仍 forbidden（不泄漏存在性）。"""
        comp = _computer(tmp_path)
        pkg = _make_pkg(tmp_path / "home")
        assert (pkg / ".skillenv").exists()
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo", rel_path=".skillenv"))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE)
        assert ret["details"]["reason"] == "forbidden"

    @pytest.mark.asyncio
    async def test_4017_not_found(self, tmp_path: Path) -> None:
        comp = _computer(tmp_path)
        pkg = _make_pkg(tmp_path / "home")
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo", rel_path="nope.txt"))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE)
        assert ret["details"]["reason"] == "not_found"

    @pytest.mark.asyncio
    async def test_4017_too_large_no_handle_minted(self, tmp_path: Path) -> None:
        """安全反例：超 too_large_cap → 4017 too_large，零字节、不铸句柄。"""
        comp = _computer(tmp_path, thresholds=BlobThresholds(too_large_cap=4))
        pkg = _make_pkg(tmp_path / "home")
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo", rel_path="references/note.txt"))  # type: ignore[arg-type]
        assert ret["code"] == int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE)
        assert ret["details"]["reason"] == "too_large"
        assert ret["details"]["total_size"] == len(b"note content\n")
        assert "blob_handle" not in ret  # 不铸句柄

    @pytest.mark.asyncio
    async def test_computer_mismatch_raises(self, tmp_path: Path) -> None:
        client = SMCPComputerClient(computer=_computer(tmp_path))
        with pytest.raises(SMCPNamespaceError):
            await client.on_get_skill(_skill_req("mcp:srv:demo", computer_name="other"))  # type: ignore[arg-type]


# ===========================================================================
# on_get_skill —— 成功路径（inline vs handle）
# ===========================================================================
class TestOnGetSkillSuccess:
    @pytest.mark.asyncio
    async def test_skill_md_inline_body_frontmatter_stripped(self, tmp_path: Path) -> None:
        comp = _computer(tmp_path)  # 默认 inline_budget 32KiB → 小 body 内联
        pkg = _make_pkg(tmp_path / "home")
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo"))  # type: ignore[arg-type]  # 缺省 rel_path
        import hashlib

        expected = _SKILL_BODY.encode("utf-8")
        assert ret["name"] == "mcp:srv:demo"
        assert ret["rel_path"] == "SKILL.md"
        assert ret["mime_type"] == "text/markdown"
        assert ret["body"] == _SKILL_BODY
        assert "blob_handle" not in ret
        assert ret["total_size"] == len(expected)
        assert ret["sha256"] == hashlib.sha256(expected).hexdigest()

    @pytest.mark.asyncio
    async def test_text_over_inline_budget_mints_handle(self, tmp_path: Path) -> None:
        comp = _computer(tmp_path, thresholds=BlobThresholds(inline_budget=4))  # 强制超预算
        pkg = _make_pkg(tmp_path / "home")
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo"))  # type: ignore[arg-type]
        assert "body" not in ret
        assert "blob_handle" in ret
        kind, payload = decode_blob_handle(ret["blob_handle"])
        assert kind == "skill"
        assert payload == {"name": "mcp:srv:demo", "rel_path": "SKILL.md"}

    @pytest.mark.asyncio
    async def test_binary_always_mints_handle(self, tmp_path: Path) -> None:
        """二进制 MIME（image/png）无论大小一律铸句柄（不内联）。"""
        comp = _computer(tmp_path)  # 默认大预算
        pkg = _make_pkg(tmp_path / "home")
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo", rel_path="logo.png"))  # type: ignore[arg-type]
        assert ret["mime_type"] == "image/png"
        assert "body" not in ret
        assert "blob_handle" in ret

    @pytest.mark.asyncio
    async def test_subresource_inline_text(self, tmp_path: Path) -> None:
        comp = _computer(tmp_path)
        pkg = _make_pkg(tmp_path / "home")
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo", rel_path="references/note.txt"))  # type: ignore[arg-type]
        assert ret["rel_path"] == "references/note.txt"
        assert ret["body"] == "note content\n"  # 非 SKILL.md → 不剥 frontmatter（原始字节）
        assert ret["mime_type"] == "text/plain"

    @pytest.mark.asyncio
    async def test_textual_mime_but_invalid_utf8_falls_back_to_handle(self, tmp_path: Path) -> None:
        """文本 MIME（text/plain）但内容非 UTF-8 → decode 失败回退铸句柄（不内联破损字符串）。"""
        comp = _computer(tmp_path)  # 默认大预算：≤budget 才进 decode 路径
        pkg = _make_pkg(tmp_path / "home")
        (pkg / "bad.txt").write_bytes(b"\xff\xfe\xff\xfe")  # 非法 UTF-8，.txt → text/plain
        _register_demo(comp, pkg)
        client = SMCPComputerClient(computer=comp)
        ret = await client.on_get_skill(_skill_req("mcp:srv:demo", rel_path="bad.txt"))  # type: ignore[arg-type]
        assert ret["mime_type"] == "text/plain"
        assert "body" not in ret  # decode 失败 → 不内联
        assert "blob_handle" in ret  # 回退铸句柄


# ===========================================================================
# emit_update_skills
# ===========================================================================
class TestEmitUpdateSkills:
    @pytest.mark.asyncio
    async def test_emits_when_in_office(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = SMCPComputerClient(computer=_computer(tmp_path, name="comp-x"))
        client.office_id = "office-1"
        called: dict[str, Any] = {}

        async def fake_emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
            called["event"] = event
            called["data"] = data

        monkeypatch.setattr(SMCPComputerClient, "emit", fake_emit)
        await client.emit_update_skills()
        assert called["event"] == UPDATE_SKILLS_EVENT
        assert called["data"]["computer"] == "comp-x"

    @pytest.mark.asyncio
    async def test_noop_when_not_in_office(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = SMCPComputerClient(computer=_computer(tmp_path))
        client.office_id = None
        called: dict[str, Any] = {}

        async def fake_emit(self, event: str, data: Any = None, namespace: str | None = None, callback: Any = None) -> None:
            called["event"] = event

        monkeypatch.setattr(SMCPComputerClient, "emit", fake_emit)
        await client.emit_update_skills()
        assert "event" not in called  # office_id 守卫：未入房间不发送
