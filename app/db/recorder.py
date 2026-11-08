"""시뮬레이션 기록을 DB에 저장한다.

- `save_run`: 끝난 실행 하나를 통째로 저장 (비교 실험)
- `LiveRecorder`: 실시간 서버의 상태 변화를 모았다가 주기적으로 묶어서 저장 (write-behind)
"""

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, Table, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.db.tables import elevator_events, passenger_trips, service_log, simulation_runs
from app.simulation import Passenger, Simulation, StateChange
from app.simulation.monitor import ServiceEvent

logger = logging.getLogger(__name__)

# 한 번의 INSERT 문에 넣을 최대 행 수. 너무 크면 MySQL의 max_allowed_packet(기본 64MB)을
# 넘을 수 있고, 너무 작으면 왕복이 많아진다.
CHUNK_ROWS = 5000

Row = dict[str, Any]


def _utc_now() -> datetime:
    # DB에는 시간대 없는 UTC 시각으로 저장한다 (연결의 time_zone도 UTC로 고정).
    return datetime.now(UTC).replace(tzinfo=None)


def _insert_chunks(conn: Connection, table: Table, rows: Sequence[Row]) -> None:
    """여러 행을 CHUNK_ROWS씩 나눠 INSERT한다.

    행 목록을 넘기면 SQLAlchemy가 executemany로 실행하고, PyMySQL은 이를 여러 행짜리
    INSERT ... VALUES (...), (...) 한 문장으로 묶어 보낸다.
    한 행씩 INSERT하면 행마다 DB 왕복이 생긴다.
    """
    for start in range(0, len(rows), CHUNK_ROWS):
        conn.execute(table.insert(), rows[start : start + CHUNK_ROWS])


def event_rows(run_id: int, car: int, changes: Iterable[StateChange], first_seq: int) -> list[Row]:
    return [
        {
            "run_id": run_id,
            "car": car,
            "seq": first_seq + i,
            "time_ms": c.time_ms,
            "state": c.state.value,
            "floor": c.floor,
            "direction": c.direction.value,
        }
        for i, c in enumerate(changes)
    ]


def trip_rows(run_id: int, passengers: Iterable[Passenger]) -> list[Row]:
    # wait_ms, journey_ms는 생성 열이라 넣지 않는다 (DB가 계산).
    return [
        {
            "run_id": run_id,
            "passenger_id": p.id,
            "origin": p.origin,
            "destination": p.destination,
            "arrival_ms": p.arrival_ms,
            "board_ms": p.board_ms,
            "alight_ms": p.alight_ms,
            "car": p.car,
        }
        for p in passengers
    ]


def service_rows(run_id: int, events: Iterable[ServiceEvent], first_seq: int) -> list[Row]:
    return [
        {
            "run_id": run_id,
            "seq": first_seq + i,
            "time_ms": e.time_ms,
            "car": e.car,
            "kind": e.kind.value,
            "detail": e.detail,
            "severity": None if e.severity is None else e.severity.value,
            "message": e.message[:200],
        }
        for i, e in enumerate(events)
    ]


def _insert_run(conn: Connection, sim: Simulation, source: str, **meta: Any) -> int:
    result = conn.execute(
        simulation_runs.insert().values(
            source=source,
            strategy=sim.strategy.name,
            num_floors=sim.building.num_floors,
            num_elevators=len(sim.elevators),
            started_at=_utc_now(),
            sim_ms=sim.now_ms,
            **meta,
        )
    )
    return int(result.inserted_primary_key[0])


def save_run(
    engine: Engine,
    sim: Simulation,
    *,
    pattern: str | None = None,
    rate_per_min: float | None = None,
    seed: int | None = None,
) -> int:
    """끝난 실험 실행 하나를 저장하고 run id를 돌려준다.

    실행 정보, 상태 변화, 승객 기록을 한 트랜잭션으로 넣는다. 중간에 실패하면 모두 취소되어
    "실행은 있는데 기록이 반만 있는" 상태가 남지 않는다.
    """
    with engine.begin() as conn:
        run_id = _insert_run(
            conn, sim, "experiment", pattern=pattern, rate_per_min=rate_per_min, seed=seed
        )
        events: list[Row] = []
        for car, elevator in enumerate(sim.elevators):
            events += event_rows(run_id, car, elevator.history, first_seq=0)
        _insert_chunks(conn, elevator_events, events)
        _insert_chunks(conn, passenger_trips, trip_rows(run_id, sim.passengers))
        _insert_chunks(conn, service_log, service_rows(run_id, sim.service_log, first_seq=0))
    return run_id


