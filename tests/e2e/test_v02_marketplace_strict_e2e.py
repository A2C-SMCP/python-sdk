# -*- coding: utf-8 -*-
# filename: test_v02_marketplace_strict_e2e.py
# @Author  : JQQ
# @Software: PyCharm
"""
中文：A2C-SMCP v0.2.2 marketplace strict 模式 + entry.skills override 经 Agent 发现的真进程 e2e（#80/#89）。

  零网络：本地 ``git init --bare`` 裸仓作 ``file://`` 源；``A2C_SKILL_HOME`` → tmp。marketplace plugin
  在 marketplace.json 声明 ``skills`` 覆写容器（entry.skills，§4.3，strict=true 追加合并）。经
  ``stage_marketplace_skills`` 物化进真实 Computer 的 SkillRegistry 后，Agent ``client:get_skills``
  应同时发现「约定 skills/」与「entry.skills 覆写」两类 SKILL——验证 strict/override 语义在
  **Agent 协议表面**（而非仅 staging 内部）生效。

English: Real-process e2e for v0.2.2 marketplace strict mode + entry.skills override, observed via the
  Agent's ``client:get_skills`` discovery. Zero network (local bare git repo as a ``file://`` source).

协议依据 / Protocol: tfrobot-marketplace protocol-v1 §4.3（entry.skills override）/ §4.4（strict 冲突）；
  SDK staging ``a2c_smcp/computer/skills/staging.py``（``stage_marketplace_skills`` / ``resolve_skill_override_dirs``）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a2c_smcp.computer import Computer
from a2c_smcp.computer.skills.staging import stage_marketplace_skills
from tests.e2e._skill_harness import connect_agent_and_computer, running_server, teardown

pytestmark = pytest.mark.e2e

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="requires the git binary")

_GIT_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_IDENTITY, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _skill_md(name: str, description: str = "a skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\nlicense: MIT\n---\n# {name}\nbody\n"


def _make_bare(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """中文：建含 files 的工作仓 + 裸仓镜像，返回裸仓路径（作 file:// 源）。"""
    work = tmp_path / f"{name}-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    _write(work, files)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / f"{name}.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def _marketplace_files() -> dict[str, str]:
    """中文：plugin `audit` 同时有约定 skills/ 与 entry.skills 覆写容器 `extra-skills`（strict 默认 true 追加合并）。"""
    manifest = {
        "name": "acme-skills",
        "owner": {"name": "Acme"},
        "metadata": {"pluginRoot": "./plugins"},
        "plugins": [
            # entry.skills 覆写容器（相对 plugin 根）+ 约定 skills/ 追加合并（§4.3，strict 默认 true）
            {"name": "audit", "source": "./plugins/audit", "version": "1.0.0", "skills": ["extra-skills"]},
        ],
    }
    return {
        ".tfrobot-plugin/marketplace.json": json.dumps(manifest),
        "plugins/audit/skills/audit-code/SKILL.md": _skill_md("audit-code"),  # 约定容器 / convention
        "plugins/audit/extra-skills/extra-tool/SKILL.md": _skill_md("extra-tool"),  # override 容器
    }


@requires_git
@pytest.mark.asyncio
async def test_strict_mode_entry_skills_override_via_agent_discovery(tmp_path) -> None:
    """中文：约定 + entry.skills override 两类 SKILL 均经 Agent get_skills 发现，名为 <plugin>:<skill>、source marketplace:*。"""
    bare = _make_bare(tmp_path, "acme", _marketplace_files())
    home = (tmp_path / "skill-home").resolve()
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "A2C_SKILL_HOME": str(home)}

    office_id = "e2e-mp-strict"
    computer = Computer(name="comp-mp", skill_home=home, blob_cache_root=tmp_path / "blob")

    async with running_server() as base:
        agent, _comp_client = await connect_agent_and_computer(base, office_id, computer, agent_id="robot-mp")
        try:
            # 直接物化 marketplace 源进 Computer 的 Registry（无 CLI 依赖）/ stage directly into the shared registry
            registered = await stage_marketplace_skills(
                "acme-skills",
                {"type": "git", "url": f"file://{bare}"},
                computer.skill_registry,
                home,
                env=env,
            )
            assert set(registered) == {"audit:audit-code", "audit:extra-tool"}, (
                f"约定 + override 两类均应注册 / convention+override must both register: {registered}"
            )

            # 经 Agent 协议表面发现 / discover via the Agent protocol surface
            ret = await agent.get_skills("comp-mp")
            by_name = {s["name"]: s for s in ret["skills"]}
            assert {"audit:audit-code", "audit:extra-tool"} <= set(by_name), (
                f"entry.skills override 未经 Agent 发现 / override not surfaced via Agent: {set(by_name)}"
            )
            for name in ("audit:audit-code", "audit:extra-tool"):
                s = by_name[name]
                assert s["source"] == "marketplace:acme-skills"
                assert s["name"].count(":") == 1, "marketplace 源为 2 段裸名 <plugin>:<skill>"
                assert all(k in s for k in ("name", "source", "path", "description"))  # 4 必选字段
        finally:
            await teardown(agent, computer)
