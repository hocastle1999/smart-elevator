"""운행 기록 통계 (SQL, 윈도 함수).

SQL을 text()로 직접 쓴다. 분석 쿼리는 SQL 자체가 설계의 핵심이라, 쿼리 빌더로 감추기보다
그대로 보이는 편이 읽고 검증하기 쉽다. 값은 항상 바인드 파라미터(:run_id)로 넘긴다
(문자열로 이어 붙이면 SQL 인젝션 위험).

MySQL 8.0에는 PERCENTILE_CONT 같은 백분위수 함수가 없다. 그래서 ROW_NUMBER()로 순위를 매기고
COUNT(*) OVER ()로 전체 수를 구해 nearest-rank 백분위수(ceil(p x n)번째 값)를 고른다.
파이썬 쪽 `app.simulation.passengers.percentile`과 같은 정의라 결과가 정확히 같아야 한다(테스트).
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Connection, text


def _seconds(ms: Decimal | int | None) -> float | None:
    return None if ms is None else round(float(ms) / 1000, 1)


# ---------------------------------------------------------------------- 실행 목록


@dataclass(frozen=True)
class RunInfo:
    id: int
    source: str
    strategy: str
    pattern: str | None
    rate_per_min: float | None
    seed: int | None
    num_floors: int
    num_elevators: int
    sim_ms: int
    passengers: int


RUNS_SQL = text("""
    SELECT r.id, r.source, r.strategy, r.pattern, r.rate_per_min, r.seed,
           r.num_floors, r.num_elevators, r.sim_ms,
           (SELECT COUNT(*) FROM passenger_trips p WHERE p.run_id = r.id) AS passengers
    FROM simulation_runs r
    WHERE (:source IS NULL OR r.source = :source)
    ORDER BY r.id DESC
    LIMIT :limit
""")


def list_runs(conn: Connection, source: str | None = None, limit: int = 50) -> list[RunInfo]:
    rows = conn.execute(RUNS_SQL, {"source": source, "limit": limit}).mappings()
    return [
        RunInfo(
            **{
                **row,
                "rate_per_min": None if row["rate_per_min"] is None else float(row["rate_per_min"]),
            }
        )
        for row in rows
    ]


def get_run(conn: Connection, run_id: int) -> RunInfo | None:
    row = (
        conn.execute(
            text("""
            SELECT r.id, r.source, r.strategy, r.pattern, r.rate_per_min, r.seed,
                   r.num_floors, r.num_elevators, r.sim_ms,
                   (SELECT COUNT(*) FROM passenger_trips p WHERE p.run_id = r.id) AS passengers
            FROM simulation_runs r WHERE r.id = :run_id
        """),
            {"run_id": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    rate = None if row["rate_per_min"] is None else float(row["rate_per_min"])
    return RunInfo(**{**row, "rate_per_min": rate})


# ---------------------------------------------------------------------- 대기 시간 요약


@dataclass(frozen=True)
class WaitSummary:
    passengers: int
    served: int
    mean_wait_s: float | None
    p95_wait_s: float | None
    max_wait_s: float | None
    mean_journey_s: float | None
    p95_journey_s: float | None


# 같은 SELECT 안에서 정렬 기준이 다른 윈도를 두 개(대기, 이동) 쓴다.
# 아직 못 탄(NULL) 승객은 순위에서 빼야 하므로 각각 따로 걸러서 센다.
WAIT_SUMMARY_SQL = text("""
    WITH ranked AS (
        SELECT wait_ms, journey_ms,
               ROW_NUMBER() OVER (
                   PARTITION BY wait_ms IS NULL ORDER BY wait_ms) AS wait_rank,
               COUNT(wait_ms) OVER () AS waited,
               ROW_NUMBER() OVER (
                   PARTITION BY journey_ms IS NULL ORDER BY journey_ms) AS journey_rank,
               COUNT(journey_ms) OVER () AS served
        FROM passenger_trips
        WHERE run_id = :run_id
    )
    SELECT COUNT(*) AS passengers,
           MAX(served) AS served,
           AVG(wait_ms) AS mean_wait,
           MAX(CASE WHEN wait_ms IS NOT NULL AND wait_rank = CEIL(0.95 * waited)
                    THEN wait_ms END) AS p95_wait,
           MAX(wait_ms) AS max_wait,
           AVG(journey_ms) AS mean_journey,
           MAX(CASE WHEN journey_ms IS NOT NULL AND journey_rank = CEIL(0.95 * served)
                    THEN journey_ms END) AS p95_journey
    FROM ranked
