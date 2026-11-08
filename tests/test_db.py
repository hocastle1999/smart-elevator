"""운행 기록 저장과 SQL 통계 테스트 (MySQL 테스트 DB 필요).

작은 데이터를 직접 넣어 손으로 검산한 값과 비교하고, 실제 시뮬레이션 결과로는 SQL과 파이썬의
계산이 같은지 확인한다.
"""

import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.db import stats
from app.db.recorder import LiveRecorder, save_run
from app.db.tables import elevator_events, passenger_trips, service_log, simulation_runs
from app.main import create_app
from app.server.settings import ServerSettings
from app.simulation import STRATEGIES, Building, Direction, PassengerArrival, Simulation
from app.simulation.passengers import percentile
from app.simulation.traffic import PATTERNS, poisson_passengers

# ---------------------------------------------------------------- 도우미


def poisson_sim(strategy: str = "eta", seed: int = 0, minutes: int = 10) -> Simulation:
    building = Building(num_floors=12)
    sim = Simulation(building, num_elevators=3, strategy=STRATEGIES[strategy]())
    sim.schedule_all(poisson_passengers(building, PATTERNS["up-peak"], 20, minutes * 60_000, seed))
    sim.run_until_idle()
    return sim


def insert_run(engine: Engine, sim_ms: int = 0, **meta) -> int:
    values = {
        "source": "experiment",
        "strategy": "eta",
        "num_floors": 10,
        "num_elevators": 1,
        "started_at": datetime.now(UTC).replace(tzinfo=None),
        "sim_ms": sim_ms,
        **meta,
    }
    with engine.begin() as conn:
        return int(conn.execute(simulation_runs.insert().values(**values)).inserted_primary_key[0])


def insert_trips(engine: Engine, run_id: int, trips: list[tuple[int, int, int | None]]) -> None:
    """trips: (출발층, 도착 시각, 탄 시각 또는 None)"""
    rows = [
        {
            "run_id": run_id,
            "passenger_id": i,
            "origin": origin,
            "destination": 10 if origin != 10 else 1,
            "arrival_ms": arrival,
            "board_ms": board,
            "alight_ms": None if board is None else board + 10_000,
            "car": 0,
        }
        for i, (origin, arrival, board) in enumerate(trips)
    ]
    with engine.begin() as conn:
        conn.execute(passenger_trips.insert(), rows)


def count(engine: Engine, table, run_id: int) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(table).where(table.c.run_id == run_id)
        ).scalar_one()


# ---------------------------------------------------------------- 저장


def test_save_run_stores_everything_in_one_go(db):
    sim = poisson_sim()
    run_id = save_run(db, sim, pattern="up-peak", rate_per_min=20, seed=0)
    assert count(db, passenger_trips, run_id) == len(sim.passengers)
    assert count(db, elevator_events, run_id) == sum(len(c.history) for c in sim.elevators)
    with db.connect() as conn:
        run = stats.get_run(conn, run_id)
    assert run is not None
    assert (run.strategy, run.pattern, run.rate_per_min, run.seed) == ("eta", "up-peak", 20.0, 0)
    assert run.sim_ms == sim.now_ms


def test_generated_columns_are_computed_by_db(db):
    run_id = insert_run(db)
    insert_trips(db, run_id, [(1, 1000, 4000), (2, 500, None)])
    with db.connect() as conn:
        rows = conn.execute(
            select(passenger_trips.c.wait_ms, passenger_trips.c.journey_ms)
            .where(passenger_trips.c.run_id == run_id)
            .order_by(passenger_trips.c.passenger_id)
        ).all()
    assert rows == [(3000, 13_000), (None, None)]


def test_deleting_run_cascades_to_records(db):
    sim = poisson_sim(minutes=2)
    run_id = save_run(db, sim)
    with db.begin() as conn:
        conn.execute(simulation_runs.delete().where(simulation_runs.c.id == run_id))
    assert count(db, elevator_events, run_id) == 0
    assert count(db, passenger_trips, run_id) == 0


# ---------------------------------------------------------------- 대기 시간 요약 (p95)


