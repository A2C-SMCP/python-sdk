# -*- coding: utf-8 -*-
# filename: git_url.py
# @Time    : 2026/11/16
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm
"""
git URL 展示脱敏单一权威（python-sdk#261）/ Git URL display redaction, single source of truth.

契约来源 / Contract source（**非协议**，SDK 自治；与 rust-sdk 共用同一展示契约）:
    - rust-sdk#273 维护侧决策（2026-11-16，issuecomment-6908841070）：
      已校验 source 的展示面（CLI list / info / add）用 known-source display——保留 scheme/host/path，
      scp-like 显示 ``host:path``（不保留 ``user@``），去除 userinfo / query / fragment。
    - rust-sdk#154（fail-closed）：错误 / 诊断 / 日志不得回显 git URL 的 username / password / token；
      无法安全呈现 → 同一占位符 :data:`REDACTED_GIT_SOURCE`。
    - rust-sdk PR #277（``fix/273-scp-like-display``）``settings/redaction.rs`` 与
      ``skills/staging.rs::sanitize_git_stderr`` 为展示契约与散文算法的对拍源（散文 token 边界 = **仅空白**；
      外层引号闭合由 ``split_outer_wrapper`` 判定；scp-like 散文由 ``redact_scp_like_sources`` 处理）。
      **序列化层 best-effort**（非逐字节），差异清单见 :func:`git_url_for_display`。

两模式 / Two modes：
    - :func:`known_git_source_for_display` —— 已校验 source 的**展示面**（可读性优先，仍去凭据）。
    - :func:`git_url_for_display` —— 错误 / 诊断 / 日志的**严格 fail-closed**；scp-like 与一切
      无法安全解析的形态一律占位符。
    - :func:`redact_git_urls_in_text` —— 对 git stderr / 异常消息等**散文**做 URL token 级脱敏。
    - :func:`sanitize_git_text` —— :func:`redact_git_urls_in_text` 的 git 专用前置：先按本次 git 命令的
      已知 URL 参数（含 scp-like 端点前缀）做 verbatim 替换，再走通用散文脱敏（对齐 rust
      ``sanitize_git_stderr``）。

与 rust 的一处有意差异 / Deliberate stricter divergence：受支持 scheme 的 opaque 形态
（如 ``ssh:alice:PW@host``，缺 ``//``）rust PR #277 已列为既有待修泄漏点，本模块按
「非 file scheme 缺 host → 占位符」直接 fail-closed，不复刻该泄漏；展示值含非可视 ASCII
（空格 / 控制字符 / 非 ASCII）时亦直接占位符，避免终端转义注入。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit

# 无法安全呈现时的统一占位符（与 rust 逐字节一致）/ Placeholder literal shared with rust.
REDACTED_GIT_SOURCE = "<redacted-git-source>"

# 允许展示的 scheme（与 rust ``git_url_for_display`` 白名单一致）/ Supported schemes.
_SUPPORTED_SCHEMES = frozenset({"ssh", "git", "http", "https", "file"})

# 默认端口（与 url crate 序列化一致：http 80 / https 443 不输出）/ Default ports dropped on display.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# scp-like ``user@host:path``：user / host 字符集与 rust 一致；path 先取到首个 ?/# 前。
_SCP_LIKE_RE = re.compile(r"^(?P<user>[A-Za-z0-9._+-]+)@(?P<host>[A-Za-z0-9._-]+):(?P<path>.+)$", re.DOTALL)

# 散文 scheme 识别字符（rust ``is_scheme_byte``）/ scheme byte set.
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")

# 外层引号与「闭合后可安全吞掉」的后缀标点（rust ``wrapper_suffix_boundary``）/ wrapper chars.
_QUOTE_CHARS = frozenset("'\"`")
_WRAPPER_SUFFIX_CHARS = frozenset("'\"`:;,.!?)]}")

# 已知 git 参数的 scp-like 形态（rust ``SSH_SCP_LIKE_RE``，Unicode 感知）/ scp-like git arg shape.
_SCP_ARG_RE = re.compile(r"^[\w.+-]+@[\w.-]+:.+$", re.UNICODE)


def _ascii_graphic(text: str) -> bool:
    """是否全为可视 ASCII（0x21–0x7E）/ Whether every char is printable ASCII."""
    return all(0x21 <= ord(ch) <= 0x7E for ch in text)


def _scp_like_source_for_display(raw: str) -> str | None:
    """
    scp-like 合法源的展示值 ``host:path``；不满足 rust 白名单 → ``None``（交严格模式）/ scp-like display.

    rust ``scp_like_source_for_display`` 对拍：user 非空且 ``[A-Za-z0-9._+-]``；host 非空且
    ``[A-Za-z0-9._-]``；path 非空、不以 ``//`` 开头、不含 ``@ : %``、全可视 ASCII；path 在首个
    ``?`` / ``#`` 处截断（凭据类 query/fragment 不展示）。
    """
    text = raw.strip()
    match = _SCP_LIKE_RE.match(text)
    if match is None:
        return None
    path = re.split(r"[?#]", match.group("path"), maxsplit=1)[0]
    if (
        not path
        or path.startswith("//")
        or any(ch in path for ch in "@:%")
        or not _ascii_graphic(path)
    ):
        return None
    return f"{match.group('host')}:{path}"


def known_git_source_for_display(raw: object) -> str:
    """
    已校验 source 的展示值（list / info / add 表格、JSON、确认提示）/ Known-source display value.

    scp-like 优先走 :func:`_scp_like_source_for_display`（``host:path``），其余回退严格模式。
    **仅用于展示，不可用于 re-clone**；存储与 git 调用必须使用原始 URL。
    ``raw`` 接受任意类型（``known_marketplaces.json`` 可能被手工编辑）；非 ``str`` → 占位符。
    """
    if not isinstance(raw, str):
        return REDACTED_GIT_SOURCE
    known = _scp_like_source_for_display(raw)
    if known is not None:
        return known
    return git_url_for_display(raw)


def git_url_for_display(raw: object) -> str:
    """
    严格 fail-closed 展示值（错误 / 诊断 / 日志）/ Strict display for errors, diagnostics and logs.

    规则（对齐 rust ``git_url_for_display``）：
    - 仅接受 ``ssh|git|http|https|file``；opaque 形态（缺 ``//``）、解析失败、非 file scheme 缺 host、
      非法端口 → :data:`REDACTED_GIT_SOURCE`；
    - 去 userinfo（username + password）、query、fragment；保留 scheme / host / port / path；
    - scheme 与 host 小写；http 80 / https 443 默认端口不输出；空 path 补 ``/``（url crate 序列化）；
    - scp-like 在严格模式一律占位符（``Url::parse`` 失败；rust 行为）；
    - path 按 url crate 序列化 percent-encode（空格 / C0 控制字符 / 非 ASCII 均转义，防终端注入）；
      host 走 IDNA；host 仍含非可视 ASCII（空格 / 控制字符）→ 占位符。序列化为 **best-effort 对齐**
      rust ``Url::to_string()``（非逐字节一致），已知差异**例如**：不做 dot-segment 归一 / host ``%xx``
      归一；path 保留字符集（``|`` / ``^`` 等）与 host IDNA 方言可能不同；反斜杠 host 解析以 Python
      （与 git 实际连接一致）为准。清单为示例，非穷举。
    """
    if not isinstance(raw, str):
        return REDACTED_GIT_SOURCE
    text = raw.strip()
    if "://" not in text:
        return REDACTED_GIT_SOURCE
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if scheme not in _SUPPORTED_SCHEMES:
            return REDACTED_GIT_SOURCE
        host = parts.hostname
        port = parts.port
    except ValueError:
        return REDACTED_GIT_SOURCE
    if scheme != "file" and not host:
        return REDACTED_GIT_SOURCE
    try:
        host_for_display = host or ""
        if host_for_display and ":" not in host_for_display:  # IPv6 字面量已是 ASCII
            host_for_display = host_for_display.encode("idna").decode("ascii")
        host_repr = f"[{host_for_display}]" if host_for_display and ":" in host_for_display else host_for_display
        if port is not None and port != _DEFAULT_PORTS.get(scheme):
            netloc = f"{host_repr}:{port}"
        else:
            netloc = host_repr
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~[]")
        return_value = urlunsplit((scheme, netloc, path, "", ""))
    except UnicodeError:
        # 孤立代理项 / 非法 IDNA 等：无法安全序列化 → 占位符（fixture 来自手编 ledger / JSON 转义）。
        return REDACTED_GIT_SOURCE
    if not (_ascii_graphic(scheme) and _ascii_graphic(netloc)):
        return REDACTED_GIT_SOURCE
    return return_value


def _split_outer_wrapper(prefix: str, candidate: str) -> tuple[str, str]:
    """
    拆分 URL 的外层引号闭合符（rust ``split_outer_wrapper``）/ Peel an outer quoting wrapper.

    仅当 scheme 前紧邻引号（``' " ` ``）时，取 candidate 中**最后一个**同类引号且其后全为外层标点者为
    闭合符——URL userinfo / query 自身允许引号，按第一个引号截断会泄漏后半段凭据。
    """
    if not prefix or prefix[-1] not in _QUOTE_CHARS:
        return candidate, ""
    wrapper = prefix[-1]
    for index in range(len(candidate) - 1, -1, -1):
        if candidate[index] == wrapper and all(ch in _WRAPPER_SUFFIX_CHARS for ch in candidate[index:]):
            return candidate[:index], candidate[index:]
    return candidate, ""


def _redact_scheme_urls(text: str) -> str:
    """散文中的 ``scheme://`` token：严格展示（嵌套 ``://`` → 占位符）/ Redact scheme URLs in prose."""
    out: list[str] = []
    rest = text
    while True:
        marker = rest.find("://")
        if marker < 0:
            out.append(rest)
            break
        scheme_start = marker
        while scheme_start > 0 and rest[scheme_start - 1] in _SCHEME_CHARS:
            scheme_start -= 1
        while scheme_start < marker and not rest[scheme_start].isalpha():
            scheme_start += 1
        scheme = rest[scheme_start:marker]
        if not scheme or any(ch not in _SCHEME_CHARS for ch in scheme):
            out.append(rest[: marker + 3])
            rest = rest[marker + 3:]
            continue
        out.append(rest[:scheme_start])
        token = rest[scheme_start:]
        end = len(token)
        for index, ch in enumerate(token):
            if ch.isspace():
                end = index
                break
        candidate, suffix = _split_outer_wrapper(rest[:scheme_start], token[:end])
        first_marker_end = marker - scheme_start + 3
        if "://" in candidate[first_marker_end:]:
            out.append(REDACTED_GIT_SOURCE)
        else:
            out.append(git_url_for_display(candidate))
        out.append(suffix)
        rest = token[end:]
    return "".join(out)


