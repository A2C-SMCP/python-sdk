# -*- coding: utf-8 -*-
# filename: __init__.py
# @Author  : JQQ
# @Software: PyCharm
"""
Computer 侧通用二进制传输 / Generic binary transfer on the Computer side.

模块组成 / Module composition:
  - handle.py     blob_handle 编解码（单层 msgpack + base64url，无 MAC）
  - toolspool.py  tool_call 二进制溢写的内容寻址暂存（``.blobspool/<cid>``）
  - resolver.py   BlobResolver Protocol + 内置 ToolspoolBlobResolver；skill resolver 占位待 #39 接管
  - thresholds.py inline budget / too_large cap / chunk max 默认值 + 环境变量覆盖

设计依据 / Design source: ``docs/design-0.2.1-skill-computer-management.md`` §4.3 / §4.4.
协议依据 / Protocol: ``a2c-smcp-protocol`` ``docs/specification/blob-transfer.md``.
"""

from a2c_smcp.computer.blob.handle import (
    BlobHandleError,
    BlobHandleForbiddenError,
    BlobHandleGoneError,
    BlobHandleInvalidError,
    BlobTooLargeError,
    SkillHandlePayload,
    ToolspoolHandlePayload,
    decode_blob_handle,
    encode_skill_handle,
    encode_toolspool_handle,
)
from a2c_smcp.computer.blob.resolver import (
    BlobResolver,
    ResolvedBlob,
    SkillBlobResolverPending,
    ToolspoolBlobResolver,
)
from a2c_smcp.computer.blob.thresholds import (
    BlobThresholds,
    default_thresholds,
)
from a2c_smcp.computer.blob.toolspool import (
    ToolspoolBlobStore,
)

__all__ = [
    "BlobHandleError",
    "BlobHandleForbiddenError",
    "BlobHandleGoneError",
    "BlobHandleInvalidError",
    "BlobResolver",
    "BlobThresholds",
    "BlobTooLargeError",
    "ResolvedBlob",
    "SkillBlobResolverPending",
    "SkillHandlePayload",
    "ToolspoolBlobResolver",
    "ToolspoolBlobStore",
    "ToolspoolHandlePayload",
    "decode_blob_handle",
    "default_thresholds",
    "encode_skill_handle",
    "encode_toolspool_handle",
]