""")


def wait_summary(conn: Connection, run_id: int) -> WaitSummary:
    row = conn.execute(WAIT_SUMMARY_SQL, {"run_id": run_id}).mappings().one()
    return WaitSummary(
        passengers=row["passengers"],
        served=row["served"] or 0,
        mean_wait_s=_seconds(row["mean_wait"]),
        p95_wait_s=_seconds(row["p95_wait"]),
        max_wait_s=_seconds(row["max_wait"]),
        mean_journey_s=_seconds(row["mean_journey"]),
        p95_journey_s=_seconds(row["p95_journey"]),
    )


# ---------------------------------------------------------------------- 전략 비교


@dataclass(frozen=True)
class StrategyComparison:
    pattern: str | None
    rate_per_min: float | None
    strategy: str
    runs: int
    passengers: int
    mean_wait_s: float | None
    p95_wait_s: float | None
    mean_journey_s: float | None


# 실험 실행들을 (패턴, 부하, 전략)으로 묶고, 묶음 안의 모든 승객으로 p95를 구한다
# (시드별 p95의 평균이 아니라 승객을 모은 p95. docs/experiments.md와 같은 방식).
COMPARE_SQL = text("""
    WITH trips AS (
        SELECT r.pattern, r.rate_per_min, r.strategy, r.id AS run_id,
               p.wait_ms, p.journey_ms,
               ROW_NUMBER() OVER w_order AS wait_rank,
               COUNT(*) OVER w_all AS n
        FROM passenger_trips p
        JOIN simulation_runs r ON r.id = p.run_id
        WHERE r.source = 'experiment' AND p.wait_ms IS NOT NULL
        WINDOW w_all AS (PARTITION BY r.pattern, r.rate_per_min, r.strategy),
               w_order AS (w_all ORDER BY p.wait_ms)
    )
    SELECT pattern, rate_per_min, strategy,
           COUNT(DISTINCT run_id) AS runs,
           COUNT(*) AS passengers,
           AVG(wait_ms) AS mean_wait,
           MAX(CASE WHEN wait_rank = CEIL(0.95 * n) THEN wait_ms END) AS p95_wait,
           AVG(journey_ms) AS mean_journey
    FROM trips
    GROUP BY pattern, rate_per_min, strategy
    ORDER BY pattern, rate_per_min, AVG(wait_ms)
""")


def compare_strategies(conn: Connection) -> list[StrategyComparison]:
    return [
        StrategyComparison(
            pattern=row["pattern"],
            rate_per_min=None if row["rate_per_min"] is None else float(row["rate_per_min"]),
            strategy=row["strategy"],
            runs=row["runs"],
            passengers=row["passengers"],
            mean_wait_s=_seconds(row["mean_wait"]),
            p95_wait_s=_seconds(row["p95_wait"]),
            mean_journey_s=_seconds(row["mean_journey"]),
        )
        for row in conn.execute(COMPARE_SQL).mappings()
    ]


# ---------------------------------------------------------------------- 카별 시간 사용


@dataclass(frozen=True)
class StateShare:
    car: int
    state: str
    seconds: float
    percent: float


# 상태 변화 기록은 "이 시각부터 이 상태"만 있다. 그 상태가 얼마나 이어졌는지는 다음 기록의
# 시각에서 빼야 한다: LEAD(time_ms) = 같은 카의 다음 행의 시각. 마지막 행은 다음이 없으므로
# 실행의 마지막 시각(sim_ms)까지로 본다.
# 비율은 집계 결과 위에 다시 윈도를 건다: SUM(SUM(dur)) OVER (PARTITION BY car) = 카별 전체 시간.
CAR_USAGE_SQL = text("""
    WITH spans AS (
        SELECT e.car, e.state,
               COALESCE(LEAD(e.time_ms) OVER (PARTITION BY e.car ORDER BY e.seq), r.sim_ms)
                   - e.time_ms AS duration_ms
        FROM elevator_events e
        JOIN simulation_runs r ON r.id = e.run_id
        WHERE e.run_id = :run_id
    )
    SELECT car, state,
           SUM(duration_ms) AS total_ms,
           100 * SUM(duration_ms) / NULLIF(SUM(SUM(duration_ms)) OVER (PARTITION BY car), 0)
               AS percent
    FROM spans
    GROUP BY car, state
    ORDER BY car, total_ms DESC