def _redact_scp_like_sources(text: str) -> str:
    """
    散文中的 scp-like token 整体占位符（rust ``redact_scp_like_sources`` 逐段移植）/ Hide scp-like tokens.

    token 边界 = 仅空白（RFC 3986 userinfo / query 允许 ``, ; ' ( )`` 等 sub-delims，按标点截断会泄漏
    后半段凭据）。缩写端点（``user@host:`` 后接空格）、possessive（``user@host's``）、Unicode 端点均覆盖。
    """
    out: list[str] = []
    rest = text
    while True:
        at = rest.find("@")
        if at < 0:
            out.append(rest)
            break
        start = 0
        for index in range(at - 1, -1, -1):
            if rest[index].isspace():
                start = index + 1
                break
        end = len(rest)
        for index in range(at + 1, len(rest)):
            if rest[index].isspace():
                end = index
                break
        if "://" in rest[start:at]:
            # 已由 `_redact_scheme_urls` 处理过的 scheme token（含 path 里的 `@`，如
            # `file:///home/alice@corp/repo.git`）：再按 scp-like 处理会把**展示安全**的 URL 吞成占位符。
            # 判据只看 `@` **之前**是否出现 `://`——`alice:PW@host,https://…` 这类 `@` 在前、URL 在后的
            # token 仍是 scp-like 凭据，必须继续脱敏。
            out.append(rest[: at + 1])
            rest = rest[at + 1 :]
            continue
        userinfo = rest[start:at]
        destination = rest[at + 1 : end]
        colon = destination.find(":")
        possessive = "'s" in destination
        if start == at or not destination or colon == 0 or (colon < 0 and ":" not in userinfo and not possessive):
            out.append(rest[: at + 1])
            rest = rest[at + 1 :]
            continue
        if start < at:
            out.append(rest[:start])
            out.append(REDACTED_GIT_SOURCE)
            rest = rest[end:]
        else:
            out.append(rest[: at + 1])
            rest = rest[at + 1 :]
    return "".join(out)


