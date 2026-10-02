"""冷链货物追责服务 —— FastAPI 入口。

启动（开发）: uvicorn app.main:app --reload
一键启动    : docker compose up --build  （见 README）
"""
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import (
    chain,
    correction_crud,
    crud,
    effective,
    responsibility,
    stance_crud,
    timeline,
    vehicle_crud,
)
from app.database import Base, engine, get_db
from app.models import (
    Cargo,
    Event,
    Loading,
    Stance,
    StanceKind,
    TemperatureCorrection,
    Trip,
    VehicleReading,
)
from app.schemas import (
    CargoCreate,
    CargoOut,
    CargoTimeline,
    ChainReport,
    EventCreate,
    EventOut,
    Excursion,
    IdempotentReplay,
    LatestStance,
    LoadingOut,
    ManifestCreate,
    ManifestOut,
    ReadingApplication,
    ResponsibilityInterval,
    ResponsibilityReport,
    ResponsibilitySegment,
    StanceCreate,
    StanceHistory,
    StanceHistoryItem,
    StanceOut,
    StanceReplay,
    StanceSegment,
    TemperatureCorrectionAck,
    TemperatureCorrectionCreate,
    TemperatureCorrectionHistory,
    TemperatureCorrectionOut,
    TemperatureCorrectionReplay,
    TimelinePointOut,
    TripCreate,
    TripDetail,
    TripOut,
    TripReadingOut,
    VehicleReadingAck,
    VehicleReadingCreate,
    VehicleReadingOut,
    VehicleReadingReplay,
    SimultaneousExcursionReport,
    SimultaneousExcursionSegment,
)
from app.timeutil import ensure_utc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 生产中由 db/init.sql 建表；此处确保直接 uvicorn / sqlite 测试也可用
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="冷链货物追责服务",
    description=(
        "记录冷链货物的自有测温与交接事件、车次装卸时段与车载测温读数，"
        "在合并读数时间线上按发生时间重建保管链，计算越温区间并拆分各保管方责任。"
    ),
    version="1.3.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.ApiError)
async def api_error_handler(request: Request, exc: crud.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# ---------------------------------------------------------------- 货物


@app.post("/cargo", response_model=CargoOut, status_code=201, tags=["货物"])
def create_cargo(payload: CargoCreate, db: Session = Depends(get_db)) -> Cargo:
    payload.started_at = ensure_utc(payload.started_at)
    return crud.create_cargo(db, payload)


@app.get("/cargo", response_model=list[CargoOut], tags=["货物"])
def list_cargo(db: Session = Depends(get_db)) -> list[Cargo]:
    return list(db.scalars(select(Cargo).order_by(Cargo.cargo_id)).all())


@app.get("/cargo/{cargo_id}", response_model=CargoOut, tags=["货物"])
def get_cargo(cargo_id: str, db: Session = Depends(get_db)) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    return cargo


# ---------------------------------------------------------------- 事件


def _event_out(e: Event, eff: effective.EffectiveTemperatures | None = None) -> EventOut:
    """事件响应：测温事件附当前有效温度与版本（交接为 null / 0）。"""
    raw_temp = Decimal(e.temperature) if e.temperature is not None else None
    if e.event_type.name == "measurement" and raw_temp is not None:
        eff_temp, version = (
            eff.own_temperature(e.event_id, raw_temp)
            if eff is not None
            else (raw_temp, 0)
        )
    else:
        eff_temp, version = None, 0
    return EventOut(
        event_id=e.event_id,
        cargo_id=e.cargo_id,
        event_type=e.event_type,
        occurred_at=e.occurred_at,
        temperature=raw_temp,
        giver=e.giver,
        receiver=e.receiver,
        effective_temperature=eff_temp,
        temperature_version=version,
    )


@app.post(
    "/events",
    response_model=EventOut,
    status_code=201,
    tags=["货物自有事件"],
    responses={
        200: {
            "model": IdempotentReplay,
            "description": "同 ID 同内容重传：不重复写入",
        }
    },
)
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    payload.occurred_at = ensure_utc(payload.occurred_at)
    result = crud.create_event(db, payload)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=IdempotentReplay(
                event_id=payload.event_id,
                message="相同内容的事件已存在，重传被忽略（幂等）",
            ).model_dump(),
        )
    # 新写入的事件尚无更正：有效温度即原始温度，版本 0
    return _event_out(result.event)


