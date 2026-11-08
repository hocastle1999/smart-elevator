"""배차 전략: 홀 호출을 어느 카에 맡길지 정한다 (Strategy 패턴).

시뮬레이션은 전략의 `select`만 부르고, 전략이 어떻게 고르는지는 모른다. 그래서 새 알고리즘을
추가할 때 시뮬레이션 코드를 고치지 않고, 같은 시드로 바꿔 끼우며 비교할 수 있다.

전략 3가지 (4단계에서 비교, docs/experiments.md):
- NearestCar: 거리와 진행 방향으로 매긴 적합도(FS)
- EtaCost: 카가 지금 맡은 정지를 모두 거쳐 호출 층에 도착하기까지의 예상 시간(ETA)
- DestinationDispatch: 목적층을 미리 받아, 내 대기 시간 + 다른 승객에게 주는 지연을 합한 비용
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.simulation.elevator import Elevator
from app.simulation.types import DOOR_STATES, Direction, ElevatorState


@dataclass(frozen=True)
class CallRequest:
    """배차 요청. 목적층 예약 방식에서만 destination이 있다."""

    floor: int
    direction: Direction
    destination: int | None = None


class DispatchContext(Protocol):
    """카 객체만으로는 알 수 없는, 승객 단위 정보 (Simulation이 제공).

    typing.Protocol: 상속 없이 이 메서드들만 있으면 된다(구조적 타이핑). 전략 쪽에서 Simulation
    전체에 의존하지 않고 필요한 것만 요구하려고 쓴다.
    """

    def destinations(self, car: int) -> frozenset[int]:
        """car번 카가 태웠거나 태우기로 배정된 승객들의 목적층."""
        ...

    def load(self, car: int) -> int:
        """car번 카가 태웠거나 태우기로 배정된 승객 수."""
        ...


class DispatchStrategy(ABC):
    """배차 전략의 공통 인터페이스.

    ABC를 쓰는 이유: `select`를 구현하지 않은 전략은 객체를 만드는 순간 TypeError가 나서
    실수를 일찍 잡는다.
    """

    name: str = "base"
    # True면 승객이 층에서 목적층을 입력하고(목적층 예약 방식), 승객마다 따로 배차한다.
    uses_destination: bool = False

    @abstractmethod
    def select(
        self,
        request: CallRequest,
        elevators: Sequence[Elevator],
        context: DispatchContext | None = None,
    ) -> int:
        """요청을 맡을 카의 번호(elevators 안의 위치)를 돌려준다.

        결정성을 위해 같은 입력에는 항상 같은 번호를 돌려줘야 한다 (무작위 금지,
        동점이면 번호가 작은 카).
        """


def _argmin(costs: Sequence[float]) -> int:
    if not costs:
        raise ValueError("배차할 카가 없습니다.")
    # index()는 처음 나오는 위치 = 동점이면 번호가 작은 카
    return list(costs).index(min(costs))


# ---------------------------------------------------------------------- Nearest Car


class NearestCar(DispatchStrategy):
    """가장 가까운 차 (Nearest Car).

    단순히 거리만 보지 않고, 카의 진행 방향까지 따진 적합도(Figure of Suitability, FS)가
    가장 큰 카를 고른다 (Barney, "Elevator Traffic Handbook"). N = 층 수 - 1, d = 거리.

    | 카의 상황                                   | FS        |
    |---------------------------------------------|-----------|
    | 호출 쪽으로 오는 중, 호출과 같은 방향       | N + 2 - d |
    | 호출 쪽으로 오는 중, 호출과 반대 방향       | N + 1 - d |
    | 대기 중 (방향 없음)                         | N + 1 - d |
    | 호출에서 멀어지는 중                        | 1         |
    """

    name = "nearest-car"

    def select(self, request, elevators, context=None) -> int:
        return _argmin([-self.suitability(request, car) for car in elevators])

    @staticmethod
    def suitability(request: CallRequest, car: Elevator) -> float:
        n = car.building.num_floors - 1
        d = abs(request.floor - car.position)
        if car.direction is Direction.NONE:
            return n + 1 - d
        if not _is_approaching(car, request.floor):
            return 1
        if request.direction is car.direction:
            return n + 2 - d
        return n + 1 - d


def _is_approaching(car: Elevator, floor: int) -> bool:
    """카가 진행 방향으로 계속 가면 floor층에 닿는가.

    이동 중인 카는 이미 떠난 층에는 설 수 없으므로 "다음 층"부터 따진다.
    """
    moving = car.state in (ElevatorState.MOVING_UP, ElevatorState.MOVING_DOWN)
    reference = car.floor + car.direction.step if moving else car.floor
    if car.direction is Direction.UP:
        return floor >= reference
    return floor <= reference


# ---------------------------------------------------------------------- ETA


def door_cycle_ms(car: Elevator) -> int:
    """한 번 정지할 때 문 때문에 쓰는 시간 (열림 + 대기 + 닫힘)."""
    c = car.config
    return 2 * c.door_transition_ms + c.door_dwell_ms


def estimate_arrival_ms(
    car: Elevator, floor: int, direction: Direction, extra_stops: Iterable[int] = ()
) -> int:
    """카가 floor층에서 direction 방향 손님을 태우러 문을 열기까지의 예상 시간(ms).

    카가 지금 맡은 정지(카 호출, 홀 호출 층)를 LOOK 순서대로 거친다고 보고, 층마다 이동 시간,
    정지마다 문 시간을 더한다. 앞으로 새로 생길 호출과 승객이 타고 내리는 시간은 모른다(추정).
    """
    cfg = car.config
    travel = cfg.floor_travel_ms
    stops = set(car.car_calls) | {c.floor for c in car.hall_calls} | set(extra_stops)
    d = car.direction
    t = car.remaining_ms
    moving = car.state in (ElevatorState.MOVING_UP, ElevatorState.MOVING_DOWN)

    if moving:
        cur = car.floor + d.step  # t 뒤에 도착할 층
    else:
        cur = car.floor
        if car.state in (ElevatorState.DOORS_OPENING, ElevatorState.DOORS_OPEN):
            if cur == floor and d in (direction, Direction.NONE):
                return 0  # 지금 문이 열려 있는 카가 바로 태운다
            if car.state is ElevatorState.DOORS_OPENING:
                t += cfg.door_dwell_ms
            t += cfg.door_transition_ms
        stops.discard(cur)  # 지금 층 정지는 이미 처리 중
        if d is Direction.NONE:
            if cur == floor:
                return t
            d = Direction.UP if floor > cur else Direction.DOWN

    def beyond(f: int, way: Direction) -> bool:
        target_beyond = floor > f if way is Direction.UP else floor < f
        return target_beyond or any((s > f) if way is Direction.UP else (s < f) for s in stops)

    first = not moving
    for _ in range(4 * car.building.num_floors):
        if cur == floor and (d is direction or not beyond(cur, d)):
            return t
        if not first and cur in stops:
            t += door_cycle_ms(car)
            stops.discard(cur)
        first = False
        if not beyond(cur, d):
            d = d.opposite
        cur += d.step
        t += travel
    return t  # 판단 로직이 맞다면 오지 않는다


class EtaCost(DispatchStrategy):
    """도착 예상시간(ETA)이 가장 짧은 카를 고른다.

    Nearest Car의 FS는 "같은 방향으로 오는 중"이면 중간에 정지가 10번 있어도 높은 점수를 준다.
    ETA는 그 카가 이미 맡은 정지마다 문 시간을 더하므로, 바쁜 카를 피한다.
    """

    name = "eta"

    def select(self, request, elevators, context=None) -> int:
        return _argmin(
            [estimate_arrival_ms(car, request.floor, request.direction) for car in elevators]
        )


# ---------------------------------------------------------------------- 목적층 예약


class DestinationDispatch(DispatchStrategy):
    """목적층 예약 방식 (Destination Dispatch).

    승객이 층에서 목적층을 누르면, 시스템이 "몇 호기로 가세요"를 알려 준다. 목적층을 미리 알기
    때문에 같은 목적층 승객을 한 카에 모아 정지 횟수를 줄일 수 있다.

    비용 = 이 승객의 대기 시간(ETA) + 새로 생기는 정지 때문에 다른 승객이 더 기다리는 시간
         = ETA + 문 시간 × (새 정지 수) × (그 카가 맡은 승객 수)
    정원이 찬 카는 고르지 않는다.
    """

    name = "destination"
    uses_destination = True

    def select(self, request, elevators, context=None) -> int:
        costs: list[float] = []
        for i, car in enumerate(elevators):
            eta = estimate_arrival_ms(car, request.floor, request.direction)
            if request.destination is None or context is None:
                costs.append(eta)  # 목적층을 모르면 ETA 방식과 같다
                continue
            load = context.load(i)
            if load >= car.config.capacity:
                costs.append(float("inf"))
                continue
            planned = (
                set(car.car_calls) | {c.floor for c in car.hall_calls} | context.destinations(i)
            )
            if car.state in DOOR_STATES:
                # 지금 서 있는 층은 이미 정지 중이다. 홀 호출은 문을 열 때 지워지므로 따로 넣는다.
                planned.add(car.floor)
            new_stops = sum(1 for f in (request.floor, request.destination) if f not in planned)
            costs.append(eta + door_cycle_ms(car) * new_stops * load)
        return _argmin(costs)


STRATEGIES: dict[str, type[DispatchStrategy]] = {
    s.name: s for s in (NearestCar, EtaCost, DestinationDispatch)
}