""")


def car_usage(conn: Connection, run_id: int) -> list[StateShare]:
    return [
        StateShare(
            car=row["car"],
            state=row["state"],
            seconds=_seconds(row["total_ms"]) or 0.0,
            percent=round(float(row["percent"] or 0), 1),
        )
        for row in conn.execute(CAR_USAGE_SQL, {"run_id": run_id}).mappings()
    ]


# ---------------------------------------------------------------------- 시간대별 추이


@dataclass(frozen=True)
class TimelinePoint:
    start_s: int  # 구간 시작 (시뮬레이션 시각, 초)
    passengers: int
    mean_wait_s: float | None
    moving_avg_wait_s: float | None  # 최근 3구간 이동 평균 (승객 수 가중)


# 이동 평균은 "구간 평균의 평균"이 아니라 "최근 3구간의 대기 시간 합 / 승객 수 합"이다.
# 승객이 1명인 구간과 100명인 구간을 같은 무게로 섞지 않기 위해서다.
TIMELINE_SQL = text("""
    WITH buckets AS (
        SELECT FLOOR(arrival_ms / :bucket_ms) AS bucket,
               COUNT(*) AS n,
               SUM(wait_ms) AS wait_sum
        FROM passenger_trips
        WHERE run_id = :run_id AND wait_ms IS NOT NULL
        GROUP BY bucket
    )
    SELECT bucket, n,
           wait_sum / n AS mean_wait,
           SUM(wait_sum) OVER w / SUM(n) OVER w AS moving_avg
    FROM buckets
    WINDOW w AS (ORDER BY bucket ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)
    ORDER BY bucket
""")


def wait_timeline(conn: Connection, run_id: int, bucket_ms: int) -> list[TimelinePoint]:
    rows = conn.execute(TIMELINE_SQL, {"run_id": run_id, "bucket_ms": bucket_ms}).mappings()
    return [
        TimelinePoint(
            start_s=int(row["bucket"]) * bucket_ms // 1000,
            passengers=row["n"],
            mean_wait_s=_seconds(row["mean_wait"]),
            moving_avg_wait_s=_seconds(row["moving_avg"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------- 층별 순위


@dataclass(frozen=True)
class FloorRank:
    floor: int
    departures: int
    mean_wait_s: float | None
    rank: int  # 출발 승객 수 순위 (동점이면 같은 순위, 다음 순위는 건너뜀)


FLOORS_SQL = text("""
    SELECT origin AS floor,
           COUNT(*) AS departures,
           AVG(wait_ms) AS mean_wait,
           RANK() OVER (ORDER BY COUNT(*) DESC) AS rnk
    FROM passenger_trips
    WHERE run_id = :run_id
    GROUP BY origin
    ORDER BY rnk, floor
""")


def busiest_floors(conn: Connection, run_id: int) -> list[FloorRank]:
    return [
        FloorRank(
            floor=row["floor"],
            departures=row["departures"],
            mean_wait_s=_seconds(row["mean_wait"]),
            rank=row["rnk"],
        )
        for row in conn.execute(FLOORS_SQL, {"run_id": run_id}).mappings()
    ]


# ---------------------------------------------------------------------- 가동률과 장애 (6단계)


@dataclass(frozen=True)
class CarAvailability:
    car: int
    outages: int  # 운행 중지 횟수 (고장 격리, 점검)
    down_s: float
    availability_pct: float  # 실행 시간 중 운행 중이던 비율
    mean_outage_s: float | None  # 운행 중지 한 번의 평균 길이 (평균 복구 시간, MTTR)


# 운행 중지 구간 = OUT_OF_SERVICE 기록부터 같은 카의 다음 RETURN 기록까지.
# 두 종류만 남기고 LEAD로 "다음 기록의 시각"을 붙이면, OUT_OF_SERVICE 행의 다음 행이 곧 복귀다.
# 아직 복귀하지 않았으면(다음 행 없음) 실행의 마지막 시각까지로 본다.
AVAILABILITY_SQL = text("""
    WITH marks AS (
        SELECT s.car, s.kind, s.time_ms,
               LEAD(s.time_ms) OVER (PARTITION BY s.car ORDER BY s.seq) AS next_ms
        FROM service_log s
        WHERE s.run_id = :run_id AND s.kind IN ('OUT_OF_SERVICE', 'RETURN')
    )
    SELECT m.car, COUNT(*) AS outages,
           SUM(COALESCE(m.next_ms, r.sim_ms) - m.time_ms) AS down_ms
    FROM marks m
    JOIN simulation_runs r ON r.id = :run_id
    WHERE m.kind = 'OUT_OF_SERVICE'
    GROUP BY m.car