@app.get(
    "/cargo/{cargo_id}/events",
    response_model=list[EventOut],
    tags=["货物自有事件"],
)
def list_events(cargo_id: str, db: Session = Depends(get_db)) -> list[EventOut]:
    """只列货物**自有**事件（测温 / 交接）；车载读数不在此列表，经车次/读数时间线查询。

    测温事件的 ``temperature`` 恒为**原始录入值**（更正不改原始记录），另给
    ``effective_temperature``（当前有效温度）与 ``temperature_version``
    （0=未更正，>0=已更正到第 N 版）。
    """
    if db.get(Cargo, cargo_id) is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    stmt = (
        select(Event)
        .where(Event.cargo_id == cargo_id)
        .order_by(Event.occurred_at, Event.event_id)
    )
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)
    eff = effective.load_target_corrections(
        db,
        event_ids=[
            e.event_id for e in events if e.event_type.name == "measurement"
        ],
    )
    return [_event_out(e, eff) for e in events]


# ---------------------------------------------------------------- 车次 / 装卸 / 车载读数


@app.post("/trips", response_model=TripOut, status_code=201, tags=["车次与车载测温"])
def create_trip(payload: TripCreate, db: Session = Depends(get_db)) -> Trip:
    return vehicle_crud.create_trip(db, payload)


@app.get("/trips", response_model=list[TripOut], tags=["车次与车载测温"])
def list_trips(db: Session = Depends(get_db)) -> list[Trip]:
    return vehicle_crud.list_trips(db)


def _reading_out(
    r: VehicleReading, eff: effective.EffectiveTemperatures | None = None
) -> VehicleReadingOut:
    eff_temp, version = (
        eff.vehicle_temperature(r.reading_id, Decimal(r.temperature))
        if eff is not None
        else (Decimal(r.temperature), 0)
    )
    return VehicleReadingOut(
        reading_id=r.reading_id,
        trip_id=r.trip_id,
        read_at=ensure_utc(r.read_at),
        temperature=Decimal(r.temperature),
        effective_temperature=eff_temp,
        temperature_version=version,
        created_at=ensure_utc(r.created_at),
    )


@app.post(
    "/trips/{trip_id}/manifest",
    response_model=ManifestOut,
    status_code=201,
    tags=["车次与车载测温"],
)
def register_manifest(
    trip_id: str, payload: ManifestCreate, db: Session = Depends(get_db)
) -> ManifestOut:
    """批量登记车次上各票货物的装卸时段（原子：任一非法整批拒绝，不留部分记录）。"""
    payload.trip_id = trip_id
    for it in payload.items:
        it.loaded_at = ensure_utc(it.loaded_at)
        it.unloaded_at = ensure_utc(it.unloaded_at)
    result = vehicle_crud.register_manifest(db, payload)
    return ManifestOut(
        trip_id=result.trip_id,
        items=[
            LoadingOut(
                trip_id=l.trip_id,
                cargo_id=l.cargo_id,
                loaded_at=l.loaded_at,
                unloaded_at=l.unloaded_at,
            )
            for l in result.items
        ],
    )


@app.post(
    "/vehicle-readings",
    response_model=VehicleReadingAck,
    status_code=201,
    tags=["车次与车载测温"],
    responses={
        200: {
            "model": VehicleReadingReplay,
            "description": "同编号同内容重传：返回原读数（幂等）",
        }
    },
)
def create_vehicle_reading(
    payload: VehicleReadingCreate, db: Session = Depends(get_db)
):
    """写入车载原始读数（允许晚到）。

    与读数时刻在车上的全部货物的事件写入 / 责任表态共用货物行锁串行一致。
    回执给出受影响货物（读数时刻仍在车上，按各自温度上限判断越温）。
    """
    payload.read_at = ensure_utc(payload.read_at)
    result = vehicle_crud.create_vehicle_reading(db, payload)
    eff = effective.load_target_corrections(db, reading_ids=[result.reading.reading_id])
    reading_out = _reading_out(result.reading, eff)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=VehicleReadingReplay(
                replayed=True,
                reading=reading_out,
                affected_cargo_ids=result.affected_cargo_ids,
                message="相同内容的车载读数已存在，重传被忽略（幂等）",
            ).model_dump(mode="json"),
        )
    return VehicleReadingAck(
        reading=reading_out, affected_cargo_ids=result.affected_cargo_ids
    )


