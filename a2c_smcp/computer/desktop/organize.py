# -*- coding: utf-8 -*-
# filename: organize.py
# @Time    : 2025/10/02 16:12
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
桌面组织策略（抽象层）
Desktop organizing strategy (abstraction layer)

说明 / Notes:
- 本文件仅定义组织函数签名与最小可运行实现，便于主流程打通；
  后续会基于：最近工具调用历史、MCP内优先级、全屏窗口等规则完善策略。
- This file only defines the function signature and a minimal runnable implementation
  to keep main flow working. We'll implement real policies later based on recent
  tool-call history, in-server priority, and fullscreen windows, etc.
"""

from __future__ import annotations

from mcp.types import BlobResourceContents, ReadResourceResult, Resource, TextResourceContents

from a2c_smcp.computer.types import ToolCallRecord
from a2c_smcp.smcp import Desktop
from a2c_smcp.types import SERVER_NAME
from a2c_smcp.utils import is_window_uri

__all__ = ["organize_desktop"]

from a2c_smcp.utils.logger import get_logger

logger = get_logger("computer")


def _read_priority(res: Resource) -> float:
    """
    从 ``Resource.annotations.priority`` 读取布局优先级（v0.2 协议指南 §6.2 / §6.4）。
    Read layout priority from ``Resource.annotations.priority`` per protocol §6.2/§6.4.

    - ``annotations is None`` 与 ``annotations.priority is None`` 等价处理为 ``0.0``（DEBUG）。
      Both ``annotations is None`` and ``annotations.priority is None`` map to ``0.0`` (DEBUG).
    - 越界（不在 ``[0.0, 1.0]``）记录 WARN 后回退为 ``0.0``。
      Values outside ``[0.0, 1.0]`` are logged as WARN and clamped to ``0.0``.
    - 非数值类型记录 WARN 后回退为 ``0.0``。
      Non-numeric values are logged as WARN and fall back to ``0.0``.
    """
    annotations = getattr(res, "annotations", None)
    if annotations is None:
        return 0.0
    prio = getattr(annotations, "priority", None)
    if prio is None:
        return 0.0
    try:
        prio_f = float(prio)
    except (TypeError, ValueError):
        logger.warning(f"annotations.priority 非数值类型，按 0.0 处理 / non-numeric priority, treat as 0.0: {prio!r}")
        return 0.0
    if not 0.0 <= prio_f <= 1.0:
        logger.warning(
            f"annotations.priority 越界 [0.0, 1.0]，按 0.0 处理 / out-of-range priority, treat as 0.0: {prio_f}",
        )
        return 0.0
    return prio_f


def _read_fullscreen(res: Resource) -> bool:
    """
    从 ``Resource._meta['fullscreen']`` 读取全屏标记（v0.2 协议指南 §6.2）。
    Read fullscreen flag from ``Resource._meta['fullscreen']`` per protocol §6.2.

    - ``_meta is None`` 与缺 ``fullscreen`` 键等价处理为 ``False``（DEBUG）。
      Missing ``_meta`` or missing ``fullscreen`` key both map to ``False`` (DEBUG).
    - 非布尔类型记录 WARN 后回退为 ``False``。
      Non-bool values are logged as WARN and fall back to ``False``.
    """
    meta = getattr(res, "meta", None)
    if not isinstance(meta, dict):
        return False
    if "fullscreen" not in meta:
        return False
    val = meta["fullscreen"]
    if isinstance(val, bool):
        return val
    logger.warning(f"_meta.fullscreen 非布尔类型，按 False 处理 / non-bool fullscreen, treat as False: {val!r}")
    return False


def _check_audience(res: Resource) -> None:
    """
    Window 资源的 ``annotations.audience`` SHOULD 显式声明 ``["assistant"]``（协议 §4.1）。
    Window resources SHOULD declare ``annotations.audience=["assistant"]`` per §4.1.

    若声明 ``["user"]`` 而不含 ``"assistant"``，v0.2 仅记录 WARN 但仍纳入聚合。
    If declared as ``["user"]`` without ``"assistant"``, v0.2 logs WARN but still includes the resource.
    """
    annotations = getattr(res, "annotations", None)
    if annotations is None:
        return
    audience = getattr(annotations, "audience", None)
    if audience is None:
        return
    if "assistant" not in audience:
        logger.warning(
            "Window 资源 annotations.audience 不含 'assistant'（v0.2 仅 WARN，v0.3+ 可能硬过滤）/ "
            f"window resource missing 'assistant' in audience: {audience!r}",
        )


async def organize_desktop(
    *,
    windows: list[tuple[SERVER_NAME, Resource, ReadResourceResult]],
    size: int | None,
    history: tuple[ToolCallRecord, ...],
) -> list[Desktop]:
    """
    组织来自各 MCP Server 的窗口资源，生成 Desktop 列表。
    Organize window resources from MCP servers into a list of Desktop items.

    组织规则（v0.2 协议指南 §6.2）：
    Rules per protocol guide §6.2 (v0.2):
      1) 若指定 window_uri，Manager 层已完成过滤；此处按一般规则组织即可。
      2) 按最近工具调用历史对应的 MCP Server 倒序优先（最近使用的服务器优先）。
      3) 同一 MCP Server 内，按 ``Resource.annotations.priority``（float [0.0, 1.0]，缺省 0.0）
         降序推入；URI query 不再承载排序信息。
         Within the same MCP Server, sort by ``Resource.annotations.priority`` (float [0.0, 1.0],
         default 0.0) descending; URI query no longer carries ordering metadata.
      4) 若 ``Resource._meta.fullscreen=True``，则该 MCP 仅推入第一个 fullscreen 窗口（按原始序号最小者）；
         然后进入下一个 MCP。
         If ``Resource._meta.fullscreen=True``, push only the first fullscreen window per server,
         then move to the next server.
      5) 全局按 size 截断（None 表示不限；size<=0 则返回空）。

    Args:
        windows (list[tuple[SERVER_NAME, Resource, ReadResourceResult]]): (server_name, resource, detail) 列表。
        size (int | None): 期望返回的最大数量；None 表示全部。
        history (tuple[ToolCallRecord, ...]): 最近的工具调用历史（用于服务器顺序）。

    Returns:
        list[Desktop]: 桌面内容（字符串列表）。
    """
    # 快速处理 size 边界
    if size is not None and size <= 0:
        return []

    # 1) 构建服务器 -> 窗口 列表映射，从 Resource.annotations / _meta 读取 priority 与 fullscreen，
    #    保留原始序号以确定"第一个 fullscreen"。同时过滤无内容的资源（contents 为空时跳过）。
    # Build server->windows mapping; read priority/fullscreen from Resource.annotations/_meta and
    # keep original index to pick the first fullscreen.
    grouped: dict[SERVER_NAME, list[tuple[Resource, float, bool, int, ReadResourceResult]]] = {}
    for idx, (server, res, detail) in enumerate(windows):
        try:
            contents = detail.contents
            if not contents or (isinstance(contents, list) and len(contents) == 0):
                # 无内容的窗口不进入桌面组织 / skip windows without contents
                continue
        except Exception:
            # detail 异常则保守丢弃 / drop on error
            continue
        # URI 仅作合法性守卫（必须是 window:// 协议），不再读取 query 中的元数据
        # URI is only a scheme guard; no metadata is read from query
        if not is_window_uri(res.uri):
            continue
        _check_audience(res)
        prio = _read_priority(res)
        fullscreen = _read_fullscreen(res)
        grouped.setdefault(server, []).append((res, prio, fullscreen, idx, detail))

    # 2) 服务器优先级：根据最近工具调用历史，倒序去重
    recent_servers: list[SERVER_NAME] = []
    seen: set[SERVER_NAME] = set()
    for rec in reversed(history):  # 最近在前
        srv = rec.get("server")
        if srv in grouped and srv not in seen:
            seen.add(srv)
            recent_servers.append(srv)

    # 其余服务器（未在历史中出现）按名称稳定排序追加
    remaining = sorted([s for s in grouped.keys() if s not in seen])
    server_order: list[SERVER_NAME] = recent_servers + remaining

    # 3) 每个服务器内按 priority 降序排序
    for srv in server_order:
        grouped[srv].sort(key=lambda x: x[1], reverse=True)

    # 4) 组装按服务器顺序的窗口列表，处理 fullscreen 规则
    # 渲染函数：将 URI 与内容一起渲染到 Desktop 字符串
    # Render function: combine URI and textual contents into Desktop string
    def _render(ri: Resource, di: ReadResourceResult) -> str:
        try:
            parts: list[str] = []
            for block in di.contents or []:
                if isinstance(block, TextResourceContents):
                    if isinstance(block.text, str) and block.text:
                        parts.append(block.text)
                elif isinstance(block, BlobResourceContents):
                    logger.warning("当前桌面环境尚不支持Blob内容")
                else:
                    logger.error("未成功识别当前Window内容")
            body = "\n\n".join(parts).strip()
            return f"{str(ri.uri)}\n\n{body}" if body else str(ri.uri)
        except Exception as e:
            logger.error(f"发生未知异常: {e}", exc_info=True)
            return str(ri.uri)

    result: list[str] = []
    cap = size if size is not None else float("inf")
    for srv in server_order:
        items = grouped.get(srv, [])
        if not items:
            continue
        if len(result) >= cap:
            break
        # 若存在 fullscreen -> 选择“第一个出现的 fullscreen”（按原始 windows 序号最小）
        fullscreen_items = [(res, prio, fs, idx, det) for (res, prio, fs, idx, det) in items if fs]
        if fullscreen_items:
            res, _, _, _, det = min(fullscreen_items, key=lambda x: x[3])
            result.append(_render(res, det))
            continue  # 该服务器仅加入这一个，转下一个服务器

        # 否则按优先级加入多条直到 cap
        for res, _, _, _, det in items:
            if len(result) >= cap:
                break
            result.append(_render(res, det))

    return result