def test_wait_summary_hand_checked(db):
    run_id = insert_run(db)
    # 탄 승객 20명(대기 1~20초) + 아직 못 탄 승객 2명
    trips = [(1, 0, s * 1000) for s in range(20, 0, -1)] + [(2, 0, None), (3, 0, None)]
    insert_trips(db, run_id, trips)
    with db.connect() as conn:
        s = stats.wait_summary(conn, run_id)
    assert s.passengers == 22
    assert s.served == 20
    assert s.mean_wait_s == 10.5
    assert s.p95_wait_s == 19.0  # ceil(0.95 x 20) = 19번째 값
    assert s.max_wait_s == 20.0


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
def test_sql_summary_matches_python(db, strategy):
    sim = poisson_sim(strategy)
    run_id = save_run(db, sim)
    py = sim.stats()
    with db.connect() as conn:
        s = stats.wait_summary(conn, run_id)
    waits = [p.wait_ms for p in sim.passengers]
    journeys = [p.journey_ms for p in sim.passengers]
    assert s.served == py.served
    assert s.mean_wait_s == pytest.approx(py.mean_wait_s, abs=0.051)
    assert s.p95_wait_s == round(percentile(waits, 95) / 1000, 1)
    assert s.p95_journey_s == round(percentile(journeys, 95) / 1000, 1)
    assert s.max_wait_s == round(max(waits) / 1000, 1)


def test_wait_summary_of_empty_run(db):
    run_id = insert_run(db)
    with db.connect() as conn:
        s = stats.wait_summary(conn, run_id)
    assert (s.passengers, s.served, s.p95_wait_s) == (0, 0, None)


# ---------------------------------------------------------------- 카별 시간 사용 (LEAD)


def test_car_usage_hand_checked(db):
    run_id = insert_run(db, sim_ms=10_000)
    # 같은 0ms에 IDLE -> MOVING_UP (seq로 순서를 정한다), 2초 이동, 5초 문 열림, 마지막 3초 대기
    events = [
        (0, "IDLE"),
        (0, "MOVING_UP"),
        (2000, "DOORS_OPENING"),
        (7000, "IDLE"),
    ]
    rows = [
        {
            "run_id": run_id,
            "car": 0,
            "seq": i,
            "time_ms": t,
            "state": state,
            "floor": 1,
            "direction": "NONE",
        }
        for i, (t, state) in enumerate(events)
    ]
    with db.begin() as conn:
        conn.execute(elevator_events.insert(), rows)
    with db.connect() as conn:
        usage = {(u.state, u.seconds, u.percent) for u in stats.car_usage(conn, run_id)}
    assert usage == {("DOORS_OPENING", 5.0, 50.0), ("IDLE", 3.0, 30.0), ("MOVING_UP", 2.0, 20.0)}


def test_car_usage_covers_whole_run_for_every_car(db):
    sim = poisson_sim()
    run_id = save_run(db, sim)
    with db.connect() as conn:
        usage = stats.car_usage(conn, run_id)
    for car in range(len(sim.elevators)):
        shares = [u for u in usage if u.car == car]
        assert sum(u.seconds for u in shares) == pytest.approx(sim.now_ms / 1000, abs=0.5)
        assert sum(u.percent for u in shares) == pytest.approx(100, abs=0.5)


# ---------------------------------------------------------------- 시간대별 추이, 층 순위


def test_timeline_moving_average_is_weighted_by_passengers(db):
    run_id = insert_run(db)
    # 0~5분 구간: 1명이 10초 대기, 5~10분 구간: 3명이 2초씩 대기
    trips = [(1, 0, 10_000)] + [(1, 300_000 + i, 300_000 + i + 2000) for i in range(3)]
    insert_trips(db, run_id, trips)
    with db.connect() as conn:
        points = stats.wait_timeline(conn, run_id, 300_000)
    assert [(p.start_s, p.passengers, p.mean_wait_s) for p in points] == [
        (0, 1, 10.0),
        (300, 3, 2.0),
    ]
    # 가중 이동 평균: (10 + 2 + 2 + 2) / 4 = 4초 (구간 평균의 단순 평균이면 6초)
    assert points[1].moving_avg_wait_s == 4.0


def test_busiest_floors_rank_with_ties(db):
    run_id = insert_run(db)
    insert_trips(db, run_id, [(1, 0, 1000)] * 3 + [(2, 0, 1000), (3, 0, 1000)])
    with db.connect() as conn:
        ranks = [(f.floor, f.departures, f.rank) for f in stats.busiest_floors(conn, run_id)]
    assert ranks == [(1, 3, 1), (2, 1, 2), (3, 1, 2)]


# ---------------------------------------------------------------- 전략 비교


def test_compare_pools_passengers_across_seeds(db):
    sims = {}
    for strategy in ("eta", "nearest-car"):
        for seed in (0, 1):
            sim = poisson_sim(strategy, seed, minutes=5)
            save_run(db, sim, pattern="up-peak", rate_per_min=20, seed=seed)
            sims.setdefault(strategy, []).append(sim)
    with db.connect() as conn:
        rows = {r.strategy: r for r in stats.compare_strategies(conn)}
    assert set(rows) == {"eta", "nearest-car"}
    for strategy, runs in sims.items():
        waits = [p.wait_ms for sim in runs for p in sim.passengers]
        row = rows[strategy]
        assert (row.runs, row.passengers) == (2, len(waits))
        assert row.p95_wait_s == round(percentile(waits, 95) / 1000, 1)
        assert row.mean_wait_s == pytest.approx(sum(waits) / len(waits) / 1000, abs=0.051)


