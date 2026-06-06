# -*- coding: utf-8 -*-
# filename: test_v02_skill_blob_e2e.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：A2C-SMCP v0.2.x SKILL 通道真进程全链路 e2e（补齐工单 §5 承诺的「Agent 渐进式披露」用例）。

  在真实 Uvicorn ASGI 进程 + 真实协议版本握手中间件 + 真实 Computer（user 源 DropIn，零网络）上，
  端到端覆盖 0.2.1/0.2.2 SKILL + 通用二进制传输能力：

    1. 渐进式披露 + A2CSkillRef 必选字段契约（#88 0.2.2 定稿）：
       client:get_skills → 每个 ref 4 必选字段恒存 → client:get_skill 取入口 SKILL.md（文本内联）
       → 取文本子资源（内联）→ 取超预算文本（高层 API 自动 drain 回填 body）→ 取二进制子资源
       （保留 blob_handle，drain_blob 还原字节 + sha256 一致）。
    2. 安全模型反例：rel_path 穿越 / .skillenv forbidden → 4017（真进程级红→绿）。
    3. notify:update_skills 实时刷新环：运行期写入新 user SKILL → watcher→debouncer→
       server:update_skills→Server 广播→Agent 自动重拉 + on_skills_received 回调。
    4. 飞行断连守卫（#88/#46）：目标 Computer 断连后 Agent 调用确定性失败（不挂起）、Server 仍存活。

English: Real-process full-chain e2e for the v0.2.x SKILL channel (delivers the §5-promised
  progressive-disclosure case). Zero network: user-source DropIn skills only.

协议依据 / Protocol: a2c-smcp-protocol skill.md / blob-transfer.md / events.md / data-structures.md /
  error-handling.md（4017）；SDK 工单 docs/upgrade-0.2.1-skill-blob-transfer.md §5（e2e 行）。
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from socketio.exceptions import TimeoutError as SioTimeoutError

from a2c_smcp.agent.errors import SMCPProtocolError
from a2c_smcp.computer import Computer
from a2c_smcp.computer.blob import BlobThresholds
from a2c_smcp.smcp import ErrorCode
from a2c_smcp.utils.blob import drain_blob
from tests.e2e._skill_harness import (
    connect_agent_and_computer,
    running_server,
    teardown,
    write_user_skill,
)

pytestmark = pytest.mark.e2e

_REQUIRED_REF_FIELDS = ("name", "source", "path", "description")  # A2CSkillRef 0.2.2 定稿必选集
_BINARY_ASSET = bytes((i * 7 + 3) % 256 for i in range(4096))  # 4 KiB 确定性二进制 / deterministic
_BIG_TEXT = "X" * 1024  # > inline_budget(256) → 文本超预算路径 / over-budget text


class _RecordingHandler:
    """中文：记录 on_skills_received 触发的异步事件处理器（其余回调实现为 no-op，避免 AttributeError）。"""

    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.last_skill_names: list[str] = []

    async def on_computer_enter_office(self, data, client) -> None:  # noqa: ANN001
        return None

    async def on_computer_leave_office(self, data, client) -> None:  # noqa: ANN001
        return None

    async def on_computer_update_config(self, data, client) -> None:  # noqa: ANN001
        return None

    async def on_tools_received(self, computer, tools, client) -> None:  # noqa: ANN001
        return None

    async def on_skills_received(self, computer, skills, client) -> None:  # noqa: ANN001
        self.last_skill_names = [s.get("name") for s in skills]
        self.received.set()


