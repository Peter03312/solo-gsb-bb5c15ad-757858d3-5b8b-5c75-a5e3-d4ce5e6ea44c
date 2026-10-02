"""Pydantic 请求 / 响应模型。"""
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models import EventType, StanceKind


class CargoCreate(BaseModel):
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    started_at: datetime = Field(..., description="起始时间（ISO 8601，建议带时区）")
    temp_upper_limit: Decimal = Field(..., description="温度上限（摄氏度）")
    initial_holder: str = Field(
        ..., min_length=1, max_length=128, description="起始保管方"
    )


class EventCreate(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=64, description="事件ID")
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    occurred_at: datetime = Field(..., description="发生时间（ISO 8601，建议带时区）")

    # 测温
    temperature: Decimal | None = Field(
        default=None, description="测温温度；测温事件必填且事件只能含此字段"
    )
    # 交接
    giver: str | None = Field(default=None, max_length=128, description="交出方")
    receiver: str | None = Field(default=None, max_length=128, description="接收方")

    def event_type(self) -> EventType:
        is_measurement = self.temperature is not None
        is_handover = self.giver is not None or self.receiver is not None
        if is_measurement and is_handover:
            raise ValueError("事件不能同时包含温度与交接双方")
        if not is_measurement and not is_handover:
            raise ValueError("事件必须包含 temperature，或同时包含 giver 与 receiver")
        if is_handover and not (self.giver and self.receiver):
            raise ValueError("交接事件必须同时提供交出方与接收方")
        if is_handover and self.giver == self.receiver:
            raise ValueError("交出方与接收方不能相同")
        return EventType.measurement if is_measurement else EventType.handover


class CargoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cargo_id: str
    started_at: datetime
    temp_upper_limit: Decimal
    initial_holder: str
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    cargo_id: str
    event_type: EventType
    occurred_at: datetime
    # 原始录入温度（测温事件）；测温更正后仍保持原值不变
    temperature: Decimal | None
    giver: str | None
    receiver: str | None
    # 更正后的有效温度与当前版本（交接事件为 null；无更正时版本 0 且等于原始值）
    effective_temperature: Decimal | None = Field(
        default=None,
        description="测温更正后的有效温度；交接为 null",
    )
    temperature_version: int = Field(
        default=0,
        description="测温当前版本：0=原始值，>0=已更正到第 N 版",
    )


class ResponsibilitySegment(BaseModel):
    """越温区间内按交接时刻切分出的责任片段。"""

    start: datetime = Field(..., description="片段起点（含）")
    end: datetime | None = Field(
        ..., description="片段终点（不含）；null 表示区间未闭合（未结束）"
    )
    responsible_party: str = Field(..., description="该片段责任方（保管方）")


class Excursion(BaseModel):
    """一次越温区间及其责任拆分。"""

    start: datetime = Field(..., description="越温开始（首次超限测温时刻，含）")
    end: datetime | None = Field(
        ..., description="越温结束（首次不超限测温时刻，不含）；null=未闭合"
    )
    is_open: bool = Field(..., description="是否尚未闭合（未结束）")
    segments: list[ResponsibilitySegment] = Field(..., description="责任方片段")
    interval_id: str = Field(
        ...,
        description="区间稳定标识：货物自有测温开启=事件ID；车载读数开启=veh:<读数ID>",
    )
    start_source: str = Field(
        ..., description="开启读数来源：own=货物自有测温；vehicle=车载读数"
    )
    end_source: str | None = Field(
        ..., description="闭合读数来源；未闭合时为 null"
    )
    start_reading_id: str | None = Field(
        ..., description="车载开启时的读数ID（自有开启为 null）"
    )
    end_reading_id: str | None = Field(
        ..., description="车载闭合时的读数ID（自有闭合 / 未闭合为 null）"
    )
    start_trip_id: str | None = Field(
        ..., description="车载开启时所属车次ID"
    )
    end_trip_id: str | None = Field(
        ..., description="车载闭合时所属车次ID"
    )