# ---------------------------------------------------------------- 测温值更正


def _correction_out(c: TemperatureCorrection) -> TemperatureCorrectionOut:
    return TemperatureCorrectionOut(
        correction_id=c.correction_id,
        target_type=(
            c.target_type.value
            if hasattr(c.target_type, "value")
            else str(c.target_type)
        ),
        event_id=c.event_id,
        reading_id=c.reading_id,
        cargo_id=c.cargo_id,
        expected_version=c.expected_version,
        version=c.version,
        new_temperature=Decimal(c.new_temperature),
        created_at=ensure_utc(c.created_at),
    )


def _correction_out_dict(c: TemperatureCorrection) -> dict:
    """供 JSONResponse 使用：Decimal 序列化为三位小数字符串。"""
    data = _correction_out(c).model_dump(mode="json")
    data["new_temperature"] = str(Decimal(c.new_temperature).quantize(Decimal("0.001")))
    return data


def _correction_ack_dict(result) -> dict:
    """组装更正回执内容（Decimal 统一为三位小数字符串，与其余响应一致）。"""
    return {
        "correction": _correction_out_dict(result.correction),
        "target_type": result.target_type,
        "target_id": result.target_id,
        "cargo_id": result.cargo_id,
        "effective_temperature": str(result.effective_temperature.quantize(Decimal("0.001"))),
        "temperature_version": result.version,
        "affected_cargo_ids": result.affected_cargo_ids,
    }


@app.post(
    "/temperature-corrections",
    response_model=TemperatureCorrectionAck,
    status_code=201,
    tags=["测温更正"],
    responses={
        200: {
            "model": TemperatureCorrectionReplay,
            "description": "同更正ID同内容重传：返回原结果（幂等）",
        },
        404: {"description": "目标自有事件 / 车载读数不存在"},
        409: {"description": "更正ID异内容冲突 / 预期版本过期"},
        422: {"description": "目标是交接不可更正 / 请求目标形状非法"},
    },
)
def submit_temperature_correction(
    payload: TemperatureCorrectionCreate, db: Session = Depends(get_db)
):
    """更正一笔测温值（自有测温事件 或 车载读数，二选一定位目标）。

    只改温度：测温时刻、货物归属与原编号不变，交接事件不可更正。每次更正生成
    新版本；货物时间线、越温报告、责任确认与车次同时越温查询一律按最新有效
    温度重建。车载读数更正同时影响读数时刻仍在该车次车上的全部货物。

    更正与相关事件、责任表态、车载读数及装卸登记在同一套行锁下串行；
    预期版本与服务端当前版本不一致（版本过期）、目标不存在或目标为交接，
    一律拒绝且不落任何部分结果。
    """
    result = correction_crud.submit_correction(db, payload)
    if result.replayed:
        content = _correction_ack_dict(result)
        content["replayed"] = True
        content["message"] = "相同内容的测温更正已存在，重传返回原结果（幂等）"
        return JSONResponse(status_code=200, content=content)
    return JSONResponse(status_code=201, content=_correction_ack_dict(result))


@app.get(
    "/temperature-corrections/{correction_id}",
    response_model=TemperatureCorrectionOut,
    tags=["测温更正"],
    responses={404: {"description": "更正不存在"}},
)
def get_temperature_correction(
    correction_id: str, db: Session = Depends(get_db)
) -> TemperatureCorrectionOut:
    """按更正ID查询单条更正记录（原始测温记录及历次更正均保留可查）。"""
    return _correction_out(correction_crud.get_correction(db, correction_id))


