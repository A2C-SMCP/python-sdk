# -*- coding: utf-8 -*-
"""
文件名: test_resolve.py
作者: JQQ
创建日期: 2026/7/17
最后修改日期: 2026/7/17
版权: 2023 JQQ. All rights reserved.
依赖: pytest
描述:
  中文: `cli/resolve.py` 的 `resolve_target` 纯函数契约（#143 / R4 / 协议 sdk-api-guidance §5.1）。
    人机面唯一的 name→bundle_id 解析处；四分支 + 禁字典序 + 未命中必报错（杜绝静默假成功）。
  English: Pure-function contract of `resolve_target` (#143 / R4 / protocol sdk-api-guidance §5.1) —
    the only name→bundle_id resolution site, which lives on the human-facing surface.

⚠️ 夹具铁律（Epic #147 陷阱其一，已四例致盲）: name 与 bundle_id **必须分叉**。
   ``name="my.server"`` → ``bundle_id="my_server"``（``.`` 被 normalize 成 ``_``）。
   **禁**用 ``stdio-srv`` 这类规范化后恰等于自身的名——``-`` 不被折叠，两概念同值 ⇒ 断言零鉴别力。
"""

from __future__ import annotations

import pytest

from a2c_smcp.computer.cli.resolve import (
    AmbiguousTargetError,
    ServerCandidate,
    TargetNotFoundError,
    resolve_target,
)

# 分叉夹具：display name 含 `.` / 空格，规范化后与 bundle_id 不等 ⇒ 断言有鉴别力。
_MY_SERVER = ServerCandidate(bundle_id="my_server", name="my.server", attribution="user")
_CAP_SERVER = ServerCandidate(bundle_id="My_Server", name="My Server", attribution="project")


def test_unique_name_hit_resolves_to_bundle_id() -> None:
    """步骤 1: token 按 display name 反查唯一命中 → 解析为其 bundle_id。"""
    assert resolve_target("my.server", (_MY_SERVER, _CAP_SERVER)) == "my_server"


def test_zero_name_hit_but_valid_registered_bundle_id_resolves_as_id() -> None:
    """步骤 2: 0 个 name 命中 ∧ token 是合法且**已注册**的 bundle_id → 按 bundle_id 执行。"""
    assert resolve_target("my_server", (_MY_SERVER, _CAP_SERVER)) == "my_server"


def test_valid_but_unregistered_bundle_id_raises_not_found() -> None:
    """步骤 2 后半 + 步骤 5: token 形如合法 bundle_id 但**不在候选集** → 必须报「未找到」。

    这是杀死 ``stop <未注册 token>`` 静默假成功的关键断言：语法合法 ≠ 存在。
    """
    with pytest.raises(TargetNotFoundError):
        resolve_target("bundle_deadbeef", (_MY_SERVER,))


def test_unknown_token_raises_not_found() -> None:
    """步骤 4/5: 既非 name 又非合法已注册 bundle_id → 报「未找到」，MUST NOT 静默成功。"""
    with pytest.raises(TargetNotFoundError):
        resolve_target("nonexistent", (_MY_SERVER, _CAP_SERVER))


def test_name_collision_raises_ambiguous_with_full_candidates() -> None:
    """步骤 3 + PROTO-10 扩条: 多命中 → 报错并列出**每个候选的 bundle_id + name + 归属三者**。

    只列 bundle_id 用户分不清哪个是自己的（协议 §5.1-3）。
    """
    user_fs = ServerCandidate(bundle_id="filesystem", name="filesystem", attribution="user")
    plugin_fs = ServerCandidate(bundle_id="bundle_a3f9c2e1", name="filesystem", attribution="plugin:fs-tools")

    with pytest.raises(AmbiguousTargetError) as exc:
        resolve_target("filesystem", (user_fs, plugin_fs))

    assert set(exc.value.candidates) == {user_fs, plugin_fs}
    # 三个维度都必须能被 CLI 渲染出来，缺一不可。
    for cand in exc.value.candidates:
        assert cand.bundle_id and cand.name and cand.attribution


def test_ambiguous_never_silently_picks_lexicographically_smallest() -> None:
    """步骤 4: MUST NOT 以字典序最小等任意规则「确定性地选一个」——那是把不确定的错变成确定的错。

    变异守卫: 若实现退化为 ``return min(hits)``，本例会拿到 ``"bundle_a"`` 而非抛错 ⇒ 转红。
    """
    a = ServerCandidate(bundle_id="bundle_a", name="dup", attribution="user")
    z = ServerCandidate(bundle_id="bundle_z", name="dup", attribution="plugin:p")

    with pytest.raises(AmbiguousTargetError):
        resolve_target("dup", (a, z))


def test_name_hit_takes_precedence_over_bundle_id_hit() -> None:
    """步骤序: name 反查（步骤 1）先于 bundle_id 反查（步骤 2）——步骤 2 的门是「0 个 name 命中」。

    A(name='foo', id='foo_1') 与 B(name='bar', id='foo') 共存时，``foo`` 命中 A 的 **name** ⇒ 解析为 foo_1。
    """
    a = ServerCandidate(bundle_id="foo_1", name="foo", attribution="user")
    b = ServerCandidate(bundle_id="foo", name="bar", attribution="user")

    assert resolve_target("foo", (a, b)) == "foo_1"


@pytest.mark.xfail(
    reason=(
        "协议 sdk-api-guidance §5.1 步骤序缺口（#143 决策 4，待协议裁决）: 步骤 2「0 命中且是合法 "
        "bundle_id → 当 id」的门是**「0 命中」**，故同名多命中时够不到步骤 2；而步骤 3 要求多命中 MUST "
        "报错、步骤 4 禁任意规则选一。⇒ A(name='foo', 缺省派生 id='foo') 与 B(name='foo', 显式 "
        "id='bundle_x') 合法共存（§5.6）时，A 的 bundle_id 恰等于那个冲突的名字，用户照「请用 bundle_id "
        "重试」再敲 'foo' 仍是 name 多命中 ⇒ **A 永远不可寻址**。修它属改协议明文，且该语义 MUST 双端逐字"
        "一致 ⇒ 不单端发明。本轮严格实现协议并以本例钉住缺口（不靠文档降级）。"
    ),
    strict=True,
)
def test_deadlock_corner_bundle_id_equal_to_colliding_name_should_be_addressable() -> None:
    """死锁角落: 同名冲突 ∧ 其中一条的 bundle_id 恰等于那个名字 → 该条应仍可用 bundle_id 寻址。"""
    a = ServerCandidate(bundle_id="foo", name="foo", attribution="user")
    b = ServerCandidate(bundle_id="bundle_x", name="foo", attribution="plugin:fs-tools")

    assert resolve_target("foo", (a, b)) == "foo"
