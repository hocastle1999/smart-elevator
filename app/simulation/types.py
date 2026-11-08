"""시뮬레이션 전체에서 쓰는 기본 타입 (방향, 상태, 호출)."""

from dataclasses import dataclass
from enum import StrEnum


class Direction(StrEnum):
    """엘리베이터의 진행 방향(의도)이자 홀 호출의 방향.

    StrEnum이라 나중에 JSON(WebSocket, DB)으로 내보낼 때 "UP"처럼 그대로 쓸 수 있다.
    """

    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"  # 정해진 방향 없음 (대기 중)

    @property
    def opposite(self) -> "Direction":
        if self is Direction.UP:
            return Direction.DOWN
        if self is Direction.DOWN:
            return Direction.UP
        return Direction.NONE

    @property
    def step(self) -> int:
        """한 층 이동할 때 층 번호의 변화량 (+1, -1, 0)."""
        return {Direction.UP: 1, Direction.DOWN: -1, Direction.NONE: 0}[self]


class ElevatorState(StrEnum):
    """카가 물리적으로 하고 있는 일. 설계 이유는 docs/design.md 참고."""

    IDLE = "IDLE"  # 층에 서 있고 문이 닫혀 있으며 할 일이 없다
    MOVING_UP = "MOVING_UP"
    MOVING_DOWN = "MOVING_DOWN"
    DOORS_OPENING = "DOORS_OPENING"
    DOORS_OPEN = "DOORS_OPEN"
    DOORS_CLOSING = "DOORS_CLOSING"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"  # 운행에서 뺀 상태 (고장 격리, 점검). 배차 대상이 아니다


MOVING_STATES = frozenset({ElevatorState.MOVING_UP, ElevatorState.MOVING_DOWN})

DOOR_STATES = frozenset(
    {ElevatorState.DOORS_OPENING, ElevatorState.DOORS_OPEN, ElevatorState.DOORS_CLOSING}
)


class Fault(StrEnum):
    """주입할 수 있는 고장 (6단계). 카는 고장을 알리지 않는다. 감지는 Monitor가 동작을 보고 한다."""

    DOOR_SENSOR = "DOOR_SENSOR"  # 문 센서 이상: 닫힐 때마다 다시 열린다
    STALL = "STALL"  # 층 사이 멈춤: 이동 중으로 보이지만 더 나아가지 않는다
    SLOW_MOTOR = "SLOW_MOTOR"  # 모터 저하: 층간 이동 시간이 2배


@dataclass(frozen=True)
class HallCall:
    """층의 버튼(올라감/내려감)으로 부른 호출."""

    floor: int
    direction: Direction


@dataclass(frozen=True)
class CarCall:
    """카 안에서 목적층 버튼을 누른 호출."""

    floor: int


Call = HallCall | CarCall