@app.get(
    "/events/{event_id}/corrections",
    response_model=TemperatureCorrectionHistory,
    tags=["测温更正"],
    responses={404: {"description": "自有事件不存在"}},
)
def event_correction_history(
    event_id: str, db: Session = Depends(get_db)
) -> TemperatureCorrectionHistory:
    """查询某自有测温事件的原始温度与历次更正（按版本升序），并给出当前有效温度。"""
    event = db.get(Event, event_id)
    if event is None:
        raise crud.ApiError(404, "event_not_found", f"自有事件 {event_id} 不存在")
    if event.event_type.name != "measurement":
        raise crud.ApiError(
            422,
            "target_not_correctable",
            f"事件 {event_id} 是交接事件，交接不记录温度、不可更正，无更正历史",
        )
    rows = effective.list_corrections(db, correction_crud.TARGET_OWN, event_id)
    eff = effective.load_target_corrections(db, event_ids=[event_id])
    eff_temp, version = eff.own_temperature(event_id, Decimal(event.temperature))
    return TemperatureCorrectionHistory(
        target_type=correction_crud.TARGET_OWN,
        target_id=event_id,
        cargo_id=event.cargo_id,
        trip_id=None,
        target_at=ensure_utc(event.occurred_at),
        original_temperature=Decimal(event.temperature),
        current_version=version,
        effective_temperature=eff_temp,
        corrections=[_correction_out(c) for c in rows],
    )


@app.get(
    "/vehicle-readings/{reading_id}/corrections",
    response_model=TemperatureCorrectionHistory,
    tags=["测温更正"],
    responses={404: {"description": "车载读数不存在"}},
)
def reading_correction_history(
    reading_id: str, db: Session = Depends(get_db)
) -> TemperatureCorrectionHistory:
    """查询某车载读数的原始温度与历次更正（按版本升序），并给出当前有效温度。"""
    reading = db.get(VehicleReading, reading_id)
    if reading is None:
        raise crud.ApiError(
            404, "reading_not_found", f"车载读数 {reading_id} 不存在"
        )
    rows = effective.list_corrections(db, correction_crud.TARGET_VEHICLE, reading_id)
    eff = effective.load_target_corrections(db, reading_ids=[reading_id])
    eff_temp, version = eff.vehicle_temperature(reading_id, Decimal(reading.temperature))
    return TemperatureCorrectionHistory(
        target_type=correction_crud.TARGET_VEHICLE,
        target_id=reading_id,
        cargo_id=None,
        trip_id=reading.trip_id,
        target_at=ensure_utc(reading.read_at),
        original_temperature=Decimal(reading.temperature),
        current_version=version,
        effective_temperature=eff_temp,
        corrections=[_correction_out(c) for c in rows],
    )


def _excursion_schema(ex) -> Excursion:
    return Excursion(
        start=ex.start,
        end=ex.end,
        is_open=ex.is_open,
        interval_id=chain.interval_id(ex),
        start_source=ex.start_source,
        end_source=(None if ex.is_open else ex.end_source),
        start_reading_id=ex.start_reading_id,
        end_reading_id=(None if ex.is_open else ex.end_reading_id),
        start_trip_id=ex.start_trip_id,
        end_trip_id=(None if ex.is_open else ex.end_trip_id),
        segments=[
            ResponsibilitySegment(
                start=seg.start, end=seg.end, responsible_party=seg.party
            )
            for seg in ex.segments
        ],
    )


def _trip_reading_application(db, reading: VehicleReading, loadings, cargos, eff):
    """一笔车载读数对车次上各票货物的适用性/采用情况（温度均取有效温度）。"""
    apps = []
    read_at = ensure_utc(reading.read_at)
    cargo_loadings: dict[str, list] = {}
    for l in loadings:
        cargo_loadings.setdefault(l.cargo_id, []).append(l)

    own_at: dict[str, Event] = {}
    if cargo_loadings:
        rows = db.scalars(
            select(Event).where(
                Event.cargo_id.in_(sorted(cargo_loadings)),
                Event.occurred_at == read_at,
            )
        ).all()
        for e in rows:
            if e.event_type.name == "measurement":
                own_at[e.cargo_id] = e

    read_eff_temp, read_version = eff.vehicle_temperature(
        reading.reading_id, Decimal(reading.temperature)
    )

    for cargo_id in sorted(cargo_loadings):
        on_board = any(timeline.is_on_board(l, read_at) for l in cargo_loadings[cargo_id])
        own_event = own_at.get(cargo_id)
        adopted = bool(on_board and own_event is None)
        over = None
        applied_temp = None
        reason = "adopted"
        if not on_board:
            reason = "not_on_board"
        elif own_event is not None:
            reason = "supplanted_by_own"
            # 同刻自有测温优先：采用该自有测温的有效温度
            own_temp, _ = eff.own_temperature(
                own_event.event_id, Decimal(own_event.temperature)
            )
            applied_temp = own_temp
        if on_board:
            if applied_temp is None:
                applied_temp = read_eff_temp
            over = applied_temp > Decimal(cargos[cargo_id].temp_upper_limit)
        apps.append(
            ReadingApplication(
                cargo_id=cargo_id,
                on_board=on_board,
                adopted=adopted,
                over_limit=over,
                applied_temperature=applied_temp,
                reading_temperature=read_eff_temp,
                reading_temperature_version=read_version,
                reason=reason,
            )
        )
    return apps


