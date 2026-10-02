"""
* 文件名: types
* 作者: JQQ
* 创建日期: 2025/9/29
* 最后修改日期: 2025/9/29
* 版权: 2023 JQQ. All rights reserved.
* 依赖: None
* 描述: Server端类型定义 / Server-side type definitions
"""

from typing import Literal, TypeAlias

from typing_extensions import TypedDict

# 类型别名定义 / Type aliases
OFFICE_ID: TypeAlias = str
SID: TypeAlias = str
# 名字注册表键 ``(office_id, role, name)``（#215，协议 room-model.md §房内名字唯一性）：唯一性是**房内同 role**
# 的，跨房同名 / 同名异 role 均允许。元组键天然无字符串拼接歧义。
# Name-registry key ``(office_id, role, name)`` (#215): uniqueness is per room and per role.
NAME_KEY: TypeAlias = tuple[OFFICE_ID, str, str]
# 席位键 ``(office_id, role)``（#230，协议 room-model.md §成员类型「每 role 一席」）：房内每个 role 至多一个会话。
# 席位是**准入**的权威判据（4101），名字注册表只承担路由解析；同 role 的第二个会话无论同名与否都先撞席位。
# Seat key ``(office_id, role)`` (#230, "one seat per role"): the admission authority (4101); the name
# registry above only serves routing.
SEAT_KEY: TypeAlias = tuple[OFFICE_ID, str]


class BaseSession(TypedDict):
    """
    公共会话基类，包含sid和name属性
    Base session class, includes sid and name attributes
    """

    sid: str  # 会话ID / Session ID
    name: str  # 会话名称 / Session name


class ComputerSession(BaseSession):
    """
    Computer会话类型
    Computer session type
    """

    role: Literal["computer"]
    office_id: str


class AgentSession(BaseSession):
    """
    Agent会话类型
    Agent session type
    """

    role: Literal["agent"]
    office_id: str


# 联合类型定义 / Union type definition
Session = ComputerSession | AgentSession
