# -*- coding: utf-8 -*-
# filename: env_segment.py
# @Time    : 2026/07/17
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
input 环境变量命名（ENV_SEGMENT）单一权威 / Input env var naming (ENV_SEGMENT), single source of truth.

协议依据 / Protocol: a2c-smcp-protocol docs/guides/computer-mcp-config-guide.md
                     §「环境变量命名规则（双端统一规范）」（PROTO-5 / Discussion #23 F4-F5，
                     规范 commit 9cde57c，0.3.0-dev）。

存在意义 / Why this module：
    env 名派生 **MUST** 逐字节确定、各 SDK（Python / Rust）产出同一结果——运维写在 CI 里的那一份
    env 配置双端通用是硬门槛。本模块为 `inputs/resolver.py` 提供**唯一**权威，杜绝跨 SDK 漂移。
    一致性向量：`tests/unit_tests/utils/test_env_segment_conformance.py`（16 条，rust 镜像 rust-sdk#140）。

形态 / Shape：
    A2C_SMCP_<ENV_SEGMENT(input_id)>[_<ENV_SEGMENT(bundle_id)>][_<ENV_SEGMENT(tool_name)>]

🔴 与 `bundle_id.normalize_name` 的关键差异（**勿复用后者**）：
    ENV_SEGMENT **不**折叠连续 `_`、**不**裁首尾 `[_-]`——`normalize_name` 两者都做。误复用会让
    `a_b`/`a__b` 坍缩、`_lead_` 变 `lead`。二者是**两个不同函数**，各自服务不同规范面。

**server/tool 段现状（#155 决策 1）**：双端 live 解析路径**均只传裸 id**（rust 的 `InputContext`
调用点全在 `#[cfg(test)]`，rust-sdk#140 明文本轮保持预防性）。此处支持全形态是为「将来接线时不双端分叉」，
由一致性向量锁定形态。

**0.3.0 硬切（F5）**：历史前缀 `A2C_INPUT_` + `upper()` 已废止，**无双读、无过渡期**（正式上线前一律
不做向后兼容设计）。旧 `upper()` 方案会让 `figma-token` / `figma_token` / `Figma_Token` 三者静默坍缩
到同一变量名——F4 的「保留大小写」正为消灭此类坍缩。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# 0.3.0 起统一前缀（F4）。同时保证产出恒为合法 POSIX env 名（首字符为字母，不以数字起头）。
A2C_ENV_PREFIX = "A2C_SMCP_"

# 显式 ASCII 字符类 [A-Za-z0-9_]（规范：MUST 用显式类，MUST NOT 用 \w——各语言 Unicode \w 集合不一致）。
# 取反后逐**码点**匹配：CJK / astral 平面 emoji 均为单码点 → 单个 `_`（与 rust chars() 对齐）。
_ENV_DISALLOWED = re.compile(r"[^A-Za-z0-9_]")


class EnvNameCollisionError(ValueError):
    """
    中文: 两个不同身份映射到**同一完整 env 变量名**（ENV_SEGMENT 非单射，如 ``a-b`` 与 ``a_b``）。
    English: Two distinct identities map to the same full env var name (ENV_SEGMENT is not injective).

    规范要求注册期**硬错误**（F4）：此前是静默串味、后写的赢——含 ``password:true`` 密钥。
    """


def env_segment(s: str) -> str:
    """按 ENV_SEGMENT 规范归一单段 / Normalize one segment per the ENV_SEGMENT spec.

    规则（按 **Unicode 码点**迭代，MUST NOT 按 UTF-8 字节 / grapheme）：
      1. 非 `[A-Za-z0-9_]` 码点（任何非 ASCII 一律命中）→ `_`；
      2. **保留大小写**（POSIX env 名本就大小写敏感；折叠会让 `MyServer`/`myserver` 坍缩）；
      3. **不**折叠连续 `_`、**不**裁首尾——与 `bundle_id.normalize_name` 相反，勿混用。
    """
    return _ENV_DISALLOWED.sub("_", s)


def env_var_name(input_id: str, *, bundle_id: str | None = None, tool_name: str | None = None) -> str:
    """派生 input 的完整 env 变量名 / Derive the full env var name for an input.

    形态：``A2C_SMCP_<seg(input_id)>[_<seg(bundle_id)>][_<seg(tool_name)>]``；段缺省则整段省略
    （含其前导 `_`）。server 上下文段用 **bundle_id**（运行期唯一身份），MUST NOT 用 display name——
    同名 server 会串用彼此的解析值（D2）。
    """
    parts = [env_segment(input_id)]
    if bundle_id is not None:
        parts.append(env_segment(bundle_id))
    if tool_name is not None:
        parts.append(env_segment(tool_name))
    return A2C_ENV_PREFIX + "_".join(parts)


def detect_env_name_collisions(input_ids: Iterable[str]) -> dict[str, list[str]]:
    """检出映射到同一**完整 env 名**的 input id 分组 / Group input ids colliding on the full env name.

    :return: ``{env_var_name: [撞在一起的 input_id, ...]}``，仅含 **>1** 的分组；无冲突则空 dict。

    检测面 = **完整 env 名**（F4）：某段 ENV_SEGMENT 相同但完整名不同的情形**无害**，MUST NOT 报错
    ——按段判会误拒（如 `plugin-a@mp/token` 与 `plugin_a@mp/secret` 前缀段坍缩但完整名分叉）。

    🔴 **接线 server/tool 段时本函数 MUST 同步扩形**：当前只吃裸 id，因 live 路径只有 id 段 ⇒ 裸 id 集
    即「全部活跃 env 名」，协议 MUST 已满足。一旦 bundle_id 段接入 live（见模块头「决策 1」），活跃
    env 名成 (id × bundle_id) 的积，本函数不会自动跟进，会静默退化成只查 id 空间——而协议给的坍缩
    例子（`a-b` / `a_b` → 提示显式指定 `bundleId`）恰恰就是 bundle_id 段。
    """
    by_name: dict[str, list[str]] = {}
    for input_id in input_ids:
        by_name.setdefault(env_var_name(input_id), []).append(input_id)
    return {name: sorted(ids) for name, ids in by_name.items() if len(ids) > 1}


def raise_on_env_name_collisions(input_ids: Iterable[str]) -> None:
    """检出即抛 :class:`EnvNameCollisionError` / Detect and raise, for registration-time fail-fast.

    提示须自解释：同时给出撞上的完整 env 名与全部肇事 id，并指出消歧方向（改 id）。
    """
    collisions = detect_env_name_collisions(input_ids)
    if not collisions:
        return
    detail = "；".join(f"{', '.join(repr(i) for i in ids)} → {name!r}" for name, ids in sorted(collisions.items()))
    raise EnvNameCollisionError(
        f"input id 映射到同一环境变量名，请改 id 消歧 / input ids collide on the same env var name, "
        f"rename one to disambiguate: {detail}",
    )