@app.get(
    "/trips/{trip_id}",
    response_model=TripDetail,
    tags=["车次与车载测温"],
)
def get_trip_detail(trip_id: str, db: Session = Depends(get_db)) -> TripDetail:
    """车次详情：装卸时段、车载原始读数，以及每笔读数对各货物的适用性/采用情况。"""
    trip = vehicle_crud.get_trip(db, trip_id)
    loadings = vehicle_crud.get_loadings(db, trip_id)
    for l in loadings:
        l.loaded_at = ensure_utc(l.loaded_at)
        l.unloaded_at = ensure_utc(l.unloaded_at)
    readings = vehicle_crud.get_readings(db, trip_id)
    cargo_ids = sorted({l.cargo_id for l in loadings})
    read_ats = {ensure_utc(r.read_at) for r in readings}
    # 仅取“车次上各票货物 × 各读数时刻”的自有测温（用于同刻自有优先判定），
    # 连同全部读数的更正一起折叠为有效温度覆盖
    own_at_reading_rows = []
    if cargo_ids and read_ats:
        own_at_reading_rows = list(
            db.scalars(
                select(Event).where(
                    Event.cargo_id.in_(cargo_ids),
                    Event.occurred_at.in_(read_ats),
                )
            ).all()
        )
    eff = effective.load_target_corrections(
        db,
        reading_ids=[r.reading_id for r in readings],
        event_ids=[
            e.event_id
            for e in own_at_reading_rows
            if e.event_type.name == "measurement"
        ],
    )

    cargos = {
        c.cargo_id: c
        for c in db.scalars(
            select(Cargo).where(Cargo.cargo_id.in_(cargo_ids or [""]))
        ).all()
    }

    trip_readings = []
    for r in readings:
        apps = _trip_reading_application(db, r, loadings, cargos, eff)
        trip_readings.append(
            TripReadingOut(
                reading_id=r.reading_id,
                trip_id=r.trip_id,
                read_at=ensure_utc(r.read_at),
                temperature=Decimal(r.temperature),
                effective_temperature=eff.vehicle_temperature(
                    r.reading_id, Decimal(r.temperature)
                )[0],
                temperature_version=eff.vehicle_version_of(r.reading_id),
                created_at=ensure_utc(r.created_at),
                applications=apps,
            )
        )

    return TripDetail(
        trip_id=trip.trip_id,
        vehicle_no=trip.vehicle_no,
        note=trip.note,
        created_at=ensure_utc(trip.created_at),
        loadings=[
            LoadingOut(
                trip_id=l.trip_id,
                cargo_id=l.cargo_id,
                loaded_at=ensure_utc(l.loaded_at),
                unloaded_at=ensure_utc(l.unloaded_at),
            )
            for l in loadings
        ],
        readings=trip_readings,
    )


# ---------------------------------------------------------------- 质控：同时越温查询


def _parse_aware_datetime(raw: str, field: str) -> datetime:
    """解析查询参数中的 ISO 8601 时刻；必须带时区（调用方明确给出带时区起止）。"""
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise crud.ApiError(
            422,
            "invalid_query_window",
            f"参数 {field}={raw!r} 不是合法的 ISO 8601 日期时间",
        )
    if parsed.tzinfo is None:
        raise crud.ApiError(
            422,
            "timezone_required",
            f"参数 {field}={raw!r} 必须携带时区偏移（如 2026-09-30T08:00:00+08:00）",
        )
    return ensure_utc(parsed)


def _parse_min_cargo(raw: str | None) -> int:
    """数量门槛：不小于 2 的正整数（缺省 2）。"""
    if raw is None:
        return 2
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise crud.ApiError(
            422,
            "invalid_min_cargo",
            f"参数 min_cargo={raw!r} 非法：必须为不小于 2 的整数",
        )
    if value < 2:
        raise crud.ApiError(
            422,
            "invalid_min_cargo",
            f"参数 min_cargo={value} 非法：必须为不小于 2 的整数",
        )
    return value


