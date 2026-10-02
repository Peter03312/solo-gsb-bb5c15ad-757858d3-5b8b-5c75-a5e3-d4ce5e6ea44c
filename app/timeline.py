"""把货物自有事件与车载测温读数合并为统一读数时间线。

合并规则
--------
* 车载读数仅对“读数时刻仍在车上”的货物生效——该货物在对应车次登记了装卸时段
  ``[loaded_at, unloaded_at)`` 且 ``loaded_at <= 读数时刻 < unloaded_at``（左闭右开）。
* 同一时刻该货物有自有测温时，**优先采用自有测温**；车载读数在该时刻对该货物
  标记 ``adopted=False``（被取代），不参与越温重建，但原始读数仍可查询。
* 交接事件只来自货物自有事件，车载读数从不改变保管方。
"""
from __future__ import annotations

from decimal import Decimal

from app import effective
from app.chain import (
    SOURCE_OWN,
    SOURCE_VEHICLE,
    TimelinePoint,
    point_from_event,
)


def is_on_board(loading, at) -> bool:
    """读数时刻是否仍在车上：左闭右开 [loaded_at, unloaded_at)。"""
    return loading.loaded_at <= at < loading.unloaded_at


def is_reading_applicable(loading, reading) -> bool:
    """车载读数是否对某票货物生效：同一车次且读数时刻仍在车上（左闭右开）。

    必须同时匹配车次与时刻：同一货物可在首尾相接的不同车次上各有装卸时段，
    不能因另一车次同刻开始的窗口而把本车次已下车时刻的读数误判为适用。
    """
    return loading.trip_id == reading.trip_id and is_on_board(
        loading, reading.read_at
    )


def onboard_loadings(loadings, at) -> list:
    """返回 at 时刻车上的装卸登记（同货物同刻至多一条，应用侧已校验不重叠）。"""
    return [l for l in loadings if is_on_board(l, at)]


def merged_timeline(
    own_events,
    vehicle_readings,
    loadings,
    eff: effective.EffectiveTemperatures | None = None,
) -> list[TimelinePoint]:
    """生成货物的完整读数时间线（含被取代的车载点，按时间升序）。

    :param own_events: 该货物的全部自有事件（测温 / 交接）。
    :param vehicle_readings: 该货物所有车次的车载原始读数（可含与其无关车次的，
        由 loadings 决定是否适用）。
    :param loadings: 该货物的全部装卸时段登记。
    :param eff: 测温更正后的有效温度覆盖（自有按事件ID、车载按读数ID）；
        缺省表示无更正，全部沿用原始温度（版本 0）。
    """
    if eff is None:
        eff = effective.EffectiveTemperatures.empty()

    def _as_own_point(e):
        if point_from_event(e).is_measurement:
            temp, version = eff.own_temperature(e.event_id, e.temperature)
            return point_from_event(
                e, temperature_override=temp, temperature_version=version
            )
        return point_from_event(e)

    points: list[TimelinePoint] = [_as_own_point(e) for e in own_events]
    own_measure_at = {
        e.occurred_at
        for e in own_events
        if point_from_event(e).is_measurement
    }
    for r in vehicle_readings:
        # 必须在“同一车次”的装卸时段内且读数时刻仍在车上
        applicable = any(is_reading_applicable(l, r) for l in loadings)
        if not applicable:
            # 与该货物无关的车次读数（不在车 / 别的车次）完全不出现在其时间线上
            continue
        eff_temp, version = eff.vehicle_temperature(r.reading_id, r.temperature)
        points.append(
            TimelinePoint(
                occurred_at=r.read_at,
                is_measurement=True,
                temperature=eff_temp,
                source=SOURCE_VEHICLE,
                source_id=r.reading_id,
                trip_id=r.trip_id,
                adopted=r.read_at not in own_measure_at,
                temperature_version=version,
                original_temperature=Decimal(r.temperature),
            )
        )
    points.sort(
        key=lambda p: (
            p.occurred_at,
            0 if p.is_measurement else 1,
            {SOURCE_OWN: 0, SOURCE_VEHICLE: 1}.get(p.source, 9),
            p.source_id or "",
        )
    )
    return points


def adopted_timeline(
    own_events,
    vehicle_readings,
    loadings,
    eff: effective.EffectiveTemperatures | None = None,
) -> list[TimelinePoint]:
    """只含被采用点的时间线，供保管链重建使用。"""
    return [
        p
        for p in merged_timeline(own_events, vehicle_readings, loadings, eff)
        if p.adopted
    ]


def load_cargo_context(db, cargo_id: str):
    """一次读齐货物的自有事件、装卸时段、相关车次车载读数与有效温度覆盖。

    返回 ``(events, loadings, readings, eff)``。
    """
    from sqlalchemy import select

    from app.models import Event, Loading, VehicleReading
    from app.timeutil import ensure_utc

    events = list(
        db.scalars(
            select(Event)
            .where(Event.cargo_id == cargo_id)
            .order_by(Event.occurred_at, Event.event_id)
        ).all()
    )
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)

    loadings = list(
        db.scalars(
            select(Loading)
            .where(Loading.cargo_id == cargo_id)
            .order_by(Loading.loaded_at, Loading.trip_id)
        ).all()
    )
    for l in loadings:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)

    readings: list = []
    if loadings:
        trip_ids = {l.trip_id for l in loadings}
        rows = list(
            db.scalars(
                select(VehicleReading)
                .where(VehicleReading.trip_id.in_(trip_ids))
                .order_by(VehicleReading.read_at, VehicleReading.reading_id)
            ).all()
        )
        for r in rows:
            r.read_at = ensure_utc(r.read_at)
        # 仅按关系惰性引用，trip 行由车次查询单独获取
        readings = rows

    eff = effective.load_target_corrections(
        db,
        event_ids=[
            e.event_id
            for e in events
            if e.event_type.name == "measurement"
        ],
        reading_ids=[r.reading_id for r in readings],
    )
    return events, loadings, readings, eff


def resolve_trip_for_reading(db, trip_id: str):
    """取车次行（供读数适用性判断等场景）。"""
    from app.models import Trip

    return db.get(Trip, trip_id)