# ---------------------------------------------------------------- 실시간 기록 (write-behind)


def test_live_recorder_writes_in_batches_and_continues_seq(db):
    sim = Simulation(Building(num_floors=10), num_elevators=2)
    recorder = LiveRecorder(db)
    run_id = recorder.start_run(sim)
    sim.press_hall_button(5, Direction.UP)
    sim.step(5000)
    recorder.collect(sim)
    assert all(not car.history for car in sim.elevators)  # 메모리에서 비운다
    first, service = recorder.take()
    recorder.write(first, sim.now_ms, service)
    sim.step(30_000)
    recorder.collect(sim)
    rows, service = recorder.take()
    recorder.write(rows, sim.now_ms, service)
    with db.connect() as conn:
        seqs = (
            conn.execute(
                select(elevator_events.c.seq)
                .where(elevator_events.c.run_id == run_id, elevator_events.c.car == 0)
                .order_by(elevator_events.c.seq)
            )
            .scalars()
            .all()
        )
        run = stats.get_run(conn, run_id)
    assert seqs == list(range(len(seqs)))  # 두 번에 나눠 써도 순번이 이어진다
    assert run is not None and run.source == "live" and run.sim_ms == sim.now_ms


def test_live_recorder_retry_is_idempotent(db):
    sim = Simulation(Building(num_floors=10))
    recorder = LiveRecorder(db)
    run_id = recorder.start_run(sim)
    sim.press_car_button(0, 4)
    sim.step(20_000)
    recorder.collect(sim)
    rows, _ = recorder.take()
    recorder.write(rows, sim.now_ms)
    recorder.write(rows, sim.now_ms)  # 응답을 못 받아 같은 행을 다시 보낸 경우
    assert count(db, elevator_events, run_id) == len(rows)


def test_live_recorder_drops_oldest_when_buffer_full():
    recorder = LiveRecorder(engine=None, max_buffer=3)  # type: ignore[arg-type]
    recorder.give_back([{"seq": i} for i in range(5)])
    assert [r["seq"] for r in recorder.take()[0]] == [2, 3, 4]
    assert recorder.dropped == 2


# ---------------------------------------------------------------- API


@pytest.fixture
def api(db, db_settings) -> TestClient:
    return TestClient(create_app(ServerSettings(run_loop=False), db_settings))


def test_stats_api_endpoints(api, db):
    sim = poisson_sim()
    run_id = save_run(db, sim, pattern="up-peak", rate_per_min=20, seed=0)
    runs = api.get("/api/stats/runs", params={"source": "experiment"}).json()
    assert [r["id"] for r in runs] == [run_id]
    detail = api.get(f"/api/stats/runs/{run_id}").json()
    assert detail["waits"]["served"] == len(sim.passengers)
    assert api.get(f"/api/stats/runs/{run_id}/cars").status_code == 200
    timeline = api.get(f"/api/stats/runs/{run_id}/timeline", params={"bucket_minutes": 2}).json()
    assert sum(p["passengers"] for p in timeline) == len(sim.passengers)
    assert api.get(f"/api/stats/runs/{run_id}/floors").json()[0]["floor"] == 1  # 출근: 로비
    assert api.get("/api/stats/compare").json()[0]["strategy"] == "eta"


def test_stats_api_unknown_run_is_404(api):
    for path in ("", "/cars", "/timeline", "/floors"):
        assert api.get(f"/api/stats/runs/999999{path}").status_code == 404


def test_stats_api_without_db_is_503():
    client = TestClient(create_app(ServerSettings(run_loop=False)))  # DB_ENABLED=false
    assert client.get("/api/stats/runs").status_code == 503