@app.get(
    "/trips/{trip_id}/simultaneous-excursions",
    response_model=SimultaneousExcursionReport,
    tags=["质控查询"],
    responses={
        404: {"description": "车次不存在"},
        422: {"description": "查询窗非法（终点不晚于起点 / 缺时区）或门槛非法"},
    },
)
def trip_simultaneous_excursions(
    trip_id: str,
    start_at: str | None = None,
    end_at: str | None = None,
    min_cargo: str | None = None,
    db: Session = Depends(get_db),
) -> SimultaneousExcursionReport:
    """质控：查找同一车次上**多票货物同时越温**的时段。

    每票货物按其现有合并读数时间线重建越温区间（同刻自有测温优先、车载读数
    仅对在车货物按各自上限生效），与该车次装卸时段（左闭右开）及查询窗
    相交（未闭合区间裁剪至查询终点）后扫描线叠加：货物集合变化即分段、
    相邻同集合片段合并，仅保留越温货物数达到 ``min_cargo``（默认 2）的
    正长度片段，按时间升序返回。晚到读数 / 事件提交后的查询反映新结果，
    同一次查询基于一致的已提交数据视图（PostgreSQL 下 REPEATABLE READ 快照）。
    """
    if start_at is None:
        raise crud.ApiError(
            422,
            "missing_parameter",
            "缺少必填参数 start_at：带时区的查询起点（ISO 8601）",
        )
    if end_at is None:
        raise crud.ApiError(
            422,
            "missing_parameter",
            "缺少必填参数 end_at：带时区的查询终点（ISO 8601，严格晚于起点）",
        )
    start = _parse_aware_datetime(start_at, "start_at")
    end = _parse_aware_datetime(end_at, "end_at")
    threshold = _parse_min_cargo(min_cargo)

    segments = vehicle_crud.query_simultaneous_excursions(
        db, trip_id, start, end, threshold
    )
    return SimultaneousExcursionReport(
        trip_id=trip_id,
        start_at=start,
        end_at=end,
        min_cargo=threshold,
        segments=[
            SimultaneousExcursionSegment(
                start=seg.start,
                end=seg.end,
                cargo_count=seg.cargo_count,
                cargo_ids=list(seg.cargo_ids),
            )
            for seg in segments
        ],
    )


# ---------------------------------------------------------------- 追责查询


def _load_cargo(db, cargo_id: str) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)
    return cargo


@app.get(
    "/cargo/{cargo_id}/report",
    response_model=ChainReport,
    tags=["追责查询"],
)
def cargo_report(cargo_id: str, db: Session = Depends(get_db)) -> ChainReport:
    """按合并读数时间线返回越温区间（含读数来源）与各责任方片段；未闭合区间标记未结束。"""
    cargo = _load_cargo(db, cargo_id)
    events, loadings, readings, eff = timeline.load_cargo_context(db, cargo_id)
    points = timeline.adopted_timeline(events, readings, loadings, eff)

    result = chain.rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), points
    )

    excursions = [_excursion_schema(ex) for ex in result.excursions]
    return ChainReport(
        cargo_id=cargo.cargo_id,
        temp_upper_limit=Decimal(cargo.temp_upper_limit),
        current_holder=result.current_holder,
        excursions=excursions,
    )


