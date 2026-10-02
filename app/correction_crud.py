"""测温值更正的写入 / 查询事务。

更正语义
--------
* 凭“**自有测温事件ID**或**车载读数ID** 二选一 + 更正ID + 预期版本 +
  新温度”提交；只改温度，测温时刻、货物归属与原编号不变；交接事件不可更正。
* 每次更正产生新版本（首次更正后版本 1，依次递增）；原始记录永不修改，
  时间线 / 越温 / 责任 / 同时越温一律按最新版本“有效温度”重建。
* 车载读数更正对读数时刻仍在该车次车上的**全部货物**同时生效。

加锁顺序（与既有写入完全一致，避免死锁）
----------------------------------------
* 自有测温更正：锁定该货物行——与该货物的事件写入、责任表态、车载读数写入
  及装卸登记严格串行；
* 车载读数更正：先按 cargo_id 升序锁定读数时刻在车上的全部货物行，再锁定
  车次行——与 readings 写入、各货物事件 / 表态、装卸登记串行。

拒绝场景（一律不落任何部分结果）
--------------------------------
* 目标不存在：自有事件 404 event_not_found；车载读数 404 reading_not_found；
* 目标是交接事件：422 target_not_correctable（交接不可更正）；
* 请求同时/均未给 event_id 与 reading_id：422 bad_correction_shape；
* 预期版本与当前版本不一致（版本过期 / 已被更新的更正抢先）：409 stale_version；
* 更正ID已存在：同内容重传 200 返回原结果（幂等），异内容 409 correction_id_conflict。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import effective
from app.crud import ApiError
from app.models import (
    CorrectionTarget,
    Event,
    EventType,
    TemperatureCorrection,
    Trip,
    VehicleReading,
)
from app.schemas import TemperatureCorrectionCreate
from app.timeutil import ensure_utc

TARGET_OWN = "own"
TARGET_VEHICLE = "vehicle"


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


@dataclass
class CorrectionResult:
    correction: TemperatureCorrection
    replayed: bool
    affected_cargo_ids: list[str]
    target_type: str
    target_id: str
    cargo_id: str | None
    effective_temperature: Decimal
    version: int


def submit_correction(
    db, payload: TemperatureCorrectionCreate
) -> CorrectionResult:
    # SQLite 文件库无行级锁，极端并发下 busy timeout 耗尽会抛 OperationalError，
    # 整体重试一次即可（全部校验重来，语义不变）；PG 不会触发。
    try:
        return _submit_tx(db, payload)
    except OperationalError:
        db.rollback()
        return _submit_tx(db, payload)


def _resolve_target(payload):
    """二选一目标定位 -> (target_type, target_id)；形状非法即 422。"""
    has_event = payload.event_id is not None
    has_reading = payload.reading_id is not None
    if has_event == has_reading:
        raise _unprocessable(
            "更正必须且只能给出一个目标：event_id（自有测温事件）"
            "或 reading_id（车载读数），二者互斥、必填其一",
            code="bad_correction_shape",
        )
    if has_event:
        return TARGET_OWN, payload.event_id
    return TARGET_VEHICLE, payload.reading_id


def _same_content(existing: TemperatureCorrection, payload, target_type, target_id) -> bool:
    if existing.target_type is not CorrectionTarget(target_type):
        return False
    existing_target = (
        existing.event_id if target_type == TARGET_OWN else existing.reading_id
    )
    return (
        existing_target == target_id
        and existing.expected_version == payload.expected_version
        and Decimal(existing.new_temperature) == Decimal(payload.new_temperature)
    )


def _affected_cargo_ids(db, trip_id: str, read_at) -> list[str]:
    from app.models import Loading

    rows = db.scalars(
        select(Loading.cargo_id).where(
            Loading.trip_id == trip_id,
            Loading.loaded_at <= read_at,
            Loading.unloaded_at > read_at,
        )
    ).all()
    return sorted(set(rows))


def _submit_tx(db, payload: TemperatureCorrectionCreate) -> CorrectionResult:
    target_type, target_id = _resolve_target(payload)
    new_temperature = Decimal(payload.new_temperature)

    # 幂等优先（按主键读，无需持锁）：同更正ID同内容重传返回原结果
    existing = db.get(TemperatureCorrection, payload.correction_id)
    if existing is not None:
        if _same_content(existing, payload, target_type, target_id):
            db.rollback()
            return _replay_result(db, existing)
        raise _conflict(
            f"更正ID {payload.correction_id} 已存在且内容不同，拒绝覆盖；"
            "再次更正请使用新的更正编号",
            code="correction_id_conflict",
        )

    try:
        if target_type == TARGET_OWN:
            result = _submit_own(db, payload, target_id, new_temperature)
        else:
            result = _submit_vehicle(db, payload, target_id, new_temperature)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        # PostgreSQL 下行锁已把相关写入串行；SQLite FOR UPDATE 为空操作，
        # 并发同主键插入可能直接触发唯一约束。回滚后：
        # 1) 同更正ID同内容 -> 按幂等返回原结果；
        # 2) 同更正ID异内容 -> correction_id_conflict；
        # 3) 同目标版本唯一索引落败（并发的另一笔更正抢先产生该版本）
        #    -> stale_version（不落后来者的部分结果）。
        db.rollback()
        winner = db.get(TemperatureCorrection, payload.correction_id)
        if winner is not None:
            if _same_content(winner, payload, target_type, target_id):
                return _replay_result(db, winner)
            raise _conflict(
                f"更正ID {payload.correction_id} 已存在且内容不同，拒绝覆盖；"
                "再次更正请使用新的更正编号",
                code="correction_id_conflict",
            )
        current = effective.current_version(db, target_type, target_id)
        if payload.expected_version != current:
            raise _conflict(
                f"预期版本 {payload.expected_version} 与{('车载读数' if target_type == TARGET_VEHICLE else '事件')} "
                f"{target_id} 的当前版本 {current} 不一致（并发的更新更正已抢先提交），"
                "请按当前版本重新提交",
                code="stale_version",
            )
        raise _conflict(
            f"更正 {payload.correction_id} 与既有数据冲突（字段约束）",
            code="integrity_conflict",
        )
    return result


def _submit_own(db, payload, event_id, new_temperature) -> CorrectionResult:
    from app.crud import _load_cargo_locked

    # 目标事件须存在（先读，供归属货物加锁）
    event = db.get(Event, event_id)
    if event is None:
        raise ApiError(
            404, "event_not_found", f"自有测温事件 {event_id} 不存在"
        )
    # 交接事件不可更正
    if event.event_type is not EventType.measurement:
        raise _unprocessable(
            f"事件 {event_id} 是交接事件，交接只改变保管方、不允许更正温度；"
            "仅测温事件可更正",
            code="target_not_correctable",
        )

    # 与同货物事件 / 表态 / 车载读数 / 装卸登记串行：锁货物行
    cargo = _load_cargo_locked(db, event.cargo_id)

    # 持锁后复核幂等（并发重传）
    winner = db.get(TemperatureCorrection, payload.correction_id)
    if winner is not None:
        if _same_content(winner, payload, TARGET_OWN, event_id):
            db.rollback()
            return _replay_result(db, winner)
        raise _conflict(
            f"更正ID {payload.correction_id} 已存在且内容不同，拒绝覆盖",
            code="correction_id_conflict",
        )

    current = effective.current_version(db, TARGET_OWN, event_id)
    if payload.expected_version != current:
        raise _conflict(
            f"预期版本 {payload.expected_version} 与事件 {event_id} 的当前版本 "
            f"{current} 不一致（已存在更新的更正），请按当前版本重新提交",
            code="stale_version",
        )

    correction = TemperatureCorrection(
        correction_id=payload.correction_id,
        target_type=CorrectionTarget.own,
        event_id=event_id,
        reading_id=None,
        cargo_id=cargo.cargo_id,
        expected_version=payload.expected_version,
        version=current + 1,
        new_temperature=new_temperature,
    )
    db.add(correction)
    db.flush()
    return CorrectionResult(
        correction=correction,
        replayed=False,
        affected_cargo_ids=[cargo.cargo_id],
        target_type=TARGET_OWN,
        target_id=event_id,
        cargo_id=cargo.cargo_id,
        effective_temperature=new_temperature,
        version=current + 1,
    )


def _submit_vehicle(db, payload, reading_id, new_temperature) -> CorrectionResult:
    from app.vehicle_crud import _affected_cargo_ids as affected, _load_trip_locked, _lock_cargo_rows

    # 读数须存在（先读，供受影响货物与车次加锁）
    reading = db.get(VehicleReading, reading_id)
    if reading is None:
        raise ApiError(
            404, "reading_not_found", f"车载读数 {reading_id} 不存在"
        )
    read_at = ensure_utc(reading.read_at)

    if db.get(Trip, reading.trip_id) is None:
        # 外键约束下不应发生；防御性按车次缺失处理
        raise ApiError(404, "trip_not_found", f"车次 {reading.trip_id} 不存在")

    cargo_ids = affected(db, reading.trip_id, read_at)

    # 1) 先锁读数时刻在车上的全部货物行（升序），与这些货物的事件 / 表态串行
    _lock_cargo_rows(db, cargo_ids)
    # 2) 再锁车次行，与装卸登记、同车次读数写入串行
    _load_trip_locked(db, reading.trip_id)

    # 持锁后复核幂等（并发重传）
    winner = db.get(TemperatureCorrection, payload.correction_id)
    if winner is not None:
        if _same_content(winner, payload, TARGET_VEHICLE, reading_id):
            db.rollback()
            return _replay_result(db, winner)
        raise _conflict(
            f"更正ID {payload.correction_id} 已存在且内容不同，拒绝覆盖",
            code="correction_id_conflict",
        )

    current = effective.current_version(db, TARGET_VEHICLE, reading_id)
    if payload.expected_version != current:
        raise _conflict(
            f"预期版本 {payload.expected_version} 与车载读数 {reading_id} 的当前版本 "
            f"{current} 不一致（已存在更新的更正），请按当前版本重新提交",
            code="stale_version",
        )

    correction = TemperatureCorrection(
        correction_id=payload.correction_id,
        target_type=CorrectionTarget.vehicle,
        event_id=None,
        reading_id=reading_id,
        cargo_id=None,
        expected_version=payload.expected_version,
        version=current + 1,
        new_temperature=new_temperature,
    )
    db.add(correction)
    db.flush()
    return CorrectionResult(
        correction=correction,
        replayed=False,
        affected_cargo_ids=cargo_ids,
        target_type=TARGET_VEHICLE,
        target_id=reading_id,
        cargo_id=None,
        effective_temperature=new_temperature,
        version=current + 1,
    )


def _replay_result(db, existing: TemperatureCorrection) -> CorrectionResult:
    """同更正ID同内容重传：返回原结果（有效温度/版本为该更正当时生成的版本）。"""
    is_own = existing.target_type is CorrectionTarget.own
    target_type = TARGET_OWN if is_own else TARGET_VEHICLE
    target_id = existing.event_id if is_own else existing.reading_id
    affected_ids: list[str] = []
    if is_own:
        affected_ids = [existing.cargo_id]
    else:
        reading = db.get(VehicleReading, target_id)
        if reading is not None:
            affected_ids = _affected_cargo_ids(
                db, reading.trip_id, ensure_utc(reading.read_at)
            )
    return CorrectionResult(
        correction=existing,
        replayed=True,
        affected_cargo_ids=affected_ids,
        target_type=target_type,
        target_id=target_id,
        cargo_id=existing.cargo_id,
        effective_temperature=Decimal(existing.new_temperature),
        version=existing.version,
    )


# ----------------------------------------------------------- 查询


def get_correction(db, correction_id: str) -> TemperatureCorrection:
    row = db.get(TemperatureCorrection, correction_id)
    if row is None:
        raise ApiError(
            404,
            "correction_not_found",
            f"更正 {correction_id} 不存在",
        )
    row.created_at = ensure_utc(row.created_at)
    return row