def test_live_server_records_events(db, db_settings):
    settings = ServerSettings(tick_ms=10, sim_ms_per_tick=1000, record_flush_ms=50)
    with TestClient(create_app(settings, db_settings)) as client:
        client.post("/api/hall-calls", json={"floor": 4, "direction": "UP"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            recording = client.get("/health").json()["recording"]
            if recording["written_events"] > 0:
                break
            time.sleep(0.05)
        assert recording["last_error"] is None
        run_id = recording["run_id"]
        assert recording["written_events"] > 0
        live = client.get("/api/stats/runs", params={"source": "live"}).json()
        assert [r["id"] for r in live] == [run_id]
    assert count(db, elevator_events, run_id) > 0


# ---------------------------------------------------------------- 고장과 점검 (6단계)


def test_service_report_matches_simulation(db):
    from app.simulation.monitor import Monitor, ServiceKind
    from app.simulation.types import Fault

    sim = Simulation(Building(num_floors=10), num_elevators=2)
    Monitor(sim)
    sim.press_car_button(0, 8)
    sim.step(1000)
    sim.inject_fault(0, Fault.STALL)
    sim.step(20_000)
    sim.repair(0)
    sim.return_to_service(0)
    sim.run_until_idle(dt_ms=1000)
    log = {e.kind: e.time_ms for e in sim.service_log}
    run_id = save_run(db, sim)

    with db.connect() as conn:
        run = stats.get_run(conn, run_id)
        assert run is not None
        availability = {a.car: a for a in stats.car_availability(conn, run)}
        (incident,) = stats.incidents(conn, run_id)

    down_ms = log[ServiceKind.RETURN] - log[ServiceKind.OUT_OF_SERVICE]
    assert availability[0].outages == 1
    assert availability[0].down_s == round(down_ms / 1000, 1)
    assert availability[0].availability_pct == round(100 * (1 - down_ms / sim.now_ms), 2)
    assert availability[1].availability_pct == 100.0  # 멈춘 적 없는 카도 나온다
    assert incident.fault == "STALL" and incident.first_rule == "STALLED"
    fault_ms = log[ServiceKind.FAULT]
    assert incident.detected_after_s == round((log[ServiceKind.ALERT] - fault_ms) / 1000, 1)
    assert incident.repaired_after_s == round((log[ServiceKind.REPAIR] - fault_ms) / 1000, 1)
    # 운행 중지 상태도 상태 변화 기록에 남는다 (CHECK 제약에 OUT_OF_SERVICE 추가)
    with db.connect() as conn:
        states = (
            conn.execute(select(elevator_events.c.state).where(elevator_events.c.run_id == run_id))
            .scalars()
            .all()
        )
    assert "OUT_OF_SERVICE" in states


def test_incidents_split_by_fault_and_keep_undetected(db):
    run_id = insert_run(db, sim_ms=100_000)
    rows = [
        (0, 1000, 0, "FAULT", "DOOR_SENSOR"),
        (1, 3000, 0, "ALERT", "DOOR_REOPEN_LOOP"),
        (2, 3000, 0, "OUT_OF_SERVICE", "DOOR_REOPEN_LOOP"),
        (3, 9000, 0, "REPAIR", ""),
        (4, 10_000, 0, "RETURN", ""),
        (5, 20_000, 0, "FAULT", "SLOW_MOTOR"),  # 두 번째 사건: 감지되지 않음
    ]
    with db.begin() as conn:
        conn.execute(
            service_log.insert(),
            [
                {"run_id": run_id, "seq": seq, "time_ms": t, "car": car, "kind": k, "detail": d}
                for seq, t, car, k, d in rows
            ],
        )
    with db.connect() as conn:
        found = [
            (i.fault, i.detected_after_s, i.repaired_after_s) for i in stats.incidents(conn, run_id)
        ]
        run = stats.get_run(conn, run_id)
        assert run is not None
        (car0,) = stats.car_availability(conn, run)
    assert found == [("DOOR_SENSOR", 2.0, 8.0), ("SLOW_MOTOR", None, None)]
    assert (car0.outages, car0.down_s, car0.availability_pct) == (1, 7.0, 93.0)


def test_live_recorder_saves_service_log(db):
    from app.simulation.types import Fault

    sim = Simulation(Building(num_floors=10))
    recorder = LiveRecorder(db)
    run_id = recorder.start_run(sim)
    sim.inject_fault(0, Fault.SLOW_MOTOR)
    sim.repair(0)
    recorder.collect(sim, sim.drain_service_log())
    rows, service = recorder.take()
    recorder.write(rows, sim.now_ms, service)
    recorder.write(rows, sim.now_ms, service)  # 재시도해도 중복되지 않는다
    assert count(db, service_log, run_id) == 2


def test_live_recorder_saves_finished_passengers(db):
    sim = Simulation(Building(num_floors=10))
    recorder = LiveRecorder(db)
    run_id = recorder.start_run(sim)
    sim.schedule_all([PassengerArrival(0, 1, 5), PassengerArrival(0, 3, 1)])
    sim.run_until_idle(dt_ms=1000)
    recorder.collect(sim, finished=sim.drain_finished_passengers())
    rows, service = recorder.take()
    trips = recorder.take_trips()
    recorder.write(rows, sim.now_ms, service, trips)
    recorder.write(rows, sim.now_ms, service, trips)  # 재시도해도 중복 없음
    assert count(db, passenger_trips, run_id) == 2
    with db.connect() as conn:
        assert stats.wait_summary(conn, run_id).served == 2
    assert sim.passengers == []