@app.get(
    "/cargo/{cargo_id}/timeline",
    response_model=CargoTimeline,
    tags=["追责查询"],
)
def cargo_timeline(cargo_id: str, db: Session = Depends(get_db)) -> CargoTimeline:
    """货物的合并读数时间线：自有测温/交接 + 适用的车载原始读数。

    含被自有测温取代（``adopted=false``）的车载读数，可查每票货物各时刻
    实际采用的读数来源；交接（``changes_holder=true``）只来自货物自有事件。
    """
    _load_cargo(db, cargo_id)
    events, loadings, readings, eff = timeline.load_cargo_context(db, cargo_id)
    points = timeline.merged_timeline(events, readings, loadings, eff)
    return CargoTimeline(
        cargo_id=cargo_id,
        points=[
            TimelinePointOut(
                occurred_at=p.occurred_at,
                point_type=("measurement" if p.is_measurement else "handover"),
                source=p.source,
                event_id=p.source_id if p.source == "own" else None,
                reading_id=p.source_id if p.source == "vehicle" else None,
                trip_id=p.trip_id,
                temperature=(Decimal(p.temperature) if p.is_measurement else None),
                original_temperature=(
                    Decimal(p.original_temperature)
                    if p.is_measurement and p.original_temperature is not None
                    else (Decimal(p.temperature) if p.is_measurement else None)
                ),
                temperature_version=(p.temperature_version if p.is_measurement else 0),
                giver=p.giver,
                receiver=p.receiver,
                adopted=p.adopted,
                changes_holder=(not p.is_measurement),
            )
            for p in points
        ],
    )


# ---------------------------------------------------------------- 责任确认


def _interval_schema(view) -> ResponsibilityInterval:
    """责任确认视图 -> 响应模型。"""
    return ResponsibilityInterval(
        cargo_id=view.cargo_id,
        interval_id=view.interval_id,
        start_event_id=view.start_event_id,
        end_event_id=view.end_event_id,
        start_source=view.start_source,
        end_source=view.end_source,
        start_reading_id=view.start_reading_id,
        end_reading_id=view.end_reading_id,
        start_trip_id=view.start_trip_id,
        end_trip_id=view.end_trip_id,
        start=view.start,
        end=view.end,
        snapshot_id=view.snapshot_id,
        segments=[
            StanceSegment(
                start=seg.start, end=seg.end, responsible_party=seg.responsible_party
            )
            for seg in view.segments
        ],
        latest_stances=[
            LatestStance(
                stance_id=sv.stance_id,
                responsible_party=sv.responsible_party,
                stance=sv.kind,
                reason=sv.reason,
                created_at=sv.created_at,
                segments=[
                    StanceSegment(
                        start=s.start,
                        end=s.end,
                        responsible_party=s.responsible_party,
                    )
                    for s in sv.segments
                ],
            )
            for sv in view.stances
        ],
        summary=view.summary,
    )


def _closed_interval_views(db, cargo: Cargo):
    """在合并时间线上重建并为每个已闭合区间装配责任确认视图（按开始时间升序）。"""
    events, loadings, readings, eff = timeline.load_cargo_context(db, cargo.cargo_id)
    result, closed = responsibility.rebuild(cargo, events, loadings, readings, eff)
    views = []
    for ex in sorted(closed.values(), key=lambda e: e.start):
        rows = stance_crud.load_interval_stances(
            db, cargo.cargo_id, chain.interval_id(ex)
        )
        views.append(
            responsibility.build_interval_view(cargo.cargo_id, ex, rows)
        )
    return result, views


@app.get(
    "/cargo/{cargo_id}/responsibilities",
    response_model=ResponsibilityReport,
    tags=["责任确认"],
)
def list_responsibilities(cargo_id: str, db: Session = Depends(get_db)) -> ResponsibilityReport:
    """列出货物下所有**已闭合**越温区间的责任确认状态（开放区间不参与）。"""
    cargo = _load_cargo(db, cargo_id)
    _, views = _closed_interval_views(db, cargo)
    return ResponsibilityReport(
        cargo_id=cargo_id, intervals=[_interval_schema(v) for v in views]
    )


@app.get(
    "/cargo/{cargo_id}/responsibilities/{interval_id}",
    response_model=ResponsibilityInterval,
    tags=["责任确认"],
    responses={404: {"description": "货物 / 区间不存在"}, 422: {"description": "区间开放"}},
)
def get_responsibility(
    interval_id: str, cargo_id: str, db: Session = Depends(get_db)
) -> ResponsibilityInterval:
    """按区间稳定标识（自有=开启测温事件ID；车载=veh:<读数ID>）查询单个已闭合区间。"""
    cargo = _load_cargo(db, cargo_id)

    _, ex = stance_crud._resolve_interval(db, cargo, interval_id)
    rows = stance_crud.load_interval_stances(db, cargo_id, interval_id)
    return _interval_schema(
        responsibility.build_interval_view(cargo_id, ex, rows)
    )