class ChainReport(BaseModel):
    cargo_id: str
    temp_upper_limit: Decimal
    current_holder: str = Field(..., description="按事件重建出的当前保管方")
    excursions: list[Excursion]


class IdempotentReplay(BaseModel):
    """同 ID 同内容重传时的响应：不重复写入。"""

    replayed: bool = True
    event_id: str
    message: str


# ----------------------------------------------------- 责任确认


class StanceCreate(BaseModel):
    """责任方表态提交。

    区间由路径上的区间标识（开启测温事件ID）定位；
    货物编号同样在路径上，请求体不再重复。
    """

    snapshot_id: str = Field(
        ...,
        min_length=8,
        max_length=71,
        description="提交前查询到的当前快照标识；与服务端当前快照不一致则拒绝（过期）",
    )
    responsible_party: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="表态责任方，必须出现在当前区间责任片段中",
    )
    stance: str = Field(
        ...,
        description="表态：confirm=确认担责；dispute=异议（必须同时提供 reason）",
    )
    reason: str | None = Field(
        default=None,
        max_length=512,
        description="异议理由；stance=dispute 时必填且非空，confirm 时必须为空",
    )
    stance_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="表态编号，客户端指定；同编号同内容重传返回原记录",
    )


class StanceSegment(BaseModel):
    """责任方在区间内承担的片段（一个责任方可能跨交接承担多段）。"""

    start: datetime
    end: datetime
    responsible_party: str


class LatestStance(BaseModel):
    """某责任方的最新一条有效表态。"""

    stance_id: str
    responsible_party: str
    stance: StanceKind
    reason: str | None
    created_at: datetime
    segments: list[StanceSegment] = Field(
        ..., description="该责任方在当前区间内承担的全部片段"
    )


class ResponsibilityInterval(BaseModel):
    """一个已闭合越温区间的责任确认视图。"""

    cargo_id: str
    interval_id: str = Field(
        ...,
        description="区间稳定标识：自有开启=测温事件ID；车载开启=veh:<读数ID>",
    )
    start_event_id: str | None = Field(
        ..., description="货物自有测温开启/闭合时的事件ID；车载来源为 null"
    )
    end_event_id: str | None
    start_source: str = Field(..., description="开启读数来源：own / vehicle")
    end_source: str = Field(..., description="闭合读数来源：own / vehicle")
    start_reading_id: str | None = Field(..., description="车载开启读数ID")
    end_reading_id: str | None = Field(..., description="车载闭合读数ID")
    start_trip_id: str | None
    end_trip_id: str | None
    start: datetime
    end: datetime
    snapshot_id: str = Field(..., description="当前快照标识（区间起止 + 责任片段）")
    segments: list[StanceSegment] = Field(..., description="当前责任片段（按时间）")
    latest_stances: list[LatestStance] = Field(
        ..., description="各责任方最新一条有效表态（按片段顺序）"
    )
    summary: str = Field(
        ...,
        description="汇总状态：all_confirmed 全确认 / has_dispute 有异议 / pending 待确认",
    )


class ResponsibilityReport(BaseModel):
    """货物下全部已闭合越温区间的责任确认汇总（开放区间不参与确认）。"""

    cargo_id: str
    intervals: list[ResponsibilityInterval]


class StanceOut(BaseModel):
    """表态记录。"""

    model_config = ConfigDict(from_attributes=True)

    stance_id: str
    cargo_id: str
    interval_id: str = Field(
        ...,
        description="区间稳定标识：自有开启=测温事件ID；车载开启=veh:<读数ID>",
    )
    opener_source: str = Field(..., description="区间开启读数来源：own / vehicle")
    start_event_id: str | None = Field(
        ..., description="自有开启的事件ID（车载开启为 null）"
    )
    vehicle_reading_id: str | None = Field(
        ..., description="车载开启的读数ID（自有开启为 null）"
    )
    responsible_party: str
    stance: StanceKind
    reason: str | None
    snapshot_id: str
    created_at: datetime


