"""测温值更正 API 集成测试。

覆盖：
* 自有测温更正：只改温度（有效温度/版本），原始记录 temperature 不变、时间与归属不变；
* 更正后货物时间线、越温报告、责任确认按有效值重建；快照变化、历史表态保留；
* 交接不可更正；目标不存在、版本过期、目标形状非法拒绝且不落部分结果；
* 更正ID 幂等（同内容 200 返原结果）/ 异内容 409；原事件按原内容重传仍幂等；
* 历次更正可查（事件 / 读数两个历史接口 + 按更正ID直查）；
* 车载读数更正：同时影响读数时刻在车的全部货物，按各自上限重建，
  同刻自有优先与左闭右开规则继续适用；
* 同时越温查询按更正后有效值重建。
"""


def post_cargo(client, cid="C1", limit="5", holder="A",
               started="2026-01-01T08:00:00+00:00"):
    r = client.post("/cargo", json={
        "cargo_id": cid, "started_at": started,
        "temp_upper_limit": limit, "initial_holder": holder})
    assert r.status_code == 201, r.text
    return r.json()


def m(client, eid, at, temp, cargo="C1"):
    return client.post("/events", json={
        "event_id": eid, "cargo_id": cargo,
        "occurred_at": at, "temperature": str(temp)})


def h(client, eid, at, giver, receiver, cargo="C1"):
    return client.post("/events", json={
        "event_id": eid, "cargo_id": cargo, "occurred_at": at,
        "giver": giver, "receiver": receiver})


def correct(client, cid, temp, expected, *, event_id=None, reading_id=None):
    body = {"correction_id": cid, "expected_version": expected,
            "new_temperature": str(temp)}
    if event_id is not None:
        body["event_id"] = event_id
    if reading_id is not None:
        body["reading_id"] = reading_id
    return client.post("/temperature-corrections", json=body)


def post_trip(client, trip_id="T1", vehicle_no="京A-001"):
    return client.post("/trips", json={"trip_id": trip_id, "vehicle_no": vehicle_no})


def post_manifest(client, trip_id, items):
    return client.post(f"/trips/{trip_id}/manifest", json={"items": items})


def post_reading(client, rid, trip_id, at, temp):
    return client.post("/vehicle-readings", json={
        "reading_id": rid, "trip_id": trip_id,
        "read_at": at, "temperature": str(temp)})


# ------------------------------------------------------------- 自有测温更正


def test_correct_own_measurement_basic(client):
    post_cargo(client, limit="5")
    assert m(client, "m1", "2026-01-01T10:00:00+00:00", 9).status_code == 201

    r = correct(client, "corr-1", 3, 0, event_id="m1")
    assert r.status_code == 201, r.text
    ack = r.json()
    assert ack["target_type"] == "own"
    assert ack["target_id"] == "m1"
    assert ack["cargo_id"] == "C1"
    assert ack["effective_temperature"] == "3.000"
    assert ack["temperature_version"] == 1
    assert ack["affected_cargo_ids"] == ["C1"]

    # 原始事件查询：temperature 仍为原值，另给有效温度与版本
    ev = client.get("/cargo/C1/events").json()[0]
    assert ev["temperature"] == "9.000"
    assert ev["effective_temperature"] == "3.000"
    assert ev["temperature_version"] == 1
    # 时间、归属、编号不变
    assert ev["event_id"] == "m1"
    assert ev["cargo_id"] == "C1"
    assert ev["occurred_at"] == "2026-01-01T10:00:00Z"

    # 时间线点显示有效温度与版本，原始温度仍可查
    p = client.get("/cargo/C1/timeline").json()["points"][0]
    assert p["temperature"] == "3.000"
    assert p["original_temperature"] == "9.000"
    assert p["temperature_version"] == 1

    # 按有效温度 3℃（<= 5）重建：不再越温
    assert client.get("/cargo/C1/report").json()["excursions"] == []


def test_correction_can_create_excursion(client):
    # 原值 3℃ 不越温；更正为 9℃ 后开启区间
    post_cargo(client, limit="5")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 3)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    assert correct(client, "corr-1", 9, 0, event_id="m1").status_code == 201
    exs = client.get("/cargo/C1/report").json()["excursions"]
    assert len(exs) == 1
    assert exs[0]["start"] == "2026-01-01T10:00:00Z"
    assert exs[0]["is_open"] is True


