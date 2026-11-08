"""API 요청과 응답, WebSocket으로 보내는 상태의 형식 (Pydantic)."""

from pydantic import BaseModel, ConfigDict

from app.simulation import Direction, Elevator, ElevatorState, HallCall, Simulation
from app.simulation.monitor import ServiceEvent, ServiceKind, Severity
from app.simulation.types import Fault


class HallCallView(BaseModel):
    floor: int
    direction: Direction


class ElevatorView(BaseModel):
    id: int
    floor: int
    position: float
    state: ElevatorState
    direction: Direction
    car_calls: list[int]
    hall_calls: list[HallCallView]
    in_service: bool
    maintenance_requested: bool
    riding: int  # 타고 있는 승객 수 (자동 승객)
    # 주입된 고장 (데모용으로 보여 준다. 시스템은 감지 전까지 이 값을 모른다)
    fault: Fault | None


class ServiceEventView(BaseModel):
    time_ms: int
    car: int
    kind: ServiceKind
    detail: str
    severity: Severity | None
    message: str


class StateSnapshot(BaseModel):
    """한 시점의 전체 상태. WebSocket 방송과 GET /api/state가 같은 형식을 쓴다."""

    time_ms: int
    num_floors: int
    viewers: int
    elevators: list[ElevatorView]
    service_log: list[ServiceEventView]  # 최근 서비스 기록 (오래된 것부터)
    waiting: dict[int, int]  # 층별 기다리는 승객 수 (자동 승객)
    recent_wait_s: float | None  # 최근 내린 승객 50명의 평균 대기 시간


def _hall_views(calls: frozenset[HallCall]) -> list[HallCallView]:
    # 집합은 순서가 없으므로 정렬해서 내보낸다 (같은 상태면 항상 같은 JSON).
    ordered = sorted(calls, key=lambda c: (c.floor, c.direction))
    return [HallCallView(floor=c.floor, direction=c.direction) for c in ordered]


def _elevator_view(index: int, car: Elevator, maintenance: bool, riding: int) -> ElevatorView:
    return ElevatorView(
        id=index,
        floor=car.floor,
        position=round(car.position, 3),
        state=car.state,
        direction=car.direction,
        car_calls=sorted(car.car_calls),
        hall_calls=_hall_views(car.hall_calls),
        in_service=car.in_service,
        maintenance_requested=maintenance,
        riding=riding,
        fault=car.fault,
    )


def snapshot_of(
    simulation: Simulation,
    viewers: int,
    service: list[ServiceEvent] | None = None,
    recent_wait_s: float | None = None,
) -> StateSnapshot:
    return StateSnapshot(
        time_ms=simulation.now_ms,
        num_floors=simulation.building.num_floors,
        viewers=viewers,
        elevators=[
            _elevator_view(
                i, car, simulation.is_maintenance_requested(i), simulation.riding_count(i)
            )
            for i, car in enumerate(simulation.elevators)
        ],
        service_log=[ServiceEventView(**vars(e)) for e in service or []],
        waiting=simulation.waiting_counts(),
        recent_wait_s=recent_wait_s,
    )


class HallCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor: int
    direction: Direction


class CarCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor: int


class HallCallResponse(BaseModel):
    car: int | None  # 배차된 카 번호. 운행 중인 카가 없으면 None (복귀하면 배차)


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fault: Fault
