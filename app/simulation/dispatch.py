"""배차 전략: 홀 호출을 어느 카에 맡길지 정한다 (Strategy 패턴).

시뮬레이션은 전략의 `select`만 부르고, 전략이 어떻게 고르는지는 모른다. 그래서 새 알고리즘을
추가할 때 시뮬레이션 코드를 고치지 않고, 같은 시드로 바꿔 끼우며 비교할 수 있다.

"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.simulation.elevator import Elevator
from app.simulation.types import Direction, ElevatorState


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


STRATEGIES: dict[str, type[DispatchStrategy]] = {s.name: s for s in (NearestCar,)}
