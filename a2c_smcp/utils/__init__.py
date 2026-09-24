# -*- coding: utf-8 -*-
# filename: __init__.py.py
# @Time    : 2025/9/29 10:36
# @Author  : JQQ
# @Email   : jqq1716@gmail.com
# @Software: PyCharm

"""
工具模块导出
Export utilities
"""

from .atomic_io import atomic_write_bytes, atomic_write_text, unique_tmp_path
from .cancellation import cancel_entry_snapshot, restore_swallowed_cancel, restores_cancellation
from .mime import EXT_TO_MIME, guess_mime, is_text_mime
from .office import (
    JOIN_VALIDATION_REJECTION_CODES,
    OFFICE_JOIN_TIMEOUT,
    OFFICE_REJOIN_RETRY_BASE_DELAY,
    OFFICE_REJOIN_RETRY_BUDGET,
    OFFICE_REJOIN_RETRY_MAX_DELAY,
    TRANSIENT_JOIN_CONFLICT_CODES,
    JoinOfficeVerdict,
    OfficeMembership,
    build_join_failure_payload,
    is_validation_rejection,
    join_failure_message,
    log_join_rejection,
    parse_join_ack,
    rejoin_retry_delay,
    resolve_join_failure,
)
from .path import is_within, resolve_xdg_first
from .window_uri import WindowURI, is_window_uri

__all__ = [
    "EXT_TO_MIME",
    "JOIN_VALIDATION_REJECTION_CODES",
    "OFFICE_JOIN_TIMEOUT",
    "OFFICE_REJOIN_RETRY_BASE_DELAY",
    "OFFICE_REJOIN_RETRY_BUDGET",
    "OFFICE_REJOIN_RETRY_MAX_DELAY",
    "TRANSIENT_JOIN_CONFLICT_CODES",
    "JoinOfficeVerdict",
    "OfficeMembership",
    "WindowURI",
    "atomic_write_bytes",
    "atomic_write_text",
    "build_join_failure_payload",
    "cancel_entry_snapshot",
    "guess_mime",
    "is_text_mime",
    "is_validation_rejection",
    "is_within",
    "is_window_uri",
    "join_failure_message",
    "log_join_rejection",
    "parse_join_ack",
    "rejoin_retry_delay",
    "resolve_join_failure",
    "resolve_xdg_first",
    "restore_swallowed_cancel",
    "restores_cancellation",
    "unique_tmp_path",
]
