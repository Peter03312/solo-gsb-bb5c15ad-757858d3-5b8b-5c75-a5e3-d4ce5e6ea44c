"""并发写入测试：同一货物的多笔并发交接必须串行化，保管链不得损坏。

本用例需要“文件型 SQLite（每线程独立连接）”的引擎，运行方式：

    DATABASE_URL="sqlite:////tmp/coldchain_conc.db" pytest tests/test_concurrency.py

（默认的内存 StaticPool 配置下本用例自动跳过。）
"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

pytestmark = pytest.mark.skipif(
    engine.pool.__class__.__name__ == "StaticPool",
    reason="StaticPool（内存库）不支持多线程并发；"
    "请用文件型 sqlite 单独运行："
    "DATABASE_URL='sqlite:////tmp/x.db' pytest tests/test_concurrency.py",
)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def test_concurrent_handovers_preserve_chain(client):
    client.post(
        "/cargo",
        json={
            "cargo_id": "CC",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )

    def submit(i):
        # 所有线程都提交“同一时刻、A->Pi”的交接：货物行锁使其串行执行，
        # 只有第一笔成功；其余要么交出方失配（保管方已变更），
        # 要么触发同一时刻交接唯一约束
        return client.post(
            "/events",
            json={
                "event_id": f"hc{i}",
                "cargo_id": "CC",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "giver": "A",
                "receiver": f"P{i}",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert all(c in (409, 422) for c in codes if c != 201)
    report = client.get("/cargo/CC/report").json()
    # 保管链只前进一次，当前保管方是唯一成功那笔的接收方
    assert report["current_holder"].startswith("P")


def test_concurrent_stance_submits_serialize_with_snapshot(client):
    """表态并发提交：同责任方相同内容只有第一笔 201，其余按幂等返回 200。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CS",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/events", json={
        "event_id": "m1", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:10:00+00:00", "temperature": "8"})
    client.post("/events", json={
        "event_id": "m2", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:20:00+00:00", "temperature": "4"})
    snap = client.get("/cargo/CS/responsibilities").json()["intervals"][0]["snapshot_id"]

    def submit(i):
        return client.post(
            "/cargo/CS/responsibilities/m1/stances",
            json={
                "snapshot_id": snap,
                "responsible_party": "A",
                "stance": "confirm",
                # 所有线程同一编号同一内容：行锁串行后表现为 1 次写入 + 其余幂等
                "stance_id": "same-stance",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert codes.count(200) == 7
    hist = client.get("/cargo/CS/responsibilities/m1/stances").json()
    assert len(hist["history"]) == 1


def test_concurrent_vehicle_reading_and_own_measurement_serialize(client):
    """车载读数写入与受影响货物的自有测温写入共用货物行锁：

    同刻（11:00）并发提交车载越温读数与货物自有不超限测温，二者都应成功，
    且无论提交先后，重建结果确定：同刻自有测温优先，货物不越温；
    车次读数查询对该货物标记 adopted=false（原始读数仍可查）。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CV",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    assert client.post("/trips", json={"trip_id": "TV", "vehicle_no": "V"}).status_code == 201
    assert client.post(
        "/trips/TV/manifest",
        json={"items": [{
            "cargo_id": "CV",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T12:00:00+00:00",
        }]},
    ).status_code == 201

    def submit_own(i):
        # 只有一笔使用合法编号，其余预期冲突/失败，仅用于制造并发压力
        return client.post(
            "/events",
            json={
                "event_id": "m-own" if i == 0 else f"m-own-x{i}",
                "cargo_id": "CV",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "temperature": "3",
            },
        ).status_code

    def submit_vehicle(i):
        return client.post(
            "/vehicle-readings",
            json={
                "reading_id": f"vr{i}",
                "trip_id": "TV",
                "read_at": "2026-01-01T11:00:00+00:00",
                "temperature": "9",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(submit_own, range(4)))
        list(pool.map(submit_vehicle, range(4)))

    # 自有测温只有一笔（m-own），车载读数同车次同刻只有一笔
    own = client.get("/cargo/CV/events").json()
    assert [e["event_id"] for e in own] == ["m-own"]
    trip = client.get("/trips/TV").json()
    assert len(trip["readings"]) == 1
    app_cv = next(a for a in trip["readings"][0]["applications"] if a["cargo_id"] == "CV")
    assert app_cv["adopted"] is False  # 同刻自有测温优先
    assert app_cv["on_board"] is True
    # 采用自有值 3℃ -> 无越温区间
    assert client.get("/cargo/CV/report").json()["excursions"] == []
    # 车载原始读数仍可查（合并时间线中 adopted=false）
    tl = client.get("/cargo/CV/timeline").json()["points"]
    veh = [p for p in tl if p["source"] == "vehicle"]
    assert len(veh) == 1 and veh[0]["adopted"] is False


def test_concurrent_corrections_serialize_with_expected_version(client):
    """同一测温事件的并发更正按货物行锁串行：

    所有线程都以 expected_version=0 提交，只有第一笔成功（版本升到 1）；
    其余因预期版本过期（stale_version）被拒绝，不产生第二条 v1 记录。
    """
    client.post(
        "/cargo",
        json={
            "cargo_id": "CC2",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/events", json={
        "event_id": "m1", "cargo_id": "CC2",
        "occurred_at": "2026-01-01T11:00:00+00:00", "temperature": "8"})

    def submit(i):
        return client.post(
            "/temperature-corrections",
            json={
                "correction_id": f"corr{i}",
                "event_id": "m1",
                "expected_version": 0,
                "new_temperature": str(3 + i),
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert all(c == 409 for c in codes if c != 201)
    hist = client.get("/events/m1/corrections").json()
    assert hist["current_version"] == 1
    assert len(hist["corrections"]) == 1


def test_concurrent_correction_and_event_write_serialize(client):
    """车载读数更正与受影响货物的事件写入共用货物行锁串行，重建结果确定。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CV2",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    assert client.post("/trips", json={"trip_id": "TV2", "vehicle_no": "V"}).status_code == 201
    assert client.post(
        "/trips/TV2/manifest",
        json={"items": [{
            "cargo_id": "CV2",
            "loaded_at": "2026-01-01T10:00:00+00:00",
            "unloaded_at": "2026-01-01T12:00:00+00:00",
        }]},
    ).status_code == 201
    assert client.post("/vehicle-readings", json={
        "reading_id": "vr1", "trip_id": "TV2",
        "read_at": "2026-01-01T11:00:00+00:00", "temperature": "2",
    }).status_code == 201

    def submit_correction(i):
        return client.post("/temperature-corrections", json={
            "correction_id": f"vc{i}", "reading_id": "vr1",
            "expected_version": 0, "new_temperature": "9",
        }).status_code

    def submit_event(i):
        # 同刻自有测温 3℃（不越限），若生效则车载读数对 CV2 被取代
        return client.post("/events", json={
            "event_id": f"me{i}", "cargo_id": "CV2",
            "occurred_at": "2026-01-01T11:00:00+00:00", "temperature": "3",
        }).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        corr_codes = list(pool.map(submit_correction, range(4)))
        ev_codes = list(pool.map(submit_event, range(4)))

    # 更正只有一笔 v1 成功；自有同刻测温只有一笔
    assert corr_codes.count(201) == 1
    assert all(c == 409 for c in corr_codes if c != 201)
    assert ev_codes.count(201) == 1
    assert all(c in (409, 422) for c in ev_codes if c != 201)
    hist = client.get("/vehicle-readings/vr1/corrections").json()
    assert hist["current_version"] == 1
    # 同刻自有 3℃ 优先：无论提交先后，货物不越温
    assert client.get("/cargo/CV2/report").json()["excursions"] == []
