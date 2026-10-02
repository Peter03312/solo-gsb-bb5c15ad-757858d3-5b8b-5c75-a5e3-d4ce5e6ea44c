"""测温更正后的“有效温度”解析（纯逻辑 + 读取辅助）。

更正只追加（见 :class:`app.models.TemperatureCorrection`）：原始测温事件 /
车载读数表永不修改，每次更正产生一条新版本记录。重建合并读数时间线时，
测温点的温度取其目标**最新版本**更正后的温度——即“有效温度”；没有更正时
有效温度就是原始温度，版本为 0。

* 货物自有测温点：按 event_id 索引；
* 车载测温点：按 reading_id 索引（对读数时刻在车的全部货物统一生效）。

时间、归属与原编号不变；交接点没有温度，也不允许更正。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select

from app.models import CorrectionTarget, TemperatureCorrection
from app.timeutil import ensure_utc

TARGET_OWN = "own"
TARGET_VEHICLE = "vehicle"


@dataclass(frozen=True)
class EffectiveTemperatures:
    """目标ID -> 当前有效温度覆盖（无覆盖即沿用原始温度，版本 0）。"""

    own: dict[str, Decimal]
    vehicle: dict[str, Decimal]
    own_versions: dict[str, int]
    vehicle_versions: dict[str, int]

    @classmethod
    def empty(cls) -> "EffectiveTemperatures":
        return cls({}, {}, {}, {})

    @classmethod
    def from_rows(cls, rows) -> "EffectiveTemperatures":
        own: dict[str, Decimal] = {}
        vehicle: dict[str, Decimal] = {}
        own_ver: dict[str, int] = {}
        vehicle_ver: dict[str, int] = {}
        for c in rows:
            is_own = c.target_type is CorrectionTarget.own or c.target_type == TARGET_OWN
            key = c.event_id if is_own else c.reading_id
            temp = Decimal(c.new_temperature)
            if is_own:
                if key not in own_ver or c.version > own_ver[key]:
                    own_ver[key] = c.version
                    own[key] = temp
            else:
                if key not in vehicle_ver or c.version > vehicle_ver[key]:
                    vehicle_ver[key] = c.version
                    vehicle[key] = temp
        return cls(own, vehicle, own_ver, vehicle_ver)

    def own_temperature(self, event_id: str, original: Decimal):
        """返回 (有效温度, 版本)；无更正时为 (原始值, 0)。"""
        return self.own.get(event_id, Decimal(original)), self.own_versions.get(event_id, 0)

    def vehicle_temperature(self, reading_id: str, original: Decimal):
        """返回 (有效温度, 版本)；无更正时为 (原始值, 0)。"""
        return (
            self.vehicle.get(reading_id, Decimal(original)),
            self.vehicle_versions.get(reading_id, 0),
        )

    def own_version_of(self, event_id: str) -> int:
        return self.own_versions.get(event_id, 0)

    def vehicle_version_of(self, reading_id: str) -> int:
        return self.vehicle_versions.get(reading_id, 0)


def load_target_corrections(db, *, event_ids=None, reading_ids=None) -> EffectiveTemperatures:
    """按目标ID集合读取更正并折叠为当前有效温度（取同目标最大版本）。"""
    event_ids = list(event_ids or [])
    reading_ids = list(reading_ids or [])
    if not event_ids and not reading_ids:
        return EffectiveTemperatures.empty()
    conds = []
    if event_ids:
        conds.append(TemperatureCorrection.event_id.in_(event_ids))
    if reading_ids:
        conds.append(TemperatureCorrection.reading_id.in_(reading_ids))
    stmt = select(TemperatureCorrection).where(or_(*conds))
    rows = list(db.scalars(stmt).all())
    for r in rows:
        r.created_at = ensure_utc(r.created_at)
    return EffectiveTemperatures.from_rows(rows)


def current_version(db, target_type: str, target_id: str) -> int:
    """某目标当前更正版本（无更正为 0）。"""
    col = (
        TemperatureCorrection.event_id
        if target_type == TARGET_OWN
        else TemperatureCorrection.reading_id
    )
    rows = db.scalars(
        select(TemperatureCorrection.version).where(col == target_id)
    ).all()
    return max(rows, default=0)


def list_corrections(db, target_type: str, target_id: str) -> list[TemperatureCorrection]:
    """某目标的历次更正（按版本升序）。"""
    col = (
        TemperatureCorrection.event_id
        if target_type == TARGET_OWN
        else TemperatureCorrection.reading_id
    )
    rows = list(
        db.scalars(
            select(TemperatureCorrection)
            .where(col == target_id)
            .order_by(TemperatureCorrection.version, TemperatureCorrection.correction_id)
        ).all()
    )
    for r in rows:
        r.created_at = ensure_utc(r.created_at)
    return rows