class LiveRecorder:
    """실시간 서버의 상태 변화를 버퍼에 모았다가 묶어서 저장한다 (write-behind).

    - tick마다 DB에 쓰면 이벤트 루프가 DB 응답을 기다리느라 멈춘다. 그래서 루프에서는 메모리
      버퍼에 모으기만 하고(`collect`), 저장(`write`)은 다른 스레드에서 주기적으로 한다.
    - DB가 잠시 죽어도 시뮬레이션은 계속 돈다. 그동안 버퍼에 쌓되, 상한(max_buffer)을 넘으면
      오래된 기록부터 버리고 개수를 센다(`dropped`). 메모리를 지키는 것이 기록 완전성보다 우선이다.
    """

    def __init__(self, engine: Engine, max_buffer: int = 100_000) -> None:
        self.engine = engine
        self.max_buffer = max_buffer
        self.run_id: int | None = None
        self.written = 0
        self.dropped = 0
        self.last_error: str | None = None
        self._buffer: list[Row] = []
        self._service_buffer: list[Row] = []  # 서비스 기록은 적어서 상한 없이 모은다
        self._trip_buffer: list[Row] = []  # 자동 승객 데모에서 다 내린 승객
        self._next_seq: list[int] = []
        self._next_service_seq = 0

    def start_run(self, sim: Simulation) -> int:
        """실시간 실행을 하나 만든다. DB I/O라서 이벤트 루프 밖(스레드)에서 부른다."""
        with self.engine.begin() as conn:
            self.run_id = _insert_run(conn, sim, "live")
        self._next_seq = [0] * len(sim.elevators)
        return self.run_id

    def collect(
        self,
        sim: Simulation,
        service: Sequence[ServiceEvent] = (),
        finished: Sequence[Passenger] = (),
    ) -> None:
        """새 상태 변화를 버퍼로 옮기고 카의 기록을 비운다. 이벤트 루프에서 부른다 (I/O 없음).

        service: 이번 tick에 생긴 서비스 기록 (고장, 감지, 운행 중지 등). 호출한 쪽이 꺼내서 넘긴다.
        """
        if self.run_id is None:
            return
        self._trip_buffer += trip_rows(self.run_id, finished)
        if len(self._trip_buffer) > self.max_buffer:
            self.dropped += len(self._trip_buffer) - self.max_buffer
            del self._trip_buffer[: len(self._trip_buffer) - self.max_buffer]
        self._service_buffer += service_rows(self.run_id, service, self._next_service_seq)
        self._next_service_seq += len(service)
        for car, elevator in enumerate(sim.elevators):
            changes = elevator.drain_history()
            self._buffer += event_rows(self.run_id, car, changes, self._next_seq[car])
            self._next_seq[car] += len(changes)
        overflow = len(self._buffer) - self.max_buffer
        if overflow > 0:
            del self._buffer[:overflow]
            self.dropped += overflow

    def take(self) -> tuple[list[Row], list[Row]]:
        """버퍼를 통째로 꺼낸다 (이벤트 루프에서). 꺼낸 뒤 쌓이는 기록은 새 버퍼로 간다.

        (상태 변화, 서비스 기록)을 돌려준다.
        """
        rows, self._buffer = self._buffer, []
        service, self._service_buffer = self._service_buffer, []
        return rows, service

    def take_trips(self) -> list[Row]:
        """다 내린 승객 기록을 꺼낸다 (자동 승객 데모)."""
        trips, self._trip_buffer = self._trip_buffer, []
        return trips

    def give_back(
        self, rows: list[Row], service: list[Row] | None = None, trips: list[Row] | None = None
    ) -> None:
        self._trip_buffer = (trips or []) + self._trip_buffer
        """저장에 실패한 기록을 버퍼 앞에 되돌린다 (다음 저장 때 다시 시도)."""
        self._service_buffer = (service or []) + self._service_buffer
        self._buffer = rows + self._buffer
        overflow = len(self._buffer) - self.max_buffer
        if overflow > 0:
            del self._buffer[:overflow]
            self.dropped += overflow

    def write(
        self,
        rows: list[Row],
        sim_ms: int,
        service: list[Row] | None = None,
        trips: list[Row] | None = None,
    ) -> None:
        """기록을 저장하고 실행의 마지막 시각을 갱신한다. DB I/O라서 스레드에서 부른다."""
        # 재시도해도 안전하게(멱등): 커밋은 됐는데 응답 직전에 연결이 끊기면 실패로 보고 같은 행을
        # 다시 보낸다. 기본키 (run_id, car, seq)가 자연키라 이미 있는 행이면 아무것도 바꾸지 않는다.
        # INSERT IGNORE는 중복 말고 다른 오류(CHECK 위반 등)까지 경고로 바꿔 숨기므로 쓰지 않는다.
        stmt = mysql_insert(elevator_events)
        stmt = stmt.on_duplicate_key_update(seq=stmt.inserted.seq)
        service_stmt = mysql_insert(service_log)
        service_stmt = service_stmt.on_duplicate_key_update(seq=service_stmt.inserted.seq)
        with self.engine.begin() as conn:
            for start in range(0, len(rows), CHUNK_ROWS):
                conn.execute(stmt, rows[start : start + CHUNK_ROWS])
            if service:
                conn.execute(service_stmt, service)
            if trips:
                trip_stmt = mysql_insert(passenger_trips)
                conn.execute(trip_stmt.on_duplicate_key_update(car=trip_stmt.inserted.car), trips)
            conn.execute(
                update(simulation_runs)
                .where(simulation_runs.c.id == self.run_id)
                .values(sim_ms=sim_ms)
            )
        self.written += len(rows)