class StanceReplay(BaseModel):
    """同编号同内容重传时的响应：返回原记录（即使其快照已失效）。"""

    replayed: bool = True
    stance: StanceOut
    message: str


class StanceHistoryItem(StanceOut):
    """表态历史中的一条：附加该表态对当前快照是否仍有效。"""

    is_current: bool = Field(
        ...,
        description="对当前快照是否仍有效：区间/片段变化后为 false，但历史仍保留可查",
    )
    superseded: bool = Field(
        ..., description="是否已被同责任方更新的表态改口（新编号）"
    )


class StanceHistory(BaseModel):
    cargo_id: str
    interval_id: str
    interval_exists: bool = Field(..., description="区间当前是否存在")
    is_open: bool = Field(..., description="区间当前是否仍开放")
    current_snapshot_id: str | None = Field(
        ..., description="当前快照标识；区间不存在/开放时为 null"
    )
    history: list[StanceHistoryItem] = Field(
        ..., description="历次表态（含已改口、已失效），按提交时间升序"
    )


# ----------------------------------------------------- 车次 / 装卸 / 车载读数


class TripCreate(BaseModel):
    trip_id: str = Field(..., min_length=1, max_length=64, description="车次编号")
    vehicle_no: str = Field(
        ..., min_length=1, max_length=64, description="车牌号 / 车辆编号"
    )
    note: str | None = Field(default=None, max_length=256, description="备注")


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trip_id: str
    vehicle_no: str
    note: str | None
    created_at: datetime


class LoadingItem(BaseModel):
    """登记一批中的一条：某票货物在该车次上的装卸时段。"""

    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    loaded_at: datetime = Field(..., description="装载时刻（区间起点，含）")
    unloaded_at: datetime = Field(..., description="卸载时刻（区间终点，不含）")


class ManifestCreate(BaseModel):
    """整批登记一个车次承载的多票货物装卸时段（原子：任一非法整批拒绝）。

    车次编号取自路径参数，请求体无需也不应提供 trip_id。
    """

    trip_id: str | None = Field(default=None, max_length=64)
    items: list[LoadingItem] = Field(
        ...,
        min_length=1,
        description="本批装卸时段登记（至少一条）；同货物时段不得与既有/同批重叠",
    )


class LoadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trip_id: str
    cargo_id: str
    loaded_at: datetime
    unloaded_at: datetime


class ManifestOut(BaseModel):
    trip_id: str
    items: list[LoadingOut]


class VehicleReadingCreate(BaseModel):
    reading_id: str = Field(
        ..., min_length=1, max_length=64, description="车载读数编号（客户端指定）"
    )
    trip_id: str = Field(..., min_length=1, max_length=64, description="所属车次编号")
    read_at: datetime = Field(..., description="读数时刻（ISO 8601，建议带时区）")
    temperature: Decimal = Field(..., description="车载探头温度（摄氏度）")


class VehicleReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reading_id: str
    trip_id: str
    read_at: datetime
    # 原始读数温度；读数更正后仍保持原值，可查
    temperature: Decimal
    # 更正后的有效温度与当前版本（无更正时版本 0 且等于原始值）
    effective_temperature: Decimal = Field(
        ..., description="更正后的有效温度；未更正时等于原始温度"
    )
    temperature_version: int = Field(
        ..., description="读数当前版本：0=原始值，>0=已更正到第 N 版"
    )
    created_at: datetime


class VehicleReadingReplay(BaseModel):
    """同编号同内容重传：返回原读数，并给出本读数适用/影响的货物。"""

    replayed: bool = True
    reading: VehicleReadingOut
    affected_cargo_ids: list[str] = Field(
        ...,
        description="读数时刻仍在车上的货物（按各自温度上限判断越温；可能为空）",
    )
    message: str


class VehicleReadingAck(BaseModel):
    """车载读数新写入回执：含本次受影响货物。"""

    reading: VehicleReadingOut
    affected_cargo_ids: list[str] = Field(
        ..., description="读数时刻仍在车上、按各自温度上限受本读数影响的货物"
    )


