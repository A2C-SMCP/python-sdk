"""
* 文件名: name_registry
* 作者: JQQ
* 创建日期: 2026/9/27
* 最后修改日期: 2026/10/2
* 版权: 2023 JQQ. All rights reserved.
* 依赖: 无
* 描述: 席位表与名字注册表的纯数据原语（sync / async Namespace 共用）
        / Pure seat-table and name-registry primitives shared by both namespaces
"""

from a2c_smcp.exceptions import RoomFullError
from a2c_smcp.server.types import NAME_KEY, OFFICE_ID, SEAT_KEY, SID


class SessionGoneError(RuntimeError):
    """注册时发现 sid 已不在连接中（断连收尾已开始）——拒绝注册，由 ``enter_room`` 的失败收敛兜底。

    不是协议错误码：发起者已断开，ack 无人接收；存在意义只是让「死 sid 占席 / 占名」在构造上不可能
    （否则残留指向死 sid 的席位 ⇒ 该房该 role 永久 ``4101``，v0.5.0 审查 🔴7）。
    Raised when the registering sid is no longer connected; never surfaces as a protocol code.
    """


class RegistryInvariantError(RuntimeError):
    """名字注册表与席位表矛盾（键被**另一** sid 持有）——隔离不变量被破坏，显式 raise（#31：不变量只 raise）。

    「每 role 一席」下，持有 ``(office_id, role, name)`` 键的会话必然持有 ``(office_id, role)`` 席位；入房在
    阶段 1 已占席，故阶段 2 注册名字时键被他人持有**不可能**发生。真发生 ⇒ 注册表损坏 ⇒ ``500``，**不**伪装成
    任何协议拒绝码（``4105`` 已是预留码）。
    The name key is held by another sid although the seat was ours — a broken invariant, surfaced as 500.
    """


def claim_seat(
    seats: dict[SEAT_KEY, SID],
    seats_by_sid: dict[SID, set[SEAT_KEY]],
    seat: SEAT_KEY,
    sid: SID,
    *,
    connected: bool,
) -> bool:
    """**原子**「席位检查 + 占席」（protocol#66：MUST 对同一 ``office_id`` 原子，#230）。

    在 ``enter_room`` **任何副作用之前**调用（协议 注记 4「校验必须先于副作用」）：并发加入同一空房时恰一者
    占到席位，另一者在**动旧房之前**即以 ``4101`` 被拒——竞争输家不会先退掉原房再被拒。

    与 :func:`reserve_name` 同一原子性模型：**无 await、不取锁**，sync 端在注册表锁内调用，async 端依赖零挂起点。
    Atomic seat check-and-claim, run before any membership side effect; same atomicity model as reserve_name.

    一个 sid 可**暂时**持有两个席位：Computer 换房时先占新房席位，阶段 2 退旧房才释放旧席位。

    Returns:
        bool: 本次是否**新**占了该席位（``False`` = 本 sid 早已持有，幂等）。调用方只回滚自己新占的席位。

    Raises:
        SessionGoneError: ``connected`` 为假（sid 已进入断连收尾）。
        RoomFullError: ``4101``，该席位已被其它 sid 占据（同名与否无关）。
    """
    if not connected:
        raise SessionGoneError(f"sid {sid!r} is no longer connected")
    holder = seats.get(seat)
    if holder == sid:
        return False
    if holder is not None:
        raise RoomFullError(seat[1])
    seats[seat] = sid
    seats_by_sid.setdefault(sid, set()).add(seat)
    return True


def release_seats(
    seats: dict[SEAT_KEY, SID],
    seats_by_sid: dict[SID, set[SEAT_KEY]],
    sid: SID,
    office_id: OFFICE_ID | None = None,
) -> list[SEAT_KEY]:
    """释放 sid 持有的席位（**归属守卫**：只删确由本 sid 持有的席位）。

    ``office_id`` 非空时只释放**该房**的席位（``leave_room`` / 入房失败回滚用：换房途中同 sid 还持有另一房的席位）。
    Release the seats held by ``sid``; with ``office_id`` only that office's seat.

    Returns:
        实际释放的席位。
    """
    held = seats_by_sid.get(sid)
    if not held:
        return []
    released: list[SEAT_KEY] = []
    for seat in list(held):
        if office_id is not None and seat[0] != office_id:
            continue
        held.discard(seat)
        if seats.get(seat) == sid:
            del seats[seat]
            released.append(seat)
    if not held:
        del seats_by_sid[sid]
    return released


def reserve_name(
    name_map: dict[NAME_KEY, SID],
    reverse: dict[SID, NAME_KEY],
    key: NAME_KEY,
    sid: SID,
    *,
    connected: bool,
) -> None:
    """**原子**「校验 + 写入」：一次调用内完成存活校验与名字落账（路由解析用）。

    本函数**无 await、不取锁**——原子性由调用方提供：sync 端在注册表锁内调用，async 端依赖「零挂起点」
    （把它写成同步函数正是为了让 async 的原子性成为**结构保证**而非碰巧）。
    Atomic check-and-set; the caller provides atomicity (a lock on sync, zero suspension points on async).

    **准入不在这里判**（#230）：「每 role 一席」由 :func:`claim_seat` 在阶段 1 原子判定（``4101``）；调用方
    必须先占到 ``(office_id, role)`` 席位才走到这里，故键被**他人**持有只可能是不变量被破坏。
    Admission is decided by :func:`claim_seat`; a key held by another sid here means a broken invariant.

    Raises:
        SessionGoneError: ``connected`` 为假（sid 已进入断连收尾）。
        RegistryInvariantError: 键已被其它 sid 持有（席位表与注册表矛盾）。
    """
    if not connected:
        raise SessionGoneError(f"sid {sid!r} is no longer connected")
    holder = name_map.get(key)
    if holder is not None and holder != sid:
        raise RegistryInvariantError(f"name key {key!r} is held by another sid while the seat was claimed")
    if holder == sid:
        return  # 同 sid 重复注册：幂等 / idempotent re-registration
    # 一个 sid 至多持有一个键：先释放它此前持有的（正常路径已由 leave_room 释放，此处兜住收敛失败的残留）
    # One key per sid: release any key it still holds.
    release_name(name_map, reverse, sid)
    name_map[key] = sid
    reverse[sid] = key


def release_name(
    name_map: dict[NAME_KEY, SID],
    reverse: dict[SID, NAME_KEY],
    sid: SID,
    office_id: OFFICE_ID | None = None,
) -> NAME_KEY | None:
    """注销 sid 持有的键（按反向索引定位；**归属守卫**：只删确由本 sid 持有的键）。

    ``office_id`` 非空时只注销**该房**的键（``leave_room`` 用：退 A 房不得顺手注销同 sid 并发登记的 B 房键）。
    Release the key held by ``sid``; with ``office_id`` only a key of that office is released.

    Returns:
        实际从正向表删除的键；未持有 / 归属他人 / 不属该房时为 ``None``。
    """
    key = reverse.get(sid)
    if key is None or (office_id is not None and key[0] != office_id):
        return None
    del reverse[sid]
    if name_map.get(key) == sid:
        del name_map[key]
        return key
    return None
