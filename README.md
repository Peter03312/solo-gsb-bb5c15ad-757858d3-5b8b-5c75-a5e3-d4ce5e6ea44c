# 冷链货物追责服务

面向冷链货物追责的后端服务：录入货物与**自有测温 / 交接事件**、**冷链车次、每票货物的装卸时段与车载测温读数**，在**合并读数时间线**上按发生时间重建保管链，自动识别越温区间并在交接时刻拆分责任。技术栈 **Python 3.11 + FastAPI + SQLAlchemy 2 + PostgreSQL**。

车载测温（本轮）：同一冷链车承载多票货物时，可登记车次、每票货物的装卸时段（左闭右开）与车载探头读数；车载读数仅作用于读数时刻仍在车上的货物，并按**各自温度上限**判断越温；同刻有货物自有测温时**优先采用自有值**（车载原始读数仍可查）；交接仍只由货物自有事件改变保管方。质控接口 `GET /trips/{trip_id}/simultaneous-excursions` 按带时区查询窗返回同一车次上**多票货物同时越温**的时段（数量门槛 `min_cargo≥2`，货物集合变化即分段）。

测温更正（本轮）：测温值录错后，可凭**自有测温事件ID 或车载读数ID（二选一）+ 更正ID + 预期版本 + 新温度**提交更正。更正**只改温度**：测温时间、货物归属与原编号一律不变，**交接不可更正**；原始记录永不修改，每次更正生成新版本，历次更正可查。原始事件 / 读数查询的 `temperature` 恒为**原始录入值**，另给 `effective_temperature`（有效温度）与 `temperature_version`（版本）。货物时间线、越温报告、责任确认与车次同时越温查询一律按**有效温度**重建；同刻自有测温优先与装卸时段左闭右开规则继续适用；车载读数更正同时影响读数时刻仍在车上的全部货物。更正与相关事件、责任表态、车载读数及装卸登记在同一套行锁下串行。

## 一、一键启动（Docker Compose，含数据库初始化）

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
| --- | --- |
| API 服务 | http://localhost:8000 |
| 交互式接口文档 Swagger UI | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/库/密码默认均为 `coldchain`） |

首次启动时，`db/init.sql` 由 PostgreSQL 官方镜像自动执行（建枚举类型、表、CHECK 约束、部分唯一索引）。应用启动时也会执行 `CREATE TABLE IF NOT EXISTS` 作为兜底。API 容器通过 `depends_on: service_healthy` 等待数据库就绪。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据库数据卷（下次重新初始化）
```

## 二、本地开发（不走 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 需要一个可连接的 PostgreSQL，并先执行 db/init.sql
export DATABASE_URL="postgresql+psycopg2://coldchain:coldchain@localhost:5432/coldchain"
uvicorn app.main:app --reload --port 8000
```

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://coldchain:coldchain@db:5432/coldchain` | SQLAlchemy 数据库连接串（容器内主机名为 `db`） |
| `POSTGRES_USER` | `coldchain` | 数据库用户（compose） |
| `POSTGRES_PASSWORD` | `coldchain` | 数据库密码（compose） |
| `POSTGRES_DB` | `coldchain` | 数据库名（compose） |
| `API_PORT` | `8000` | 宿主机映射的 API 端口 |
| `DB_PORT` | `5432` | 宿主机映射的数据库端口 |

也可在项目根目录放 `.env` 文件供 compose 读取。时间字段统一按 UTC 存储；请求时间建议带时区偏移（如 `+08:00`），无时区时间按 UTC 解释。

## 四、HTTP API