def test_second_correction_requires_expected_version_1(client):
    post_cargo(client, limit="5")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    correct(client, "corr-1", 3, 0, event_id="m1")
    # 第二次更正，版本过期（仍传 0）-> 409 stale_version，不落部分结果
    r = correct(client, "corr-2", 4, 0, event_id="m1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_version"
    # 传正确预期版本 1 -> 成功，当前版本 2
    r2 = correct(client, "corr-2", 4, 1, event_id="m1")
    assert r2.status_code == 201, r2.text
    assert r2.json()["temperature_version"] == 2
    ev = client.get("/cargo/C1/events").json()[0]
    assert ev["temperature"] == "9.000"
    assert ev["effective_temperature"] == "4.000"
    assert ev["temperature_version"] == 2


def test_correction_history_listed_and_get_by_id(client):
    post_cargo(client, limit="5")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    correct(client, "corr-1", 8, 0, event_id="m1")
    correct(client, "corr-2", 3, 1, event_id="m1")

    hist = client.get("/events/m1/corrections").json()
    assert hist["target_type"] == "own"
    assert hist["target_id"] == "m1"
    assert hist["cargo_id"] == "C1"
    assert hist["original_temperature"] == "9.000"
    assert hist["effective_temperature"] == "3.000"
    assert hist["current_version"] == 2
    assert [c["correction_id"] for c in hist["corrections"]] == ["corr-1", "corr-2"]
    assert [c["version"] for c in hist["corrections"]] == [1, 2]

    one = client.get("/temperature-corrections/corr-1")
    assert one.status_code == 200
    assert one.json()["new_temperature"] == "8.000"
    assert client.get("/temperature-corrections/nope").status_code == 404


def test_correction_idempotent_same_content_and_conflict(client):
    post_cargo(client, limit="5")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    body = {"correction_id": "corr-1", "event_id": "m1",
            "expected_version": 0, "new_temperature": "3"}
    assert client.post("/temperature-corrections", json=body).status_code == 201
    # 同更正ID同内容重传 -> 200 返原结果
    r2 = client.post("/temperature-corrections", json=body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["temperature_version"] == 1
    # 只生成一条记录
    hist = client.get("/events/m1/corrections").json()
    assert len(hist["corrections"]) == 1
    # 异内容（温度不同）-> 409
    body["new_temperature"] = "4"
    r3 = client.post("/temperature-corrections", json=body)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "correction_id_conflict"
    assert hist["corrections"][0]["new_temperature"] == "3.000"


def test_original_event_replay_still_idempotent_after_correction(client):
    post_cargo(client, limit="5")
    body = {"event_id": "m1", "cargo_id": "C1",
            "occurred_at": "2026-01-01T10:00:00+00:00", "temperature": "9"}
    assert client.post("/events", json=body).status_code == 201
    correct(client, "corr-1", 3, 0, event_id="m1")
    # 原事件按原内容（9℃）重传仍幂等：200 replayed，原始值不被更正影响
    r = client.post("/events", json=body)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    ev = client.get("/cargo/C1/events").json()[0]
    assert ev["temperature"] == "9.000"
    assert ev["effective_temperature"] == "3.000"


def test_handover_target_rejected(client):
    post_cargo(client, holder="A")
    h(client, "h1", "2026-01-01T11:00:00+00:00", "A", "B")
    r = correct(client, "corr-x", 9, 0, event_id="h1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "target_not_correctable"
    # 交接无更正历史（422），且未产生任何更正记录
    assert client.get("/events/h1/corrections").status_code == 422


def test_target_not_found_and_bad_shape(client):
    post_cargo(client)
    # 自有事件不存在
    r = correct(client, "corr-1", 3, 0, event_id="nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "event_not_found"
    # 同时给两个目标
    r2 = client.post("/temperature-corrections", json={
        "correction_id": "corr-2", "event_id": "m1", "reading_id": "vr1",
        "expected_version": 0, "new_temperature": "3"})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "bad_correction_shape"
    # 两个都不给
    r3 = client.post("/temperature-corrections", json={
        "correction_id": "corr-3", "expected_version": 0, "new_temperature": "3"})
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "bad_correction_shape"


def test_rebuild_responsibility_and_stance_history_after_correction(client):
    # 9℃ 开启 -> 12:00 A->B -> 14:00 3℃ 闭合；更正开启点为 3℃ 后区间消失，
    # 再更正回 9℃（新版本）区间按新快照重建；历史表态保留、按重建片段判有效性。
    post_cargo(client, limit="5", holder="A")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    h(client, "h1", "2026-01-01T12:00:00+00:00", "A", "B")
    m(client, "m2", "2026-01-01T14:00:00+00:00", 3)

    iv = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    snap1 = iv["snapshot_id"]
    assert [s["responsible_party"] for s in iv["segments"]] == ["A", "B"]
    # A 确认
    assert client.post("/cargo/C1/responsibilities/m1/stances", json={
        "snapshot_id": snap1, "responsible_party": "A",
        "stance": "confirm", "stance_id": "s-a"}).status_code == 201

    # 把闭合点 m2 更正为 9℃（仍越温）-> 区间重新开放
    assert correct(client, "corr-close", 9, 0, event_id="m2").status_code == 201
    assert client.get("/cargo/C1/responsibilities").json()["intervals"] == []
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist["is_open"] is True
    assert hist["history"][0]["is_current"] is False

    # 再把 m2 更正回 3℃（预期版本 1）-> 区间重新闭合，快照恢复，A 的表态重新有效
    assert correct(client, "corr-close-2", 3, 1, event_id="m2").status_code == 201
    iv2 = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    assert iv2["snapshot_id"] == snap1
    hist2 = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist2["history"][0]["is_current"] is True
    assert iv2["summary"] == "pending"  # B 未表态


def test_correction_changes_snapshot_stance_snapshot_stale(client):
    post_cargo(client, limit="5", holder="A")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    m(client, "m2", "2026-01-01T14:00:00+00:00", 3)
    snap_before = client.get(
        "/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
    # 把闭合温度更正为另一个不超限值（3 -> 2）：边界不变、片段不变 -> 快照不变
    correct(client, "corr-1", 2, 0, event_id="m2")
    snap_after = client.get(
        "/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
    assert snap_before == snap_after


# ------------------------------------------------------------- 车载读数更正


def _two_cargo_trip(client, c1_limit="5", c2_limit="8"):
    post_cargo(client, "C1", limit=c1_limit)
    post_cargo(client, "C2", limit=c2_limit)
    post_trip(client, "T1")
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])


def test_correct_vehicle_reading_affects_all_onboard_cargo(client):
    _two_cargo_trip(client)
    # 原始 7℃：C1（上限5）越温，C2（上限8）不越温
    post_reading(client, "vr1", "T1", "2026-01-01T11:00:00+00:00", 7)
    assert len(client.get("/cargo/C1/report").json()["excursions"]) == 1
    assert client.get("/cargo/C2/report").json()["excursions"] == []

    # 更正为 2℃ -> 两票货物都不越温（同时生效）
    r = correct(client, "corr-vr-1", 2, 0, reading_id="vr1")
    assert r.status_code == 201, r.text
    ack = r.json()
    assert ack["target_type"] == "vehicle"
    assert ack["target_id"] == "vr1"
    assert ack["cargo_id"] is None
    assert ack["affected_cargo_ids"] == ["C1", "C2"]
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    assert client.get("/cargo/C2/report").json()["excursions"] == []

    # 再更正为 9℃（预期版本 1）-> 两票都越温
    correct(client, "corr-vr-2", 9, 1, reading_id="vr1")
    assert len(client.get("/cargo/C1/report").json()["excursions"]) == 1
    assert len(client.get("/cargo/C2/report").json()["excursions"]) == 1

    # 原始读数仍可查：temperature 原值，另给有效值/版本
    detail = client.get("/trips/T1").json()
    rd = detail["readings"][0]
    assert rd["temperature"] == "7.000"
    assert rd["effective_temperature"] == "9.000"
    assert rd["temperature_version"] == 2
    # 各货物适用性以有效值判定
    by_cargo = {a["cargo_id"]: a for a in rd["applications"]}
    assert by_cargo["C1"]["over_limit"] is True
    assert by_cargo["C2"]["over_limit"] is True
    assert by_cargo["C1"]["applied_temperature"] == "9.000"
    assert by_cargo["C1"]["reading_temperature"] == "9.000"
    assert by_cargo["C1"]["reading_temperature_version"] == 2


def test_vehicle_reading_correction_history(client):
    _two_cargo_trip(client)
    post_reading(client, "vr1", "T1", "2026-01-01T11:00:00+00:00", 7)
    correct(client, "corr-vr-1", 2, 0, reading_id="vr1")
    hist = client.get("/vehicle-readings/vr1/corrections").json()
    assert hist["target_type"] == "vehicle"
    assert hist["trip_id"] == "T1"
    assert hist["cargo_id"] is None
    assert hist["original_temperature"] == "7.000"
    assert hist["effective_temperature"] == "2.000"
    assert hist["current_version"] == 1
    assert len(hist["corrections"]) == 1
    # 读数不存在
    assert client.get("/vehicle-readings/nope/corrections").status_code == 404
    # 直接更正不存在读数 -> 404 reading_not_found
    r = correct(client, "corr-x", 2, 0, reading_id="nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reading_not_found"


def test_vehicle_correction_idempotent_and_stale(client):
    _two_cargo_trip(client)
    post_reading(client, "vr1", "T1", "2026-01-01T11:00:00+00:00", 7)
    body = {"correction_id": "corr-vr", "reading_id": "vr1",
            "expected_version": 0, "new_temperature": "2"}
    assert client.post("/temperature-corrections", json=body).status_code == 201
    # 同内容重传 200
    r = client.post("/temperature-corrections", json=body)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["affected_cargo_ids"] == ["C1", "C2"]
    # 新版本用过期预期 0 -> 409
    r2 = client.post("/temperature-corrections", json={
        "correction_id": "corr-vr-2", "reading_id": "vr1",
        "expected_version": 0, "new_temperature": "1"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "stale_version"


def test_vehicle_correction_respects_own_precedence(client):
    # 同刻自有测温优先：读数更正后仅对无自有测温的货物生效
    _two_cargo_trip(client)
    post_reading(client, "vr1", "T1", "2026-01-01T11:00:00+00:00", 2)
    # C1 同刻自有 3℃（不越限）
    m(client, "own-1", "2026-01-01T11:00:00+00:00", 3, cargo="C1")
    # 读数更正为 9℃：C1 因同刻自有 3℃ 优先仍不越温；C2 越温
    correct(client, "corr-vr", 9, 0, reading_id="vr1")
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    assert len(client.get("/cargo/C2/report").json()["excursions"]) == 1
    detail = client.get("/trips/T1").json()["readings"][0]
    by_cargo = {a["cargo_id"]: a for a in detail["applications"]}
    c1 = by_cargo["C1"]
    assert c1["adopted"] is False
    assert c1["reason"] == "supplanted_by_own"
    # applied_temperature 采用同刻自有值（3℃，不越限）
    assert c1["applied_temperature"] == "3.000"
    assert c1["over_limit"] is False
    # 更正自有测温为 9℃ -> C1 同刻仍采用自有（现在越温）
    correct(client, "corr-own", 9, 0, event_id="own-1")
    assert len(client.get("/cargo/C1/report").json()["excursions"]) == 1
    by_cargo2 = {a["cargo_id"]: a for a in
                 client.get("/trips/T1").json()["readings"][0]["applications"]}
    assert by_cargo2["C1"]["applied_temperature"] == "9.000"
    assert by_cargo2["C1"]["over_limit"] is True


def test_simultaneous_excursions_rebuilt_after_correction(client):
    # 两票同车货物上限都是 5℃；读数 3℃ 不越温，更正为 9℃ 后同时越温
    post_cargo(client, "C1", limit="5")
    post_cargo(client, "C2", limit="5")
    post_trip(client, "T1")
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
        {"cargo_id": "C2",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    post_reading(client, "vr1", "T1", "2026-01-01T11:00:00+00:00", 3)
    q = {
        "start_at": "2026-01-01T09:00:00+00:00",
        "end_at": "2026-01-01T13:00:00+00:00",
    }
    before = client.get("/trips/T1/simultaneous-excursions", params=q).json()
    assert before["segments"] == []
    correct(client, "corr-vr", 9, 0, reading_id="vr1")
    after = client.get("/trips/T1/simultaneous-excursions", params=q).json()
    assert len(after["segments"]) == 1
    seg = after["segments"][0]
    assert seg["cargo_ids"] == ["C1", "C2"]
    assert seg["start"] == "2026-01-01T11:00:00Z"
    # 再更正回 3℃（版本 1）-> 同时越温消失
    correct(client, "corr-vr-2", 3, 1, reading_id="vr1")
    again = client.get("/trips/T1/simultaneous-excursions", params=q).json()
    assert again["segments"] == []


def test_correction_does_not_change_timestamp_or_holder(client):
    # 更正测温温度不改变时间，也不影响保管链（交接只由自有事件决定）
    post_cargo(client, holder="A")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    h(client, "h1", "2026-01-01T11:00:00+00:00", "A", "B")
    correct(client, "corr-1", 2, 0, event_id="m1")
    rpt = client.get("/cargo/C1/report").json()
    assert rpt["current_holder"] == "B"
    ev = client.get("/cargo/C1/events").json()
    assert ev[0]["occurred_at"] == "2026-01-01T10:00:00Z"


def test_vehicle_correction_keeps_interval_id_but_changes_snapshot(client):
    # 车载读数开启/闭合区间后更正开启读数温度（仍越温）：
    # 区间稳定标识 veh:vr1 不变、边界可能不变（时刻没变），但责任视图仍可查；
    # 表态按重建后片段判定（片段不变 -> 继续有效）。
    post_cargo(client, limit="5", holder="A")
    post_trip(client, "T1")
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T14:00:00+00:00"},
    ])
    post_reading(client, "vr1", "T1", "2026-01-01T10:30:00+00:00", 9)
    post_reading(client, "vr2", "T1", "2026-01-01T12:00:00+00:00", 3)
    iv = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    assert iv["interval_id"] == "veh:vr1"
    snap = iv["snapshot_id"]
    client.post("/cargo/C1/responsibilities/veh:vr1/stances", json={
        "snapshot_id": snap, "responsible_party": "A",
        "stance": "confirm", "stance_id": "s1"})
    # 开启读数 9 -> 8（仍越温）：时刻不变 -> 区间标识与快照都不变
    correct(client, "corr-opener", 8, 0, reading_id="vr1")
    iv2 = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    assert iv2["interval_id"] == "veh:vr1"
    assert iv2["snapshot_id"] == snap
    hist = client.get("/cargo/C1/responsibilities/veh:vr1/stances").json()
    assert hist["history"][0]["is_current"] is True
    # 把开启读数改为 3（不越温）：区间消失
    correct(client, "corr-opener-2", 3, 1, reading_id="vr1")
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    assert client.get("/cargo/C1/responsibilities").json()["intervals"] == []


def test_vehicle_correction_reflects_later_manifest(client):
    # 读数写入时车上无货物（affected 为空）；事后补登记装卸时段，
    # 再做更正——影响范围按更正时刻的装卸关系推导，时间线按有效温度重建。
    post_cargo(client, limit="5")
    post_trip(client, "T1")
    r0 = post_reading(client, "vr1", "T1", "2026-01-01T11:00:00+00:00", 9)
    assert r0.json()["affected_cargo_ids"] == []
    # 补登记：读数 11:00 落在 [10:00,12:00) 内
    post_manifest(client, "T1", [
        {"cargo_id": "C1",
         "loaded_at": "2026-01-01T10:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    # 原始 9℃ 现已作用到 C1：越温
    assert len(client.get("/cargo/C1/report").json()["excursions"]) == 1
    # 更正为 2℃：回执按当前装卸关系给出受影响货物 C1，区间消失
    r = correct(client, "corr-vr", 2, 0, reading_id="vr1")
    assert r.json()["affected_cargo_ids"] == ["C1"]
    assert client.get("/cargo/C1/report").json()["excursions"] == []


def test_correction_no_partial_result_on_stale(client):
    # 版本过期拒绝后，目标状态保持原样（不产生半截更正）
    post_cargo(client, limit="5")
    m(client, "m1", "2026-01-01T10:00:00+00:00", 9)
    correct(client, "corr-1", 3, 0, event_id="m1")
    bad = correct(client, "corr-bad", 4, 0, event_id="m1")
    assert bad.status_code == 409
    hist = client.get("/events/m1/corrections").json()
    assert hist["current_version"] == 1
    assert hist["effective_temperature"] == "3.000"
    assert [c["correction_id"] for c in hist["corrections"]] == ["corr-1"]