class ReadingApplication(BaseModel):
    """一笔车载读数对一票货物的适用性/采用情况（车次查询用）。"""

    cargo_id: str
    on_board: bool = Field(..., description="读数时刻该货物是否仍在车上（左闭右开）")
    adopted: bool = Field(
        ...,
        description="是否被该货物越温时间线采用：在车上且同刻无自有测温时为 true",
    )
    over_limit: bool | None = Field(
        ...,
        description="按该货物自身温度上限、以有效温度判断是否越温；不在车上时为 null",
    )
    applied_temperature: Decimal | None = Field(
        ...,
        description="实际用于该货物判断的温度：在车上为读数有效温度"
        "（含更正），同刻有自有测温时为该自有测温的有效温度；不在车上为 null",
    )
    reading_temperature: Decimal = Field(
        ..., description="读数当前有效温度（含更正；原始温度见 readings[].temperature）"
    )
    reading_temperature_version: int = Field(
        ..., description="读数温度版本：0=原始值，>0=已更正到第 N 版"
    )
    reason: str = Field(
        ...,
        description="未采用原因：adopted=已采用 / not_on_board=不在车上 / "
        "supplanted_by_own=同刻有货物自有测温（原始读数仍可查）"
    )


class TripReadingOut(VehicleReadingOut):
    applications: list[ReadingApplication] = Field(
        ..., description="对车次上各票已登记货物的适用性，按货物编号排序"
    )


class TripDetail(TripOut):
    """车次详情：装卸时段 + 车载原始读数（含对各货物的采用情况）。"""

    loadings: list[LoadingOut]
    readings: list[TripReadingOut]


class TimelinePointOut(BaseModel):
    """货物合并读数时间线上的一个点（查询各货物采用的读数来源）。"""

    occurred_at: datetime
    point_type: str = Field(..., description="measurement=测温；handover=交接")
    source: str = Field(
        ...,
        description="读数来源：own=货物自有事件；vehicle=车载读数；交接恒为 own",
    )
    event_id: str | None = Field(..., description="自有事件ID（车载点为 null）")
    reading_id: str | None = Field(..., description="车载读数ID（自有为 null）")
    trip_id: str | None = Field(..., description="车载读数所属车次")
    temperature: Decimal | None = Field(
        ..., description="该时刻采用的有效温度（含更正）；交接为 null"
    )
    original_temperature: Decimal | None = Field(
        ...,
        description="原始录入温度（测温点；被更正时与 temperature 不同）；交接为 null",
    )
    temperature_version: int = Field(
        ..., description="测温版本：0=原始值，>0=已更正到第 N 版；交接为 0"
    )
    giver: str | None
    receiver: str | None
    adopted: bool = Field(
        ...,
        description="车载读数是否被采用：同刻有自有测温时为 false（原始仍可查）"
    )
    changes_holder: bool = Field(
        ..., description="是否为改变保管方的交接（仅自有交接为 true）"
    )


class CargoTimeline(BaseModel):
    cargo_id: str
    points: list[TimelinePointOut] = Field(
        ..., description="合并读数时间线（含被自有测温取代的车载原始读数），按时间"
    )


# ----------------------------------------------------- 质控：同时越温查询


class SimultaneousExcursionSegment(BaseModel):
    """同时越温片段：窗口内越温货物集合恒定，且数量达到门槛。"""

    start: datetime = Field(..., description="片段起点（含，UTC）")
    end: datetime = Field(..., description="片段终点（不含，UTC；未闭合区间已裁至查询终点）")
    cargo_count: int = Field(..., description="该片段同时越温的货物数量")
    cargo_ids: list[str] = Field(
        ..., description="该片段同时越温的货物编号（按编号排序）"
    )


