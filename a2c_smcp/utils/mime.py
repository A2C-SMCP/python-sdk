# -*- coding: utf-8 -*-
# filename: mime.py
# @Time    : 2026/06/12
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
SKILL 资源 MIME 推断与「文本 MIME」判据（单一权威）/ Deterministic MIME inference for SKILL resources.

协议依据 / Protocol: a2c-smcp-protocol docs/specification/skill.md §6.4「mime_type 确定性与「文本 MIME」判据」
                      （refs A2C-SMCP/a2c-smcp-protocol#6）。

存在意义 / Why this module：
    §6.4(1) 要求 SKILL 子资源 ``mime_type`` 推断 **MUST** 确定、**MUST NOT** 依赖随宿主环境而变的
    OS MIME 注册表 / 系统库（如 ``mimetypes.guess_type``）。本模块以实现内置、与宿主无关的
    「扩展名 → MIME」映射（§6.4(3)）+ 引用既有标准的「文本 MIME」判据（§6.4(2)，RFC 2046 /
    RFC 6839 / RFC 9512 + WHATWG mimesniff），为 Computer（铸造期 ``is_text`` 路由 + wire
    ``mime_type``）与 Agent（``get_skill`` 自动 drain 门）提供**唯一**权威，杜绝跨 OS / 跨 SDK /
    跨决策点的判定漂移。
    Single source of truth for textuality so the Computer mint path and the Agent drain gate cannot
    disagree, and so the same resource yields the same ``mime_type`` on any OS / any SDK.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# §6.4(3) 最小确定性扩展名映射基线（规范性 SHOULD 表，MUST 至少内置）+ 常见二进制扩展名（准确性增强：
# 避免 wire ``mime_type`` 退化为 octet-stream；§6.4(1) 的确定性约束对二进制同样适用）。
# 键为小写扩展名（含前导点）。未登记扩展名一律回退 :data:`FALLBACK_MIME`（仍确定）。
# Baseline text exts (spec §6.4(3), MUST) + common binary exts (keep wire mime accurate, no host dep).
EXT_TO_MIME: dict[str, str] = {
    # —— 文本基线（§6.4(3)，MUST）/ Text baseline (spec §6.4(3)) ——
    ".md": "text/markdown",  # RFC 7763
    ".markdown": "text/markdown",  # RFC 7763
    ".txt": "text/plain",  # RFC 2046
    ".json": "application/json",  # RFC 8259
    ".xml": "application/xml",  # RFC 7303
    ".yaml": "application/yaml",  # RFC 9512
    ".yml": "application/yaml",  # RFC 9512
    ".toml": "application/toml",  # IANA, 2024-10-21
    ".rst": "text/x-rst",  # freedesktop 事实标准（无正式 IANA 注册）
    # —— 常见二进制（准确性增强；未列出者回退 octet-stream，仍确定）/ Common binary (accuracy) ——
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/vnd.microsoft.icon",
    ".svg": "image/svg+xml",  # 注：+xml 后缀 → :func:`is_text_mime` 判为文本（SVG 本即文本 XML）
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tar": "application/x-tar",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}

# 未登记扩展名 / 无扩展名的回退 MIME / Fallback for unknown or missing extension.
FALLBACK_MIME = "application/octet-stream"

# §6.4(2) 已 IANA 注册的文本类 ``application/*`` essence 白名单（非 ``text/*`` 但属文本）。
# ``application/x-yaml`` 为 §6.4 规范名 ``application/yaml``（RFC 9512）出现前的事实别名，保留兼容。
# Textual application/* essences (per spec §6.4(2)); x-yaml kept as a legacy alias of application/yaml.
TEXT_MIME_ESSENCES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
    },
)

# §6.4(2) 结构化语法后缀（可解析为对应文本格式 → 文本）/ Structured syntax suffixes implying text.
# RFC 6839（+json / +xml）、RFC 9512（+yaml）。
_TEXT_STRUCTURED_SUFFIXES: tuple[str, ...] = ("+json", "+xml", "+yaml")


def guess_mime(filename: str) -> str:
    """按文件名扩展名推断 MIME（§6.4(1) 确定性：**绝不**调用 ``mimetypes`` / 宿主系统库）。

    纯查表：命中 :data:`EXT_TO_MIME` → 规范 MIME；未命中（含无扩展名）→ :data:`FALLBACK_MIME`。
    结果只取决于扩展名（大小写不敏感），跨 OS / 跨 SDK 一致——这正是 #105 的根因修复点
    （``mimetypes.guess_type`` 依赖宿主注册表，对 ``.md`` / ``.yaml`` 等在 macOS / 旧 Python 返回 None）。

    Args:
        filename: 文件名或包内相对路径（仅取末段扩展名）。

    Returns:
        规范 MIME 字符串（确定，与宿主无关）。
    """
    suffix = PurePosixPath(filename).suffix.lower()
    return EXT_TO_MIME.get(suffix, FALLBACK_MIME)


def is_text_mime(mime: str) -> bool:
    """是否「文本 MIME」（§6.4(2) 判据，三条任一成立即文本）。

    - top-level type 为 ``text``（RFC 2046 §4.1）；或
    - structured syntax suffix ∈ {``+json``, ``+xml``, ``+yaml``}（RFC 6839 / RFC 9512）；或
    - essence ∈ :data:`TEXT_MIME_ESSENCES`（``application/json|xml|yaml|toml|javascript``）。

    与 WHATWG MIME Sniffing 对「JSON / XML MIME type」的定义同构（后缀 ∨ essence 白名单）。不满足任一
    → 二进制（铸 ``blob_handle``）。供 Computer inline 路由与 Agent ``get_skill`` 自动 drain 门**共用**，
    保证「同一 MIME 在两端判定一致」。大小写不敏感；忽略 ``;`` 后参数（如 ``text/markdown; charset=utf-8``）。

    Args:
        mime: MIME 字符串（可含参数）。

    Returns:
        是否文本类。
    """
    essence = mime.split(";", 1)[0].strip().lower()
    if essence.startswith("text/"):
        return True
    if essence.endswith(_TEXT_STRUCTURED_SUFFIXES):
        return True
    return essence in TEXT_MIME_ESSENCES