def redact_git_urls_in_text(text: str) -> str:
    """
    对任意文本（git stderr / 异常消息 / 诊断）做 git URL token 级脱敏 / Redact git URLs in prose.

    先按 ``scheme://`` token 走严格展示（嵌套 ``://`` 整体占位符），再把残余 scp-like token 整体替换为
    :data:`REDACTED_GIT_SOURCE`。无 ``:`` 的邮箱地址（如 ``alice@example.com``）不匹配，不会误伤。
    """
    return _redact_scp_like_sources(_redact_scheme_urls(text))


# 会被转义成可见形式的字符类别（Cc = 控制字符含 ESC/BEL/换行；Cs = 孤立代理项）。#263：终端转义注入
# （OSC 52 改写剪贴板）与日志行伪造的根源；Cf（方向覆盖等）与 rust 口径一致不处理。
_UNSAFE_CHAR_CATEGORIES = frozenset({"Cc", "Cs"})

# userinfo 形态片段：`user:secret@`（要求 `@` 前存在 `:`，避免误伤 `alice@corp` 这类合法路径名）。
# #263 复审：PAT / username-only 的 `TOKEN@host` 也是凭据——**但只在 URL 语境下**（路径里出现 scheme 残形
# `:/` 或 `://`）才遮蔽，否则 `alice@corp` 这类合法文件名会被误伤。percent-encoded 冒号（`alice%3Apw@`）
# 先 decode 再判定。
_USERINFO_SEGMENT_RE = re.compile(r"[^\s/\\:@]+:[^\s/\\@]+@")
# scheme 段尾（`x=https:` / `ssh:`）：要求 scheme 名 ≥2 字符，避免把 Windows 盘符 `C:` 当 scheme。
_SCHEME_TAIL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]+:$")