@pytest.mark.asyncio
async def test_progressive_disclosure_and_ref_contract(tmp_path) -> None:
    """中文：get_skills 契约 + get_skill 文本/超预算/二进制三分支 + 安全反例（真进程级）。"""
    skill_home = (tmp_path / "home").resolve()
    skill_home.mkdir(parents=True, exist_ok=True)
    write_user_skill(
        skill_home,
        "my-helper",
        description="a user-source helper skill",
        body="# Helper\nThis is the entry doc.\n",
        files={
            "ref.md": "# Reference\ninline text reference\n",
            "big.txt": _BIG_TEXT,
            "assets/img.png": _BINARY_ASSET,
        },
    )

    office_id = "e2e-skill-disclosure"
    computer = Computer(
        name="comp-skill",
        skill_home=skill_home,
        blob_cache_root=tmp_path / "blob",
        blob_thresholds=BlobThresholds(inline_budget=256),
    )

    async with running_server() as base:
        agent, _comp_client = await connect_agent_and_computer(base, office_id, computer, agent_id="robot-skill")
        try:
            # —— client:get_skills：A2CSkillRef 4 必选字段契约（#88）——
            ret = await agent.get_skills("comp-skill")
            skills = ret["skills"]
            assert skills, "user 源 SKILL 应被发现 / user-source skill must be discovered"
            ref = next(s for s in skills if s["name"] == "my-helper")
            for field in _REQUIRED_REF_FIELDS:
                assert field in ref, f"A2CSkillRef 必选字段缺失 / required field missing: {field}"
            assert ref["source"] == "user"
            assert ":" not in ref["name"], "user 源为单段裸名 / user-source name is a bare single segment"

            # —— get_skill 入口 SKILL.md：文本内联（frontmatter 已剥离）——
            entry = await agent.get_skill("comp-skill", "my-helper")
            assert entry["mime_type"] == "text/markdown"
            assert "body" in entry and "blob_handle" not in entry
            assert "entry doc" in entry["body"]
            assert "name: my-helper" not in entry["body"], "frontmatter 应已剥离 / frontmatter must be stripped"

            # —— get_skill 文本子资源：内联 ——
            ref_md = await agent.get_skill("comp-skill", "my-helper", "ref.md")
            assert "body" in ref_md and "inline text reference" in ref_md["body"]

            # —— get_skill 超预算文本：高层 API 自动 drain 回填 body（text MIME 分支）——
            big = await agent.get_skill("comp-skill", "my-helper", "big.txt")
            assert big.get("body") == _BIG_TEXT
            assert "blob_handle" not in big, "文本超预算应自动 drain 为 body / over-budget text auto-drained"

            # —— get_skill 二进制子资源：保留 blob_handle → drain_blob 还原 + sha256 一致 ——
            img = await agent.get_skill("comp-skill", "my-helper", "assets/img.png")
            assert img["mime_type"] == "image/png"
            assert "blob_handle" in img, "二进制 MIME 一律铸句柄 / binary MIME keeps a handle"
            assert img["sha256"] == hashlib.sha256(_BINARY_ASSET).hexdigest()
            assert img["total_size"] == len(_BINARY_ASSET)
            data, mime = await drain_blob(agent._make_blob_call(), "comp-skill", img["blob_handle"], chunk_size=1024)
            assert data == _BINARY_ASSET
            assert mime == "image/png"
            assert hashlib.sha256(data).hexdigest() == img["sha256"]

            # —— 安全模型反例（真进程级红→绿）：穿越 / .skillenv forbidden → 4017 ——
            with pytest.raises(SMCPProtocolError) as ei_trav:
                await agent.get_skill("comp-skill", "my-helper", "../escape")
            assert ei_trav.value.code == int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE)
            with pytest.raises(SMCPProtocolError) as ei_env:
                await agent.get_skill("comp-skill", "my-helper", ".skillenv")
            assert ei_env.value.code == int(ErrorCode.SKILL_RESOURCE_NOT_ACCESSIBLE)
        finally:
            await teardown(agent, computer)