def _stance_out(stance: Stance) -> StanceOut:
    return StanceOut(
        stance_id=stance.stance_id,
        cargo_id=stance.cargo_id,
        interval_id=stance.interval_key,
        opener_source=stance.opener_source.value
        if hasattr(stance.opener_source, "value")
        else str(stance.opener_source),
        start_event_id=stance.start_event_id,
        vehicle_reading_id=stance.vehicle_reading_id,
        responsible_party=stance.responsible_party,
        stance=StanceKind(stance.kind),
        reason=stance.reason,
        snapshot_id=stance.snapshot_id,
        created_at=ensure_utc(stance.created_at),
    )


@app.post(
    "/cargo/{cargo_id}/responsibilities/{interval_id}/stances",
    response_model=StanceOut,
    status_code=201,
    tags=["责任确认"],
    responses={
        200: {"model": StanceReplay, "description": "同编号同内容重传：返回原记录"},
    },
)
def submit_stance(
    interval_id: str,
    payload: StanceCreate,
    cargo_id: str,
    db: Session = Depends(get_db),
):
    """提交责任方表态（确认 / 异议）。

    与同货物的自有事件写入、车载读数写入共用货物行锁串行化；以当前快照校验，
    过期快照、区间不存在或开放区间一律拒绝且不落库。
    """
    result = stance_crud.submit_stance(db, cargo_id, interval_id, payload)
    body = _stance_out(result.stance)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=StanceReplay(
                replayed=True,
                stance=body,
                message="相同内容的表态已存在，重传被忽略（幂等；即使该表态快照已失效也返回原记录）",
            ).model_dump(mode="json"),
        )
    return body


@app.get(
    "/cargo/{cargo_id}/responsibilities/{interval_id}/stances",
    response_model=StanceHistory,
    tags=["责任确认"],
)
def list_stance_history(
    interval_id: str, cargo_id: str, db: Session = Depends(get_db)
) -> StanceHistory:
    """区间表态历史：历次表态全部保留（含已改口、已失效），按提交时间升序。

    区间当前不存在 / 仍开放时，历史仍可查，头部字段给出当前状态。
    """
    cargo = _load_cargo(db, cargo_id)
    rows = stance_crud.load_interval_stances(db, cargo_id, interval_id)

    # 当前区间状态（不因此报 404/422：历史要始终可查）
    events, loadings, readings, eff = timeline.load_cargo_context(db, cargo_id)
    chain_result, closed = responsibility.rebuild(cargo, events, loadings, readings, eff)
    ex = closed.get(interval_id)
    open_ex = next(
        (e for e in chain_result.excursions if chain.interval_id(e) == interval_id),
        None,
    )
    interval_exists = ex is not None or open_ex is not None
    is_open = ex is None and open_ex is not None
    current_snapshot = responsibility.snapshot_id(ex) if ex is not None else None

    # 各责任方“最新有效表态”（行已按 (created_at, stance_id) 升序），
    # 用于把更早的有效表态标记为已改口
    latest_current: dict[str, Stance] = {}
    if ex is not None:
        for s in rows:
            if responsibility.is_stance_current(s, ex):
                latest_current[s.responsible_party] = s

    items = [
        StanceHistoryItem(
            stance_id=s.stance_id,
            cargo_id=s.cargo_id,
            interval_id=s.interval_key,
            opener_source=s.opener_source.value
            if hasattr(s.opener_source, "value")
            else str(s.opener_source),
            start_event_id=s.start_event_id,
            vehicle_reading_id=s.vehicle_reading_id,
            responsible_party=s.responsible_party,
            stance=StanceKind(s.kind),
            reason=s.reason,
            snapshot_id=s.snapshot_id,
            created_at=ensure_utc(s.created_at),
            is_current=(ex is not None and responsibility.is_stance_current(s, ex)),
            # 改口只在“当前仍有效”的行之间比较：已因快照变化失效的行不算被改口
            superseded=(
                ex is not None
                and responsibility.is_stance_current(s, ex)
                and (
                    (latest := latest_current.get(s.responsible_party)) is not None
                    and latest.stance_id != s.stance_id
                )
            ),
        )
        for s in rows
    ]
    return StanceHistory(
        cargo_id=cargo_id,
        interval_id=interval_id,
        interval_exists=interval_exists,
        is_open=is_open,
        current_snapshot_id=current_snapshot,
        history=items,
    )


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}