def escape_unsafe_chars(text: str, *, json_safe: bool = False) -> str:
    """
    把控制字符 / 孤立代理项转义为可见形式 / Escape control and surrogate code points for display.

    ``json_safe=True`` 时统一用 ``\\uXXXX``（``\\xNN`` 不是合法 JSON 转义——DEL 0x7f / C1 0x80-0x9f
    不会被 :func:`json.dumps` 转义，混进 JSON 形态的 raw 会产出无法解析的文本）。
    """
    if not any(unicodedata.category(ch) in _UNSAFE_CHAR_CATEGORIES for ch in text):
        return text
    out: list[str] = []
    for ch in text:
        if unicodedata.category(ch) in _UNSAFE_CHAR_CATEGORIES:
            code = ord(ch)
            out.append(f"\\u{code:04x}" if json_safe or code > 0xFF else f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _is_credential_segment(segment: str, *, authority_context: bool) -> bool:
    """
    段是否携带凭据 / Whether a path segment carries credentials.

    判定（#263 四轮）：percent-decode 后命中 ``user:secret@``；或该段处于 URL **authority** 位置（紧跟
    scheme 残形段，如 ``x=https:``）且含 ``@``——PAT / username-only 的 ``TOKEN@host`` 也算凭据，
    而路径里的普通 ``@``（``file:///home/alice@corp/repo.git``、``repo@2.git``）保留。
    """
    if _USERINFO_SEGMENT_RE.search(unquote(segment)) is not None:
        return True
    return authority_context and "@" in segment


def sanitize_path_for_diagnostics(path: str) -> str:
    """
    诊断用路径脱敏：逐段遮蔽携带凭据的片段 / Mask credential-bearing path segments for diagnostics.

    仅用于**展示**（validator 的 ``file`` / ``checked`` / ``message``、日志里的落盘路径），**不得**用于
    文件系统访问。规则（#263 四轮复审修正）：

    - 控制字符 / 孤立代理项先按 :func:`escape_unsafe_chars` 转义（仓库可控目录名不得注入终端序列）；
    - 分隔符 ``/`` 与 ``\\`` 都视为段边界（Windows 路径不得整条被吞）；
    - 段头（``?`` / ``#`` 之前的部分）先判凭据：命中则**整段**替换——否则 ``alice:pw@host?token=x``
      会把 userinfo 留在截断后的前缀里；
    - 一旦出现 ``?`` / ``#``（query / fragment 起点）→ 从该字符起把**余下整条路径**遮蔽（query 值里
      可以再出现分隔符，逐段判定会漏掉尾段）；
    - authority 位置（紧跟 scheme 残形段）的 ``TOKEN@host`` 按凭据处理；路径段内普通 ``@`` 保留。

    合法 kebab / 普通路径（含 ``alice@corp``、Windows 路径）逐字保留。
    """
    escaped = escape_unsafe_chars(path)
    if not any(marker in escaped for marker in ("@", "?", "#")):
        return escaped
    parts = re.split(r"([/\\])", escaped)
    out: list[str] = []
    tail_masked = False
    authority_next = False
    for index, part in enumerate(parts):
        if index % 2 == 1:  # 分隔符原样保留
            out.append(part)
            continue
        if tail_masked:
            if part:
                out.append("<redacted-source>")
            continue
        if not part:
            out.append(part)
            continue
        cut = min((pos for pos in (part.find("?"), part.find("#")) if pos >= 0), default=-1)
        head = part[:cut] if cut >= 0 else part
        if _is_credential_segment(head, authority_context=authority_next):
            out.append("<redacted-source>")
            tail_masked = cut >= 0
            authority_next = False
            continue
        if cut >= 0:
            out.append(head + "<redacted-source>")
            tail_masked = True
            authority_next = False
            continue
        out.append(part)
        # 该段以 scheme 形态结尾（`x=https:` / `ssh:`，scheme 名 ≥2 字符）→ 下一个非空段处于
        # authority 位置。Windows 盘符 `C:` 只有单字符，不会命中（否则 `C:\alice@corp` 会被误遮）。
        authority_next = _SCHEME_TAIL_RE.search(part) is not None
    return "".join(out)


# 路径 token 的判定（供 :func:`sanitize_text_for_diagnostics` 逐 token 使用）：带 URL 残形 / userinfo 形态，
# 或「含分隔符且含 query/fragment 起点」的 token 才按路径处理；散文其余部分（含换行与可读前缀）逐字保留。
_PATH_TOKEN_RE = re.compile(r":[/\\]|://")
_USERINFO_TOKEN_RE = re.compile(r"[^\s/\\:@]+:[^\s/\\@]+@")


def sanitize_text_for_diagnostics(text: str) -> str:
    """
    诊断**散文**的路径级脱敏：逐 token 判定，仅遮蔽 URL 残形 / 路径 + query 形态的 token / Token-level.

    非路径 token 仍过 :func:`escape_unsafe_chars`（ESC / 孤立代理项等），但空白分隔符原样保留——
    换行与缩进因此不受影响。
    """
    out: list[str] = []
    for token in re.split(r"([ \t\n]+)", text):
        if not token or token in (" ", "\t", "\n") or set(token) <= {" ", "\t", "\n"}:
            out.append(token)
            continue
        looks_like_path = (
            _PATH_TOKEN_RE.search(token) is not None
            or _USERINFO_TOKEN_RE.search(unquote(token)) is not None
            or (("/" in token or "\\" in token) and ("?" in token or "#" in token))
        )
        out.append(sanitize_path_for_diagnostics(token) if looks_like_path else escape_unsafe_chars(token))
    return "".join(out)


def sanitize_value_for_diagnostics(value: str) -> str:
    """
    诊断用**单个不可信值**脱敏（非散文）/ Sanitize a single untrusted value for diagnostics.

    与 :func:`sanitize_text_for_diagnostics` 的唯一区别：**先** :func:`escape_unsafe_chars`。散文口径按
    设计保留空白与换行（多行异常消息的可读性），但单个原始值里的换行可以伪造日志行，还会把 userinfo /
    scp-like 切成两段而漏遮（``alice:pw\\n@host`` 两半都不命中任何规则）。转义后值退回单 token，后续
    散文轴（scp-like 与 ``scheme:`` opaque 形态只有它能识别）与 path 轴才按整体判定。
    Escapes control characters first: a single value is not prose — newlines forge log lines and split
    userinfo/scp-like tokens so neither half matches.

    顺序不可交换（#281 复审）：先 path 再转义会把 ``x/r.git?token=…`` 的尾部截断后仍留在转义前的 token 里。
    """
    return sanitize_text_for_diagnostics(redact_git_urls_in_text(escape_unsafe_chars(value)))


def sanitize_git_text(text: str, args: Sequence[str]) -> str:
    """
    git 输出专用脱敏：先按已知 URL 参数 verbatim 替换，再走通用散文脱敏 / Sanitize git output.

    对齐 rust ``sanitize_git_stderr``：git stderr 可能把 clone URL 回显为缩写端点（``user@host:``，后接
    ``Permission denied`` / ``password:``），通用 token 规则抓不到；故先用本次命令的 URL 参数（含 scp-like
    端点前缀）做 verbatim 替换，再走 :func:`redact_git_urls_in_text` 兜底。
    """
    sanitized = text
    for raw_url in args:
        if not isinstance(raw_url, str):
            continue
        if "://" not in raw_url and _SCP_ARG_RE.match(raw_url) is None:
            continue
        display = git_url_for_display(raw_url)
        sanitized = sanitized.replace(raw_url, display)
        if _SCP_ARG_RE.match(raw_url) is not None:
            endpoint = raw_url.split(":", 1)[0]
            sanitized = sanitized.replace(endpoint, display)
    return redact_git_urls_in_text(sanitized)
