# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from collections.abc import Mapping, Sequence
from typing import TypeAlias

AttributeValue = str | bool | int | float | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]
Attributes = Mapping[str, AttributeValue] | None
AttributesAsKey = tuple[
    tuple[
        str,
        str | bool | int | float | tuple[str | None, ...] | tuple[bool | None, ...] | tuple[int | None, ...] | tuple[float | None, ...],
    ],
    ...,
]
TOOL_NAME: TypeAlias = str
SERVER_NAME: TypeAlias = str
# BundleID 模型（协议 0.3.0，a2c-smcp-protocol#15）：MCP Server 唯一身份 + 聚合暴露名。
# BUNDLE_ID：MCP Server 软件级唯一标识；EXPOSED_TOOL_NAME：暴露给 LLM 的 `{bundle_id}__{alias??原始名}`。
BUNDLE_ID: TypeAlias = str
EXPOSED_TOOL_NAME: TypeAlias = str
