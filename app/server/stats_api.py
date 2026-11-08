"""운행 통계 API (MySQL).

이 파일의 핸들러는 일부러 `def`(동기)다. PyMySQL은 블로킹 드라이버라, `async def` 안에서
쿼리하면 응답을 기다리는 동안 이벤트 루프 전체(시뮬레이션 루프, WebSocket 방송)가 멈춘다.
`def` 핸들러는 FastAPI가 스레드 풀에서 실행하므로 루프를 막지 않는다.
대신 이 핸들러들은 공유 시뮬레이션(app.state.runner)을 절대 만지지 않는다 (다른 스레드라서).
"""

from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import Connection, Engine

from app.db import stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _engine(request: Request) -> Engine:
    engine: Engine | None = request.app.state.db_engine
    if engine is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="DB가 설정되지 않았습니다 (DB_ENABLED)."
        )
    return engine


def _require_run(conn: Connection, run_id: int) -> stats.RunInfo:
    run = stats.get_run(conn, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{run_id}번 실행이 없습니다.")
    return run


@dataclass(frozen=True)
class RunDetail:
    run: stats.RunInfo
    waits: stats.WaitSummary


@router.get("/runs", response_model=list[stats.RunInfo])
def list_runs(
    request: Request,
    source: str | None = Query(None, pattern="^(experiment|live)$"),
    limit: int = Query(50, ge=1, le=500),
) -> list[stats.RunInfo]:
    with _engine(request).connect() as conn:
        return stats.list_runs(conn, source, limit)


@router.get("/runs/{run_id}", response_model=RunDetail)
def run_detail(run_id: int, request: Request) -> RunDetail:
    """실행 정보와 대기·이동 시간 요약 (평균, p95, 최대)."""
    with _engine(request).connect() as conn:
        return RunDetail(run=_require_run(conn, run_id), waits=stats.wait_summary(conn, run_id))


@router.get("/runs/{run_id}/cars", response_model=list[stats.StateShare])
def car_usage(run_id: int, request: Request) -> list[stats.StateShare]:
    """카별로 각 상태(이동, 문 열림, 대기 등)에 쓴 시간과 비율."""
    with _engine(request).connect() as conn:
        _require_run(conn, run_id)
        return stats.car_usage(conn, run_id)


@router.get("/runs/{run_id}/timeline", response_model=list[stats.TimelinePoint])
def wait_timeline(
    run_id: int, request: Request, bucket_minutes: int = Query(5, ge=1, le=60)
) -> list[stats.TimelinePoint]:
    """도착 시각 구간별 평균 대기 시간과 최근 3구간 이동 평균."""
    with _engine(request).connect() as conn:
        _require_run(conn, run_id)
        return stats.wait_timeline(conn, run_id, bucket_minutes * 60_000)


@router.get("/runs/{run_id}/floors", response_model=list[stats.FloorRank])
def busiest_floors(run_id: int, request: Request) -> list[stats.FloorRank]:
    """출발 승객이 많은 층 순위와 층별 평균 대기 시간."""
    with _engine(request).connect() as conn:
        _require_run(conn, run_id)
        return stats.busiest_floors(conn, run_id)


@router.get("/compare", response_model=list[stats.StrategyComparison])
def compare(request: Request) -> list[stats.StrategyComparison]:
    """저장된 실험 실행을 (패턴, 부하, 전략)별로 묶어 비교한다."""
    with _engine(request).connect() as conn:
        return stats.compare_strategies(conn)