@pytest.mark.asyncio
async def test_update_skills_live_refetch_loop(tmp_path, monkeypatch) -> None:
    """中文：运行期新增 user SKILL → watcher→server:update_skills→广播→Agent 自动重拉 + on_skills_received。"""
    # watcher 用 PollingObserver 保确定性（须在 Computer 构造前设置）/ deterministic polling watcher
    monkeypatch.setenv("A2C_SKILL_WATCH_POLLING", "1")

    skill_home = (tmp_path / "home").resolve()
    skill_home.mkdir(parents=True, exist_ok=True)
    write_user_skill(skill_home, "first-skill", description="the first skill", body="# first\n")

    office_id = "e2e-skill-update"
    handler = _RecordingHandler()
    computer = Computer(name="comp-upd", skill_home=skill_home, blob_cache_root=tmp_path / "blob")

    async with running_server() as base:
        agent, _comp_client = await connect_agent_and_computer(
            base,
            office_id,
            computer,
            agent_id="robot-upd",
            event_handler=handler,
        )
        try:
            # 基线：初始仅 first-skill / baseline has only first-skill
            base_ret = await agent.get_skills("comp-upd")
            assert {s["name"] for s in base_ret["skills"]} == {"first-skill"}

            handler.received.clear()
            await asyncio.sleep(0.5)  # 确保 watcher 已就绪 / let the watcher settle
            # 运行期新增第二个 user SKILL → 触发整条刷新链 / drop a 2nd skill at runtime
            write_user_skill(skill_home, "second-skill", description="the second skill", body="# second\n")

            # 等待 notify:update_skills→自动重拉→on_skills_received（宽超时防 CI 抖动）
            await asyncio.wait_for(handler.received.wait(), timeout=20)
            assert "second-skill" in handler.last_skill_names, (
                f"on_skills_received 未带出新 SKILL / new skill missing from refetch: {handler.last_skill_names}"
            )

            # 授权拉取复核：两个 SKILL 均可见 / authoritative recheck
            after = await agent.get_skills("comp-upd")
            assert {"first-skill", "second-skill"} <= {s["name"] for s in after["skills"]}
        finally:
            await teardown(agent, computer)


@pytest.mark.asyncio
async def test_inflight_disconnect_guard_relay_target_gone(tmp_path) -> None:
    """中文：目标 Computer 断连后，Agent 的 client:get_skills 在 timeout 内确定性失败（不挂起），Server 仍存活。

    复核 #88/#46 ``_relay_client_call`` 守卫家族在真进程级的「不挂起 + Server 不崩」保证：目标 Computer 名
    不在册 → Server 端 relay 直接拒绝 → 无 ack → 客户端有界超时（而非无限挂起）；其后房间查询仍正常应答。
    """
    skill_home = (tmp_path / "home").resolve()
    skill_home.mkdir(parents=True, exist_ok=True)
    write_user_skill(skill_home, "guard-skill", description="guard skill", body="# guard\n")

    office_id = "e2e-skill-guard"
    computer = Computer(name="comp-guard", skill_home=skill_home, blob_cache_root=tmp_path / "blob")

    async with running_server() as base:
        agent, comp_client = await connect_agent_and_computer(base, office_id, computer, agent_id="robot-guard")
        try:
            # 健康基线：断连前 get_skills 正常 / healthy baseline
            ok = await agent.get_skills("comp-guard")
            assert {s["name"] for s in ok["skills"]} == {"guard-skill"}

            # Computer 断连 → 其会话从 Server 移除 / disconnect the Computer
            await comp_client.disconnect()
            await asyncio.sleep(0.5)

            # 有界失败（不挂起）：relay 找不到目标 → 无 ack → 客户端超时 / bounded failure, not a hang
            with pytest.raises((SioTimeoutError, TimeoutError, SMCPProtocolError)):
                await asyncio.wait_for(agent.get_skills("comp-guard", timeout=3), timeout=10)

            # Server 仍存活：房间查询正常应答（守卫未让命名空间崩溃）/ server survived
            sessions = await agent.get_computers_in_office(office_id)
            assert isinstance(sessions, list)
        finally:
            await teardown(agent, computer)
