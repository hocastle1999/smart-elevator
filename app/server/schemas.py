"""API 요청과 응답, WebSocket으로 보내는 상태의 형식 (Pydantic)."""

from pydantic import BaseModel, ConfigDict

from app.simulation import Direction, Elevator, ElevatorState, HallCall, Simulation


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


class StateSnapshot(BaseModel):
    """한 시점의 전체 상태. WebSocket 방송과 GET /api/state가 같은 형식을 쓴다."""

    time_ms: int
    num_floors: int
    viewers: int
    elevators: list[ElevatorView]


def _hall_views(calls: frozenset[HallCall]) -> list[HallCallView]:
    # 집합은 순서가 없으므로 정렬해서 내보낸다 (같은 상태면 항상 같은 JSON).
    ordered = sorted(calls, key=lambda c: (c.floor, c.direction))
    return [HallCallView(floor=c.floor, direction=c.direction) for c in ordered]


def _elevator_view(index: int, car: Elevator) -> ElevatorView:
    return ElevatorView(
        id=index,
        floor=car.floor,
        position=round(car.position, 3),
        state=car.state,
        direction=car.direction,
        car_calls=sorted(car.car_calls),
        hall_calls=_hall_views(car.hall_calls),
    )


def snapshot_of(simulation: Simulation, viewers: int) -> StateSnapshot:
    return StateSnapshot(
        time_ms=simulation.now_ms,
        num_floors=simulation.building.num_floors,
        viewers=viewers,
        elevators=[_elevator_view(i, car) for i, car in enumerate(simulation.elevators)],
    )


class HallCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor: int
    direction: Direction


class CarCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor: int


class HallCallResponse(BaseModel):
    car: int  # 배차된 카 번호
