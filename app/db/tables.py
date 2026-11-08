"""테이블 정의 (SQLAlchemy Core).

ORM 대신 Core를 쓰는 이유: 운행 기록은 한 번 쓰고 고치지 않는 로그다. 대량 INSERT와 분석용
SQL(윈도 함수)이 중심이라, 객체 상태를 추적하는 ORM(Unit of Work)의 이점이 없고 비용만 든다.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
)

from app.simulation import Direction, ElevatorState
from app.simulation.monitor import ServiceKind, Severity

# 제약과 인덱스 이름 규칙. 이름이 정해져 있어야 마이그레이션에서 지우거나 바꿀 수 있다.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _in(column: str, values: list[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


# ENUM 대신 VARCHAR + CHECK: MySQL ENUM은 값을 추가할 때 ALTER TABLE이 필요하고, 순서에 따라
# 정렬되는 등 동작이 헷갈린다. CHECK는 MySQL 8.0.16부터 실제로 검사한다.
STATES = [s.value for s in ElevatorState]
DIRECTIONS = [d.value for d in Direction]

simulation_runs = Table(
    "simulation_runs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # experiment: 비교 실험 스크립트가 저장한 실행, live: 실시간 서버의 공유 시뮬레이션
    Column("source", String(16), nullable=False),
    Column("strategy", String(32), nullable=False),
    Column("pattern", String(16)),
    Column("rate_per_min", Numeric(6, 2)),
    Column("seed", Integer),
    Column("num_floors", SmallInteger, nullable=False),
    Column("num_elevators", SmallInteger, nullable=False),
    Column("started_at", DateTime, nullable=False),  # UTC, 실제 시각
    Column("sim_ms", BigInteger, nullable=False, server_default="0"),  # 마지막 시뮬레이션 시각
    CheckConstraint(_in("source", ["experiment", "live"]), name="source"),
    Index(None, "source", "strategy", "pattern"),
)

elevator_events = Table(
    "elevator_events",
    metadata,
    # 기본키 (run_id, car, seq): InnoDB는 기본키 순서로 행을 저장한다(클러스터형 인덱스).
    # 그래서 "한 실행의 한 카를 시간순으로" 읽는 통계 쿼리(LEAD 등)가 디스크를 차례로 읽는다.
    # 자동 증가 id를 쓰면 여러 카의 행이 섞여 저장되고, 이 순서로 읽으려면 인덱스가
    # 하나 더 필요하다.
    Column(
        "run_id",
        BigInteger,
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("car", SmallInteger, primary_key=True),
    # 카별 상태 변화 순번. 같은 ms에 상태가 두 번 바뀔 수 있어(IDLE -> MOVING)
    # time_ms로는 순서를 못 정한다.
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("time_ms", BigInteger, nullable=False),
    Column("state", String(16), nullable=False),
    Column("floor", SmallInteger, nullable=False),
    Column("direction", String(4), nullable=False),
    CheckConstraint(_in("state", STATES), name="state"),
    CheckConstraint(_in("direction", DIRECTIONS), name="direction"),
)

passenger_trips = Table(
    "passenger_trips",
    metadata,
    Column(
        "run_id",
        BigInteger,
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("passenger_id", Integer, primary_key=True, autoincrement=False),
    Column("origin", SmallInteger, nullable=False),
    Column("destination", SmallInteger, nullable=False),
    Column("arrival_ms", BigInteger, nullable=False),
    Column("board_ms", BigInteger),  # 아직 못 탔으면 NULL
    Column("alight_ms", BigInteger),
    Column("car", SmallInteger),
    # 생성 열(STORED): 저장할 때 DB가 계산해 넣는다. 앱이 계산해 넣으면 원본 시각과 어긋날 수 있다.
    # STORED라 인덱스를 걸 수 있다(VIRTUAL은 읽을 때마다 계산).
    Column("wait_ms", BigInteger, Computed("board_ms - arrival_ms", persisted=True)),
    Column("journey_ms", BigInteger, Computed("alight_ms - arrival_ms", persisted=True)),
    CheckConstraint("origin <> destination", name="different_floors"),
    # (run_id, wait_ms) 인덱스는 두지 않는다. 백분위수를 구하는 윈도 함수는 인덱스 순서로 읽어도
    # MySQL 8.0이 항상 다시 정렬해서 이득이 없고, 쓰기 비용만 는다 (docs/troubleshooting.md).
    # 실행별 조회는 기본키 (run_id, passenger_id)의 앞부분으로 찾는다.
)

service_log = Table(
    "service_log",
    metadata,
    # 고장 주입, 이상 감지, 운행 중지, 점검 요청, 수리, 복귀를 시간순으로 한 줄씩 (6단계).
    # 실행 안에서의 순번(seq)이 기본키라 실시간 저장을 다시 시도해도 중복되지 않는다.
    Column(
        "run_id",
        BigInteger,
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("time_ms", BigInteger, nullable=False),
    Column("car", SmallInteger, nullable=False),
    Column("kind", String(24), nullable=False),
    Column("detail", String(32), nullable=False, server_default=""),
    Column("severity", String(8)),
    Column("message", String(200), nullable=False, server_default=""),
    CheckConstraint(_in("kind", [k.value for k in ServiceKind]), name="kind"),
    CheckConstraint(_in("severity", [s.value for s in Severity]), name="severity"),
)