基址：`http://localhost:8000`。所有请求/响应均为 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/cargo` | 创建货物 |
| `GET` | `/cargo` | 货物列表 |
| `GET` | `/cargo/{cargo_id}` | 货物详情 |
| `POST` | `/events` | 写入货物**自有**测温 / 交接事件 |
| `GET` | `/cargo/{cargo_id}/events` | 按发生时间列出**自有事件**（不含车载读数） |
| `GET` | `/cargo/{cargo_id}/report` | **追责报告**：越温区间（含读数来源）+ 责任片段 |
| `GET` | `/cargo/{cargo_id}/timeline` | **合并读数时间线**：自有事件 + 适用的车载读数（含被取代点） |
| `GET` | `/cargo/{cargo_id}/responsibilities` | **责任确认**：全部已闭合区间的确认状态 |
| `GET` | `/cargo/{cargo_id}/responsibilities/{interval_id}` | 单个已闭合区间的责任确认视图 |
| `POST` | `/cargo/{cargo_id}/responsibilities/{interval_id}/stances` | 提交责任方表态（确认 / 异议） |
| `GET` | `/cargo/{cargo_id}/responsibilities/{interval_id}/stances` | 区间历次表态历史（含已改口 / 已失效） |
| `POST` | `/trips` | 登记车次 |
| `GET` | `/trips` | 车次列表 |
| `GET` | `/trips/{trip_id}` | 车次详情：装卸时段、车载原始读数及对各货物的适用性 |
| `POST` | `/trips/{trip_id}/manifest` | 批量登记车次上各票货物的装卸时段（原子） |
| `POST` | `/vehicle-readings` | 写入车载测温读数（允许晚到；幂等 / 冲突） |
| `GET` | `/trips/{trip_id}/simultaneous-excursions` | **质控：同一车次多票货物同时越温时段** |
| `POST` | `/temperature-corrections` | **提交测温值更正**（自有测温事件 / 车载读数，二选一定位） |
| `GET` | `/temperature-corrections/{correction_id}` | 按更正ID查询单条更正记录 |
| `GET` | `/events/{event_id}/corrections` | 自有测温事件的原始温度 + 历次更正 + 当前有效温度 |
| `GET` | `/vehicle-readings/{reading_id}/corrections` | 车载读数的原始温度 + 历次更正 + 当前有效温度 |
| `GET` | `/health` | 健康检查 |

### 1. 创建货物 `POST /cargo`

```json
{
  "cargo_id": "COLD-001",
  "started_at": "2026-09-30T08:00:00+08:00",
  "temp_upper_limit": "4.0",
  "initial_holder": "北京冷库"
}
```

字段：货物编号、起始时间、温度上限（℃）、起始保管方。编号重复返回 `409 cargo_exists`。

### 2. 写入事件 `POST /events`

两种事件由请求字段区分（互斥，不能同时出现）：

测温（含 `temperature`）：

```json
{ "event_id": "evt-1", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T10:00:00+08:00", "temperature": "8.2" }
```

交接（含 `giver` 与 `receiver`）：

```json
{ "event_id": "evt-2", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T12:00:00+08:00",
  "giver": "北京冷库", "receiver": "上海冷库" }
```

每个事件都带 **事件ID、货物编号、发生时间**。

### 2.1 登记车次 `POST /trips`

```json
{ "trip_id": "TRUCK-1", "vehicle_no": "京B-冷888", "note": "京沪冷链班" }
```

车次编号重复返回 `409 trip_exists`，车次不存在返回 `404 trip_not_found`。

### 2.2 登记装卸时段 `POST /trips/{trip_id}/manifest`

一个车次承载多票货物时，**批量**登记每票货物的装卸时段：

```json
{
  "items": [
    { "cargo_id": "COLD-001",
      "loaded_at": "2026-09-30T09:00:00+08:00",
      "unloaded_at": "2026-09-30T18:00:00+08:00" },
    { "cargo_id": "COLD-002",
      "loaded_at": "2026-09-30T09:00:00+08:00",
      "unloaded_at": "2026-09-30T18:00:00+08:00" }
  ]
}
```

* 时段**左闭右开** `[loaded_at, unloaded_at)`，且起点必须**严格早于**终点，否则
  `422 invalid_loading_window`；
* 同一货物的装卸时段不得与任意车次的既有时段重叠（首尾相接可以）——重叠即“同货物同时
  属于两个车次”，返回 `409 loading_overlap`；同批内重叠同样拒绝；
* 货物或车次不存在分别返回 `404 cargo_not_found` / `404 trip_not_found`；
* 本接口为**批量原子**操作：任一条非法整批拒绝，**不留任何部分记录**。

### 2.3 写入车载测温读数 `POST /vehicle-readings`

```json
{
  "reading_id": "vr-1",
  "trip_id": "TRUCK-1",
  "read_at": "2026-09-30T10:00:00+08:00",
  "temperature": "7.0"
}
```

读数是车次探头的**原始读数**，写入**允许晚到**（乱序补录）。回执给出读数时刻仍在车上、
按各自温度上限受影响的货物：

```json
{ "reading": { "reading_id": "vr-1", "trip_id": "TRUCK-1",
               "read_at": "2026-09-30T02:00:00Z", "temperature": "7.000", "…": "…" },
  "affected_cargo_ids": ["COLD-001"] }
```

* 读数仅作用于读数时刻仍在**同一车次**车上的货物
  （`loaded_at ≤ read_at < unloaded_at`，左闭右开），与该货物无关的车次读数不入其时间线；
* 同车次同一时刻至多一笔读数：冲突 `409 same_timestamp_conflict`；
* 编号幂等：`reading_id` **同内容**重传返回 `200 {"replayed": true, …}`（仍给出受影响货物），
  **异内容**返回 `409 reading_id_conflict`，原记录不变；车次不存在 `404 trip_not_found`；
* 读数时刻车上无货物时读数**仍正常入库**（原始可查），仅 `affected_cargo_ids` 为空。

### 2.4 车次详情 `GET /trips/{trip_id}`

返回车次信息、全部装卸时段，以及车载原始读数；每笔读数对车次上**每票已登记货物**给出：

| 字段 | 含义 |
| --- | --- |
| `on_board` | 读数时刻该货物是否仍在车上（左闭右开边界） |
| `adopted` | 是否被该货物越温时间线**采用**（在车上且同刻无自有测温） |
| `over_limit` | 按**该货物自身温度上限**判断是否越温；不在车上时为 `null` |
| `reason` | `adopted` 已采用 / `not_on_board` 不在车上 / `supplanted_by_own` 同刻有自有测温被取代 |

### 2.5 货物读数时间线 `GET /cargo/{cargo_id}/timeline`

返回该货物的**合并读数时间线**（按时间）：货物自有测温 / 交接，以及对其适用的车载原始读数。
每个点带 `source`（`own` / `vehicle`）、`event_id` / `reading_id` / `trip_id`、
`adopted`（车载点同刻被自有测温取代时为 `false`，原始读数仍列出）、
`changes_holder`（仅自有交接为 `true`——车载读数不改变保管方）。

### 2.6 质控：同时越温时段 `GET /trips/{trip_id}/simultaneous-excursions`

同一车次承载多票货物时，查找**同时越温**的时段。查询参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `trip_id`（路径） | 是 | 车次编号；未知车次返回 `404 trip_not_found` |
| `start_at` | 是 | 查询起点（**含**），ISO 8601 且**必须带时区**（如 `2026-09-30T08:00:00+08:00`） |
| `end_at` | 是 | 查询终点（**不含**，左闭右开），同样必须带时区；不严格晚于起点返回 `422 invalid_query_window` |
| `min_cargo` | 否 | 数量门槛（正整数，**不小于 2**，缺省 `2`）；非法返回 `422 invalid_min_cargo` |

```bash
curl -G "http://localhost:8000/trips/TRUCK-1/simultaneous-excursions" \
  --data-urlencode "start_at=2026-09-30T00:00:00+08:00" \
  --data-urlencode "end_at=2026-10-01T00:00:00+08:00" \
  --data-urlencode "min_cargo=2"
```

处理规则：

* 对每票货物按**现有合并时间线**重建越温区间（同刻自有测温优先，车载读数仅对
  在车货物按各自上限生效），再与该货物在**本车次**的装载时段（左闭右开
  `[loaded_at, unloaded_at)`）及查询窗相交；货物从上一车次带着未闭合区间装车时，
  自本车次装车时刻起计入；
* **未闭合区间**先被卸货时刻截断，再裁剪至查询终点；
* 同一次查询基于**一致的已提交数据视图**（PostgreSQL 下以 REPEATABLE READ
  快照事务执行）；晚到读数 / 事件**提交后**的下一次查询即反映新结果；
* 扫描线叠加：**货物集合变化即分段**，相邻且集合相同的片段合并，仅保留越温
  货物数达到门槛的**正长度**片段；片段按时间升序、互不重叠，货物编号按序排列。

```json
{
  "trip_id": "TRUCK-1",
  "start_at": "2026-09-29T16:00:00Z",
  "end_at": "2026-09-30T16:00:00Z",
  "min_cargo": 2,
  "segments": [
    {
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T03:00:00Z",
      "cargo_count": 2,
      "cargo_ids": ["COLD-CAR-A", "COLD-CAR-Q"]
    }
  ]
}
```

无任何达门槛片段时返回 `200` 且 `segments` 为空数组。响应时间统一为 UTC（`Z`）。

### 2.7 测温值更正 `POST /temperature-corrections`

测温值录错（测温探头 / 人工录入错误）后，凭**目标编号 + 更正ID + 预期版本 + 新温度**
提交更正。目标二选一：

* `event_id`：货物**自有测温事件**ID（交接事件不可更正）；
* `reading_id`：**车载测温读数**ID。

```json
{
  "correction_id": "corr-1",
  "event_id": "evt-temp-1",
  "expected_version": 0,
  "new_temperature": "2.0"
}
```

车载读数更正把 `event_id` 换成 `reading_id`（此时无需也不能给货物编号——
受影响货物按读数时刻的装卸时段推导）：

```json
{
  "correction_id": "corr-vr-1",
  "reading_id": "vr-1",
  "expected_version": 0,
  "new_temperature": "2.0"
}
```

回执给出更正记录、当前有效温度 / 版本与受影响货物：

```json
{
  "correction": {
    "correction_id": "corr-1", "target_type": "own",
    "event_id": "evt-temp-1", "reading_id": null, "cargo_id": "COLD-001",
    "expected_version": 0, "version": 1,
    "new_temperature": "2.000", "created_at": "2026-10-02T01:00:00Z"
  },
  "target_type": "own", "target_id": "evt-temp-1", "cargo_id": "COLD-001",
  "effective_temperature": "2.000", "temperature_version": 1,
  "affected_cargo_ids": ["COLD-001"]
}
```

处理规则：

* **只改温度**：测温时刻、货物归属（自有）与原编号不变；区间稳定标识
  （开启测温事件ID / `veh:<读数ID>`）也不变；**交接事件不可更正**
  （`422 target_not_correctable`）；
* 原始 `event` / `vehicle_reading` 记录**永不修改**；每次更正生成新版本：
  首次更正后版本为 **1**，同一目标再次更正依次为 2、3……提交时的
  `expected_version` 必须等于目标**当前版本**（从未更正时为 **0**），否则
  `409 stale_version`（版本过期 / 并发被抢先），拒绝且不落部分结果；
* **原始记录查询**的 `temperature` 恒为原始录入值，另显示
  `effective_temperature`（当前有效温度）与 `temperature_version`
  （0=未更正）；货物**合并时间线**点给出采用的有效温度 `temperature`、
  原始温度 `original_temperature` 与 `temperature_version`；
* **按有效值重建**：货物时间线、越温报告、责任确认（快照 / 表态有效性 /
  汇总）及车次同时越温查询全部以有效温度重新计算；**历史表态保留**，其当前
  有效性按重建后的责任片段判断（既有规则不变）；
* **同刻自有优先、装卸左闭右开继续适用**：被同刻自有测温取代的车载读数
  即使更正也仍被取代；车载读数更正**同时影响读数时刻仍在该车次车上的
  全部货物**（各自按自身温度上限判断），车次详情对每票货物给出采用的
  有效温度 `applied_temperature` 与读数版本；
* **幂等 / 冲突**：
  - 原始事件 / 读数按**原内容**重传仍幂等（`200 replayed`），原始值不被更正影响；
  - `correction_id` **同内容**重传返回 `200 {"replayed": true, …}`（返回原结果，
    受影响货物按当前装卸关系给出）；**异内容**返回 `409 correction_id_conflict`；
  - 目标不存在：自有事件 `404 event_not_found`、车载读数 `404 reading_not_found`；
    同时 / 均未给两个目标 → `422 bad_correction_shape`。

### 2.8 更正历史查询

* `GET /events/{event_id}/corrections`（自有测温事件）；
* `GET /vehicle-readings/{reading_id}/corrections`（车载读数）。

返回目标的测温时刻（更正不改变）、**原始温度** `original_temperature`、当前版本
`current_version`、**当前有效温度** `effective_temperature` 与按版本升序的历次
更正 `corrections`。按更正ID直查单条：`GET /temperature-corrections/{correction_id}`
（不存在 `404 correction_not_found`）。

```json
{
  "target_type": "own", "target_id": "evt-temp-1", "cargo_id": "COLD-001",
  "trip_id": null, "target_at": "2026-09-30T02:00:00Z",
  "original_temperature": "8.200", "current_version": 1,
  "effective_temperature": "2.000",
  "corrections": [ { "correction_id": "corr-1", "version": 1, "…": "…" } ]
}
```

### 3. 追责报告 `GET /cargo/{cargo_id}/report`

```json
{
  "cargo_id": "COLD-001",
  "temp_upper_limit": "4.000",
  "current_holder": "广州冷库",
  "excursions": [
    {
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "is_open": false,
      "segments": [
        { "start": "2026-09-30T02:00:00Z", "end": "2026-09-30T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "2026-09-30T04:00:00Z", "end": "2026-09-30T06:00:00Z", "responsible_party": "上海冷库" },
        { "start": "2026-09-30T06:00:00Z", "end": "2026-09-30T08:00:00Z", "responsible_party": "广州冷库" }
      ],
      "interval_id": "evt-temp-1",
      "start_source": "own",
      "end_source": "vehicle",
      "start_reading_id": null,
      "end_reading_id": "vr-2",
      "start_trip_id": null,
      "end_trip_id": "TRUCK-1"
    }
  ]
}
```

未闭合区间：`end` 为 `null`、`is_open` 为 `true`，最后一个责任片段的 `end` 也是 `null`（未结束）。区间与片段均按时间升序返回。

区间的读数来源字段：

* `interval_id`：区间**稳定标识**——货物自有测温开启=测温事件ID（如 `evt-temp-1`）；
  **车载读数开启=`veh:<读数ID>`**（如 `veh:vr-1`），供责任查询与表态长期引用；
* `start_source` / `end_source`：开启 / 闭合读数来源 `own`（自有测温）或 `vehicle`（车载读数）；
  开放区间无闭合来源；
* `start_reading_id` / `end_reading_id`（车载时）、`start_trip_id` / `end_trip_id`（所属车次）；
  自有测温时读数ID字段为 `null`、事件坐标见责任确认视图的 `start_event_id` / `end_event_id`。

### 4. 责任确认 `GET /cargo/{cargo_id}/responsibilities[/{interval_id}]`

围绕**已闭合**越温区间开展责任确认（开放区间不参与）。区间以**稳定区间标识** `interval_id` 定位：货物自有测温开启时等于开启测温事件ID，车载读数开启时为 `veh:<读数ID>`。响应同时给出开启 / 闭合读数来源（自有 / 车载）与事件 / 读数坐标、结束测温事件ID（自有闭合时）、责任片段、**当前快照标识**、各责任方最新一条有效表态与汇总状态。

```json
{
  "cargo_id": "COLD-001",
  "intervals": [
    {
      "interval_id": "evt-temp-1",
      "start_event_id": "evt-temp-1",
      "end_event_id": "evt-temp-2",
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "snapshot_id": "sha256:836e6b3a…e797e4",
      "segments": [
        { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "…T04:00:00Z", "end": "…T08:00:00Z", "responsible_party": "广州冷库" }
      ],
      "latest_stances": [
        {
          "stance_id": "stance-bj-1",
          "responsible_party": "北京冷库",
          "stance": "confirm",
          "reason": null,
          "created_at": "2026-09-30T09:00:00Z",
          "segments": [ { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" } ]
        }
      ],
      "summary": "pending"
    }
  ]
}
```

* **快照标识 `snapshot_id`**：`sha256:` 前缀，对「区间起止 + 责任片段序列」的规范化 JSON 取 SHA-256；边界或责任片段一变，快照标识即变。
* **汇总状态 `summary`**：`all_confirmed` 全确认（当前片段中每个责任方最新有效表态均为确认）／`has_dispute` 有异议（最新有效表态中存在异议）／`pending` 待确认（尚有责任方未表态且无异议）。
* 列表接口只返回已闭合区间；直查单个区间时，区间不存在返回 `404 interval_not_found`，区间仍开放返回 `422 interval_open`。

### 5. 提交表态 `POST /cargo/{cargo_id}/responsibilities/{interval_id}/stances`

请求体（货物编号与区间标识在路径上）：

```json
{
  "snapshot_id": "sha256:836e6b3a…e797e4",
  "responsible_party": "上海冷库",
  "stance": "dispute",
  "reason": "该时段温控由交出方远程设定，接收方未实际接管",
  "stance_id": "stance-sh-1"
}
```

* `stance`：`confirm` 确认担责；`dispute` 异议——**异议必须同时提供非空 `reason`**，确认不得带理由。
* `responsible_party` **必须出现在当前责任片段中**，否则 `422 party_not_in_segment`。
* `snapshot_id` 必须等于提交时的当前快照标识；晚到事件导致区间边界 / 责任片段变化后旧快照被拒：`409 stale_snapshot`。
* `stance_id` 为客户端指定的表态编号：
  - **同编号 + 同内容重传**：返回 `200 {"replayed": true, "stance": …原记录…}`（**即使该表态所依据的快照已失效也返回原记录**）；
  - **同编号 + 异内容**：`409 stance_id_conflict`，原记录不变；
  - **改口**（确认⇄异议）必须使用**新表态编号**，产生一条新记录；历次表态全部保留。
* 区间不存在 `404 interval_not_found`、区间仍开放 `422 interval_open`，同样拒绝且不落库。
* 表态提交与同货物的事件写入在事务中共用货物行锁（`SELECT ... FOR UPDATE`）**串行化**：校验用的快照一定来自持锁期间重建出的最新事件链。

### 6. 表态历史 `GET /cargo/{cargo_id}/responsibilities/{interval_id}/stances`

返回区间的历次表态（按提交时间升序），即使区间当前已不存在或重新开放也可查。每条记录附：

* `is_current`：对**当前**快照是否仍有效——晚到事件重建后，所在责任片段边界 / 责任方发生变化的旧表态为 `false`，片段未变化的继续有效；区间消失 / 重新开放时全部为 `false`；
* `superseded`：是否已被同责任方更新的有效表态改口（新编号表态）。

头部还给出 `interval_exists`、`is_open`、`current_snapshot_id`。

## 五、业务规则

1. **时间合法性**：事件发生时间早于货物起始时间 → `422 before_start`，拒绝。
2. **越温区间**：
   - 测温 **高于** 温度上限（`temp > limit`，等于上限不算越温）开启区间；
   - 区间内首次 **不超温**（`temp <= limit`）的测温在其测温时刻闭合区间；
   - 区间内重复超限测温不另开区间；区间外的不超限测温无影响。
3. **责任拆分**：区间跨越交接时，按交接时刻切分——`[区间起, 交接时刻)` 归交出方，**交接时刻起归接收方**；不产生零长度片段。
4. **晚到记录**：事件允许乱序写入。每次写入都把候选事件并入既有事件，按发生时间整体重算受影响区间；因此交接、测温可事后补录。
5. **交接合法性（整体拒绝）**：任何一笔交接的交出方必须等于其发生时刻的保管方；晚到插入若导致此前已存在的交接不再满足该条件，本次写入**整体回滚拒绝，既有结果保持不变**（`422 giver_mismatch`）。
6. **同刻冲突**：同一货物同一时刻至多一笔测温、至多一笔交接（测温与交接可同刻发生，且测温先于交接处理），冲突 → `409 same_timestamp_conflict`。
7. **幂等**：同一 `event_id` 重传：内容完全一致（时间按同一时刻归一化比较）返回 `200 {"replayed": true}` 且不重复写入；内容不一致返回 `409 event_id_conflict`，原记录不变。
8. **并发**：同一货物的写入在事务中以 `SELECT ... FOR UPDATE` 锁定货物行串行化，并由部分唯一索引 / CHECK 约束在数据库层兜底。**责任表态提交与事件写入共用同一把货物行锁**，二者对同一货物严格串行。
9. **责任确认（仅已闭合区间）**：
   - 区间标识 = 开启区间的稳定标识：货物自有测温开启=测温事件ID；**车载读数开启=`veh:<reading_id>`**；快照标识由区间起止与责任片段序列的 SHA-256 决定（与读数来源无关，只取决于时间坐标与责任方）；
   - 表态为只追加记录：同编号同内容重传返回原记录（快照失效亦然），同编号异内容冲突，改口必须用新编号且历次表态保留；
   - 新表态以当前快照校验（过期拒绝），责任方必须在当前片段中；区间不存在 / 开放均拒绝且不落库；
   - 晚到事件 / 晚到车载读数重建后：区间标识变化（开启读数被自有测温取代等）、边界或责任片段变化的旧表态失效、未变化片段上的表态继续有效，全部历史仍可查。
10. **车次与装卸时段**：同一冷链车可承载多票货物；装卸时段左闭右开 `[loaded_at, unloaded_at)` 且起点严格早于终点；同一货物的时段不得跨车次（或同批）重叠，首尾相接允许；登记为批量原子操作，任一非法整批拒绝、不留部分记录。
11. **车载读数适用性**：车载读数只作用于读数时刻仍在**同一车次**车上的货物（`loaded_at ≤ t < unloaded_at`）；同一读数对不同货物按**各自温度上限**分别判断越温，互不影响；读数不改变任何货物的保管方，保管方只由自有交接改变。
12. **读数来源优先**：同一时刻货物有自有测温时采用**自有值**，该时刻车载读数对该货物标记为“已取代”、不参与越温重建，但**原始读数仍可查**（车次详情与货物时间线均保留）。
13. **车载读数晚到与幂等**：读数允许乱序写入，写入后对全部受影响货物的时间线按时刻整体重建；`reading_id` 同内容重传幂等、异内容冲突；同车次同刻至多一笔读数。
14. **串行一致性**：车载读数写入在单事务内**先按 cargo_id 升序锁定全部受影响货物行、再锁定车次行**，与这些货物的自有事件写入、责任表态严格串行（与装卸登记、同车次读数写入也经车次行锁串行），校验与重建所见必为持锁期间的最新时间线。
15. **同时越温质控查询**：对车次上每票货物按其合并时间线重建越温区间（规则 11–13 同样适用），先与该车次装卸时段相交（装车前 / 卸货后不计，左闭右开），再与左闭右开查询窗相交；未闭合区间先后被卸货时刻与查询终点截断。货物集合变化即分段，相邻同集合片段合并，仅保留达 `min_cargo`（≥2）门槛的正长度片段。查询只读、不加写锁：PostgreSQL 下以 REPEATABLE READ 快照事务保证同次查询视图一致，晚到读数 / 事件须提交后才会被后续查询看到。
16. **测温值更正**：
    - 凭**自有测温事件ID 或车载读数ID（二选一）+ 更正ID + 预期版本 + 新温度**提交；只改温度，测温时刻、货物归属与原编号不变，区间稳定标识（事件ID / `veh:<读数ID>`）不变；**交接不可更正**；
    - 原始测温记录永不修改，每次更正在 `temperature_correction` 表追加一条新版本（首次版本 1，依次递增）；原始查询的 `temperature` 恒为原值，另给 `effective_temperature` 与 `temperature_version`；
    - 货物时间线、越温报告、责任确认（快照、表态有效性、汇总）与车次同时越温查询一律按**有效温度**重建；历史表态保留，当前有效性按重建后责任片段判断（规则 9 不变）；
    - 同刻自有测温优先（规则 12）与装卸时段左闭右开（规则 11）继续适用；车载读数更正对读数时刻仍在该车次车上的**全部货物**同时生效，各自按自身上限判断；
    - 幂等与冲突：原始事件 / 读数按原内容重传仍幂等；更正ID 同内容重传返回原结果、异内容 `correction_id_conflict`；`expected_version` 与当前版本不一致 → `stale_version`；目标不存在（`event_not_found` / `reading_not_found`）或目标为交接（`target_not_correctable`）一律拒绝；
    - 串行：自有更正锁归属货物行；车载更正先按 cargo_id 升序锁读数时刻在车的全部货物行、再锁车次行——与规则 8 / 14 完全同一套加锁顺序；另以同目标版本唯一索引在数据库层兜底并发，落败方不落任何部分结果。

错误响应统一格式：`{"error": {"code": "...", "message": "..."}}`。

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cargo_not_found` / `interval_not_found` / `trip_not_found` / `event_not_found` / `reading_not_found` / `correction_not_found` | 货物、已闭合区间、车次、更正目标事件 / 读数或更正记录不存在 |
| 409 | `cargo_exists` / `trip_exists` / `event_id_conflict` / `same_timestamp_conflict` / `integrity_conflict` / `stance_id_conflict` / `stale_snapshot` / `reading_id_conflict` / `loading_overlap` / `correction_id_conflict` / `stale_version` | 编号或同刻冲突、表态编号冲突、快照过期、装卸跨车次重叠、更正编号异内容冲突、更正预期版本过期 |
| 422 | `before_start` / `giver_mismatch` / `bad_event_shape` / `interval_open` / `party_not_in_segment` / `bad_stance_shape` / `dispute_reason_required` / `invalid_loading_window` / `reserved_event_id` / `invalid_query_window` / `timezone_required` / `invalid_min_cargo` / `missing_parameter` / `bad_correction_shape` / `target_not_correctable` | 业务校验失败（整体拒绝、不落库）；后两项为测温更正请求形状错误 / 交接目标不可更正 |
| 200 | 响应体 `replayed=true` | 事件 / 表态 / 车载读数 / 测温更正同 ID 同内容幂等重传 |

## 六、调用示例

```bash
# curl 脚本（演示完整链路 + 幂等 + 报告）
bash examples/curl_examples.sh

# Python 示例（httpx）
python examples/python_client.py
```

或直接打开 http://localhost:8000/docs 使用 Swagger UI 调用。

## 七、测试

```bash
pip install pytest httpx

pytest -q                                          # 116 个用例（内存库），并发用例自动跳过
DATABASE_URL="sqlite:////tmp/cc.db" \
  pytest tests/test_concurrency.py -q              # 5 个并发串行化用例（事件 / 表态 / 车载读数 / 测温更正，需文件型库）
```

测试覆盖：越温开闭、跨交接责任拆分、同刻测温+交接、未闭合区间、晚到重算、交出方整体拒绝、同刻冲突、幂等重传、并发写入串行化，责任确认的快照校验、确认/异议、汇总状态、表态幂等与改口、晚到事件后表态逐片段失效与历史可查；车次登记、装卸时段左闭右开与跨车次重叠整批拒绝、车载读数仅对在车货物按各自上限越温、同刻自有测温优先（车载原始可查）、交接不被车载读数改变、区间稳定标识 `veh:<读数ID>` 与车载区间表态/失效、车载读数晚到重建、车载写入与受影响货物事件写入的并发串行化；质控同时越温查询的集合变化分段、相邻同集合合并、门槛过滤、查询窗与装卸时段裁剪（左闭右开）、未闭合区间裁至查询终点、装车前带入的开放区间自装车时刻起算、同刻自有优先、晚到读数后查询反映新结果、空结果与参数 / 未知车次错误；以及本轮测温更正的自有 / 车载更正只改温度（原值、时间、归属、编号不变）、有效温度与版本展示、货物时间线 / 越温报告 / 责任确认（快照变化与历史表态有效性）/ 同时越温按有效值重建、同刻自有优先与左闭右开继续适用、车载更正同时影响在车全部货物、交接不可更正、目标不存在 / 版本过期 / 形状非法拒绝不落部分结果、更正与原始记录幂等及异内容冲突、历次更正可查、更正并发串行化。

## 八、目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由与异常映射
│   ├── crud.py            # 自有事件写入事务、幂等、整链校验
│   ├── correction_crud.py # 测温值更正写入事务（货物行锁/车次行锁、版本与幂等）
│   ├── vehicle_crud.py    # 车次 / 装卸时段 / 车载读数写入事务（货物行锁→车次行锁）
│   ├── stance_crud.py     # 责任表态写入事务（与事件 / 读数 / 更正共用货物行锁）
│   ├── effective.py       # 更正后的有效温度 / 版本解析（自有按事件ID、车载按读数ID）
│   ├── timeline.py        # 合并读数时间线（自有事件 + 适用车载读数，同刻自有优先，按有效温度）
│   ├── overlap.py         # 质控同时越温：越温区间 × 装卸时段 × 查询窗求交 + 扫描线分段（纯函数）
│   ├── chain.py           # 保管链重建 / 越温责任拆分（纯函数，含 veh: 区间标识）
│   ├── responsibility.py  # 快照标识、表态有效性、确认汇总（纯逻辑）
│   ├── models.py          # SQLAlchemy 模型（货物 / 事件 / 车次 / 装卸 / 车载读数 / 表态 / 测温更正）
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── database.py        # 引擎、Session（含 SQLite 兼容）
│   └── timeutil.py        # UTC 归一化
├── db/init.sql            # PostgreSQL 初始化（枚举、表、约束、索引）
├── examples/              # curl / Python 调用示例（含车次、车载测温、同时越温查询与测温更正完整链路）
├── tests/                 # pytest 单元 + API + 责任确认 + 车次车载 + 同时越温 + 测温更正 + 并发测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
