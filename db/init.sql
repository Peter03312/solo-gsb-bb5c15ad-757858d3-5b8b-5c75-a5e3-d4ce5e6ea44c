-- 冷链追责服务 数据库初始化脚本
-- 由 PostgreSQL 官方镜像在数据卷首次创建时自动执行（/docker-entrypoint-initdb.d）。
-- 应用启动时也会执行 CREATE TABLE IF NOT EXISTS 作为兜底。

CREATE TYPE event_type AS ENUM ('measurement', 'handover');
CREATE TYPE stance_kind AS ENUM ('confirm', 'dispute');
CREATE TYPE interval_source AS ENUM ('own', 'vehicle');
CREATE TYPE correction_target AS ENUM ('own', 'vehicle');

CREATE TABLE IF NOT EXISTS cargo (
    cargo_id          VARCHAR(64)   PRIMARY KEY,
    started_at        TIMESTAMPTZ   NOT NULL,
    temp_upper_limit  NUMERIC(10,3) NOT NULL,
    initial_holder    VARCHAR(128)  NOT NULL,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS event (
    event_id     VARCHAR(64)   PRIMARY KEY,
    cargo_id     VARCHAR(64)   NOT NULL
                     REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    event_type   event_type    NOT NULL,
    occurred_at  TIMESTAMPTZ   NOT NULL,
    temperature  NUMERIC(10,3),
    giver        VARCHAR(128),
    receiver     VARCHAR(128),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 事件形状约束：测温只有温度；交接只有交出方/接收方且双方不同
    CONSTRAINT chk_event_shape CHECK (
        (event_type = 'measurement'
            AND temperature IS NOT NULL
            AND giver IS NULL
            AND receiver IS NULL)
        OR
        (event_type = 'handover'
            AND temperature IS NULL
            AND giver IS NOT NULL
            AND receiver IS NOT NULL
            AND giver <> receiver)
    )
);

CREATE INDEX IF NOT EXISTS ix_event_cargo_time
    ON event (cargo_id, occurred_at);

-- 同一货物同一时刻至多一笔测温
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_cargo_time_measurement
    ON event (cargo_id, occurred_at)
    WHERE event_type = 'measurement';

-- 同一货物同一时刻至多一笔交接
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_cargo_time_handover
    ON event (cargo_id, occurred_at)
    WHERE event_type = 'handover';

-- 冷链车次（一个车次可承载多票货物）
CREATE TABLE IF NOT EXISTS trip (
    trip_id      VARCHAR(64)   PRIMARY KEY,
    vehicle_no   VARCHAR(64)   NOT NULL,
    note         VARCHAR(256),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 一票货物在一个车次上的装卸时段：左闭右开 [loaded_at, unloaded_at)
CREATE TABLE IF NOT EXISTS loading (
    trip_id      VARCHAR(64)   NOT NULL
                     REFERENCES trip(trip_id) ON DELETE RESTRICT,
    cargo_id     VARCHAR(64)   NOT NULL
                     REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    loaded_at    TIMESTAMPTZ   NOT NULL,
    unloaded_at  TIMESTAMPTZ   NOT NULL,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 左闭右开且起点严格早于终点
    CONSTRAINT chk_loading_window CHECK (loaded_at < unloaded_at),
    PRIMARY KEY (trip_id, cargo_id, loaded_at)
);

CREATE INDEX IF NOT EXISTS ix_loading_cargo
    ON loading (cargo_id, loaded_at, unloaded_at);

-- 车载测温原始读数（仅对读数时刻仍在车上的货物按其各自上限判断越温；
-- 是否被某货物采用——同刻有自有测温时被取代——在合并时间线时计算）
CREATE TABLE IF NOT EXISTS vehicle_reading (
    reading_id   VARCHAR(64)   PRIMARY KEY,
    trip_id      VARCHAR(64)   NOT NULL
                     REFERENCES trip(trip_id) ON DELETE RESTRICT,
    read_at      TIMESTAMPTZ   NOT NULL,
    temperature  NUMERIC(10,3) NOT NULL,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 同车次同一时刻至多一笔车载读数
CREATE UNIQUE INDEX IF NOT EXISTS ux_vehicle_reading_trip_time
    ON vehicle_reading (trip_id, read_at);

CREATE INDEX IF NOT EXISTS ix_vehicle_reading_trip
    ON vehicle_reading (trip_id, read_at);

-- 责任确认表态（只追加：改口产生新编号，历次表态永久保留）
CREATE TABLE IF NOT EXISTS responsibility_stance (
    stance_id           VARCHAR(64)   PRIMARY KEY,
    cargo_id            VARCHAR(64)   NOT NULL
                          REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    -- 稳定区间标识：自有开启=测温事件ID；车载开启=veh:<读数ID>
    interval_key        VARCHAR(68)   NOT NULL,
    -- 开启读数来源（与下方两组坐标互斥对应）
    opener_source       interval_source NOT NULL,
    start_event_id      VARCHAR(64),   -- opener_source='own' 时有值
    vehicle_reading_id  VARCHAR(64),   -- opener_source='vehicle' 时有值
    responsible_party   VARCHAR(128)  NOT NULL,
    kind                stance_kind   NOT NULL,
    reason              VARCHAR(512),
    -- 提交时校验通过的快照标识（sha256:<hex>）；区间/片段变化后与当前快照不再一致
    snapshot_id         VARCHAR(71)   NOT NULL,
    -- 表态时该责任方承担的片段坐标 JSON（[["起","止","责任方"],...]），
    -- 晚到事件/读数重建后逐片段判定表态是否仍有效
    attested_segments   VARCHAR(2000) NOT NULL,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 表态形状：确认不得带理由；异议必须带非空理由
    CONSTRAINT chk_stance_shape CHECK (
        (kind = 'confirm' AND reason IS NULL)
        OR
        (kind = 'dispute' AND reason IS NOT NULL AND char_length(reason) > 0)
    ),

    -- 开启坐标二选一：自有事件 或 车载读数
    CONSTRAINT chk_stance_opener CHECK (
        (opener_source = 'own'
            AND start_event_id IS NOT NULL
            AND vehicle_reading_id IS NULL)
        OR
        (opener_source = 'vehicle'
            AND vehicle_reading_id IS NOT NULL
            AND start_event_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_stance_cargo_interval
    ON responsibility_stance (cargo_id, interval_key);

-- 测温值更正（只追加；原始 event / vehicle_reading 温度永不修改）。
-- 凭 自有测温事件ID 或 车载读数ID 之一 + 更正ID + 预期版本 + 新温度提交；
-- 只改温度，时刻 / 归属 / 原编号不变，交接不可更正。
-- 车载更正不记录归属货物（cargo_id 为空）：对读数时刻在车的全部货物生效。
CREATE TABLE IF NOT EXISTS temperature_correction (
    correction_id      VARCHAR(64)   PRIMARY KEY,
    target_type        correction_target NOT NULL,
    event_id           VARCHAR(64),   -- target_type='own' 时有值（测温事件）
    reading_id         VARCHAR(64),   -- target_type='vehicle' 时有值（车载读数）
    cargo_id           VARCHAR(64),   -- 自有更正的归属货物；车载更正为空
    expected_version   INTEGER       NOT NULL,
    version            INTEGER       NOT NULL,
    new_temperature    NUMERIC(10,3) NOT NULL,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 目标坐标二选一：自有测温事件（须带归属货物） 或 车载读数（不带归属货物）
    CONSTRAINT chk_correction_target CHECK (
        (target_type = 'own'
            AND event_id IS NOT NULL
            AND reading_id IS NULL
            AND cargo_id IS NOT NULL)
        OR
        (target_type = 'vehicle'
            AND reading_id IS NOT NULL
            AND event_id IS NULL)
    ),

    -- 版本：首次提交 expected=0、version=1，依次递增
    CONSTRAINT chk_correction_version CHECK (expected_version >= 0 AND version >= 1)
);

-- 同一目标的版本号唯一（每个版本只能由一笔更正产生）。
-- PG 下货物行锁已把同目标更正串行；该唯一索引同时为无行锁环境（如 SQLite）
-- 提供数据库级兜底，杜绝并发产生两条相同版本（落败方按 stale_version 拒绝）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_correction_own_event_version
    ON temperature_correction (event_id, version)
    WHERE target_type = 'own';

CREATE UNIQUE INDEX IF NOT EXISTS ux_correction_reading_version
    ON temperature_correction (reading_id, version)
    WHERE target_type = 'vehicle';
