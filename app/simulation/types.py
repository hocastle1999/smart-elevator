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


DOOR_STATES = frozenset(
    {ElevatorState.DOORS_OPENING, ElevatorState.DOORS_OPEN, ElevatorState.DOORS_CLOSING}
)


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