""")


def car_availability(conn: Connection, run: RunInfo) -> list[CarAvailability]:
    rows = {r["car"]: r for r in conn.execute(AVAILABILITY_SQL, {"run_id": run.id}).mappings()}
    result = []
    for car in range(run.num_elevators):  # 한 번도 멈추지 않은 카도 100%로 보여 준다
        row = rows.get(car)
        outages = row["outages"] if row else 0
        down_ms = int(row["down_ms"]) if row else 0
        pct = 100.0 if run.sim_ms == 0 else 100 * (1 - down_ms / run.sim_ms)
        result.append(
            CarAvailability(
                car=car,
                outages=outages,
                down_s=round(down_ms / 1000, 1),
                availability_pct=round(pct, 2),
                mean_outage_s=round(down_ms / outages / 1000, 1) if outages else None,
            )
        )
    return result


@dataclass(frozen=True)
class Incident:
    car: int
    fault: str
    fault_s: float  # 고장이 난 시각 (시뮬레이션, 초)
    first_rule: str | None  # 처음 울린 감지 규칙
    detected_after_s: float | None  # 고장부터 감지까지 (None이면 감지하지 못함)
    isolated_after_s: float | None  # 고장부터 운행 중지까지
    repaired_after_s: float | None  # 고장부터 수리까지


# 고장 하나에 뒤따르는 감지, 운행 중지, 수리 기록을 한 묶음(사건)으로 모은다.
# "섬 나누기(gaps and islands)": 카별로 시간순 누적 합 SUM(kind = 'FAULT')을 구하면 고장이 날
# 때마다 1씩 오르므로, 같은 값을 가진 행들이 한 사건이 된다. (0은 첫 고장 전의 기록)
INCIDENTS_SQL = text("""
    WITH numbered AS (
        SELECT car, seq, time_ms, kind, detail,
               SUM(kind = 'FAULT') OVER (PARTITION BY car ORDER BY seq) AS incident
        FROM service_log
        WHERE run_id = :run_id
    )
    SELECT car, incident,
           MAX(CASE WHEN kind = 'FAULT' THEN detail END) AS fault,
           MIN(CASE WHEN kind = 'FAULT' THEN time_ms END) AS fault_ms,
           MIN(CASE WHEN kind = 'ALERT' THEN time_ms END) AS alert_ms,
           SUBSTRING_INDEX(
               GROUP_CONCAT(CASE WHEN kind = 'ALERT' THEN detail END ORDER BY seq), ',', 1
           ) AS first_rule,
           MIN(CASE WHEN kind = 'OUT_OF_SERVICE' THEN time_ms END) AS isolated_ms,
           MIN(CASE WHEN kind = 'REPAIR' THEN time_ms END) AS repaired_ms
    FROM numbered
    WHERE incident > 0
    GROUP BY car, incident
    ORDER BY fault_ms, car
""")


def incidents(conn: Connection, run_id: int) -> list[Incident]:
    def after(ms: int | None, start: int) -> float | None:
        return None if ms is None else round((ms - start) / 1000, 1)

    result = []
    for row in conn.execute(INCIDENTS_SQL, {"run_id": run_id}).mappings():
        start = row["fault_ms"]
        result.append(
            Incident(
                car=row["car"],
                fault=row["fault"],
                fault_s=round(start / 1000, 1),
                first_rule=row["first_rule"],
                detected_after_s=after(row["alert_ms"], start),
                isolated_after_s=after(row["isolated_ms"], start),
                repaired_after_s=after(row["repaired_ms"], start),
            )
        )
    return result