class SimultaneousExcursionReport(BaseModel):
    """同一车次同时越温时段查询结果（按时间排序的不重叠片段）。"""

    trip_id: str
    start_at: datetime = Field(..., description="查询窗起点（含）")
    end_at: datetime = Field(..., description="查询窗终点（不含）")
    min_cargo: int = Field(..., description="生效的数量门槛")
    segments: list[SimultaneousExcursionSegment] = Field(
        ...,
        description="同时越温货物数达到门槛的正长度片段；集合变化即分段，"
        "相邻同集合片段已合并；无匹配时为空列表",
    )


# ----------------------------------------------------- 测温值更正


class TemperatureCorrectionCreate(BaseModel):
    """测温值更正提交。

    目标二选一：``event_id``（货物自有测温事件）或 ``reading_id``（车载读数）；
    只改温度，时刻 / 归属 / 原编号不变。``expected_version`` 为提交前查询到的
    目标当前版本（从未更正时为 0），与服务端不一致即拒绝（版本过期）。
    """

    correction_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="更正编号（客户端指定）；同编号同内容重传返回原结果",
    )
    event_id: str | None = Field(
        default=None, min_length=1, max_length=64, description="目标：自有测温事件ID"
    )
    reading_id: str | None = Field(
        default=None, min_length=1, max_length=64, description="目标：车载读数ID"
    )
    expected_version: int = Field(
        ..., ge=0, description="预期的目标当前版本（从未更正为 0）"
    )
    new_temperature: Decimal = Field(..., description="更正后的温度（摄氏度）")


class TemperatureCorrectionOut(BaseModel):
    """一条测温更正记录。"""

    model_config = ConfigDict(from_attributes=True)

    correction_id: str
    target_type: str = Field(..., description="目标类型：own=自有测温；vehicle=车载读数")
    event_id: str | None = Field(..., description="自有测温目标事件ID（车载更正为 null）")
    reading_id: str | None = Field(..., description="车载目标读数ID（自有更正为 null）")
    cargo_id: str | None = Field(
        ..., description="自有更正的归属货物；车载更正为 null（影响在车全部货物）"
    )
    expected_version: int
    version: int = Field(..., description="本次更正后的目标版本（1 起递增）")
    new_temperature: Decimal = Field(..., description="更正后的有效温度")
    created_at: datetime


class TemperatureCorrectionAck(BaseModel):
    """测温更正回执：更正记录 + 当前有效温度/版本 + 受影响货物。"""

    correction: TemperatureCorrectionOut
    target_type: str
    target_id: str = Field(..., description="目标编号（事件ID 或 读数ID）")
    cargo_id: str | None = Field(..., description="自有更正的归属货物；车载为 null")
    effective_temperature: Decimal = Field(..., description="更正后的当前有效温度")
    temperature_version: int = Field(..., description="更正后的当前版本")
    affected_cargo_ids: list[str] = Field(
        ...,
        description="本次更正影响的货物：自有=归属货物单元素；"
        "车载=读数时刻仍在车上的全部货物（可能为空）",
    )


class TemperatureCorrectionReplay(BaseModel):
    """同更正ID同内容重传：返回原结果（幂等）。"""

    replayed: bool = True
    correction: TemperatureCorrectionOut
    target_type: str
    target_id: str
    cargo_id: str | None
    effective_temperature: Decimal
    temperature_version: int
    affected_cargo_ids: list[str]
    message: str


class TemperatureCorrectionHistory(BaseModel):
    """某测温目标（自有事件 / 车载读数）的原始温度与历次更正。"""

    target_type: str
    target_id: str
    cargo_id: str | None = Field(..., description="自有测温的归属货物；车载为 null")
    trip_id: str | None = Field(..., description="车载读数所属车次；自有为 null")
    target_at: datetime = Field(
        ..., description="测温时刻（自有事件 occurred_at / 读数 read_at，更正不改变）"
    )
    original_temperature: Decimal = Field(..., description="原始录入温度（永不改变）")
    current_version: int = Field(..., description="当前版本：0=未更正")
    effective_temperature: Decimal = Field(..., description="当前有效温度")
    corrections: list[TemperatureCorrectionOut] = Field(
        ..., description="历次更正（按版本升序）；未更正时为空列表"
    )
