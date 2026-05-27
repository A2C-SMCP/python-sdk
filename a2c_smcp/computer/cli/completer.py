# -*- coding: utf-8 -*-
# filename: completer.py
# @Time    : 2026/05/27
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
prompt_toolkit Tab 补全 / prompt_toolkit Tab completion（S15，#68）。

设计依据 / Design: python-sdk docs/design-0.2.1-cli-marketplace-ux.md §10.3。

按上下文补全：行首 namespace / 顶层命令词；namespace 后子命令词；命令后动态名（marketplace 名取
``known_marketplaces.json``、skill 名取 ``Computer.get_skills``）；``--flag`` 位置补 flag 集。命令分类法是
:mod:`help` 的单一事实源。plugin / settings 子命令作骨架先行补全，其**动态名**（available plugins 等）由 #69 填。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from typing import Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from a2c_smcp.computer.cli.help import FLAGS, ROOT_WORDS, SUBCOMMANDS
from a2c_smcp.utils.logger import get_logger

logger = get_logger(__name__)

# 触发动态名补全的 (namespace, subcommand) / commands whose positional arg is a dynamic name。
_DYNAMIC_MARKETPLACE = {("marketplace", s) for s in ("info", "remove", "refresh", "set")}
_DYNAMIC_SKILL = {("skill", "info")}
# plugin 位置参数补 installed plugin id（取 installed_plugins.json）；install 补 available 暂不实现（返 []）。
_DYNAMIC_PLUGIN = {("plugin", s) for s in ("uninstall", "enable", "disable", "info")}
# settings get/set 的 key 补已知 settings.json 字段名（schema FIELD_* 集，静态）。
_DYNAMIC_SETTINGS = {("settings", "get"), ("settings", "set")}
# 已知 settings.json 顶层字段（与 schema.py FIELD_* 对齐；completer 静态名集，无需运行时反射）。
_SETTINGS_KEYS = (
    "extraKnownMarketplaces", "enabledPlugins", "strictKnownMarketplaces", "trustedMarketplaces",
    "blockedMarketplaces", "enableAllProjectMcpServers", "enabledMcpjsonServers", "disabledMcpjsonServers",
    "allowedMcpServers", "deniedMcpServers", "permissions",
)


class A2CCompleter(Completer):
    """REPL Tab 补全器，持 ``Computer`` 引用以取动态名 / REPL completer holding a Computer for dynamic names。"""

    def __init__(self, comp: Any) -> None:
        self._comp = comp

    def get_completions(self, document: Document, complete_event: Any) -> Iterator[Completion]:
        text = document.text_before_cursor
        words = text.split()
        trailing_space = text == "" or text.endswith(" ")
        completing = "" if trailing_space else (words[-1] if words else "")
        prev = words if trailing_space else words[:-1]
        n = len(prev)

        if n == 0:  # 行首：namespace + 顶层命令 / root words
            yield from self._match(ROOT_WORDS, completing)
            return

        ns = prev[0].lower()
        if n == 1:  # namespace 后：子命令词 / subcommand words
            yield from self._match(SUBCOMMANDS.get(ns, []), completing)
            return

        sub = prev[1].lower()
        if completing.startswith("--"):  # flag 位置 / flag position
            yield from self._match(FLAGS.get((ns, sub), []), completing)
            return

        # 位置参数：动态名 / positional dynamic name
        yield from self._match(self._dynamic_names(ns, sub), completing)

    def _dynamic_names(self, ns: str, sub: str) -> list[str]:
        try:
            if (ns, sub) in _DYNAMIC_MARKETPLACE:
                from a2c_smcp.computer.settings.store import load_known_marketplaces

                return list((load_known_marketplaces(self._comp.skill_home, os.environ).get("marketplaces") or {}).keys())
            if (ns, sub) in _DYNAMIC_SKILL:
                return [str(r.get("name")) for r in self._comp.get_skills() if r.get("name")]
            if (ns, sub) in _DYNAMIC_PLUGIN:
                from a2c_smcp.computer.settings.store import load_installed_plugins

                return list((load_installed_plugins(self._comp.skill_home, os.environ).get("plugins") or {}).keys())
            if (ns, sub) in _DYNAMIC_SETTINGS:
                return list(_SETTINGS_KEYS)
        except Exception as e:  # 动态取名失败不应打断补全；记 DEBUG 便于排障（含编程错）/ never break completion
            logger.debug("completer: dynamic-name lookup for (%s %s) failed (%s)", ns, sub, e)
            return []
        return []

    @staticmethod
    def _match(candidates: Iterable[str], prefix: str) -> Iterator[Completion]:
        for cand in candidates:
            if cand.startswith(prefix):
                yield Completion(cand, start_position=-len(prefix))
