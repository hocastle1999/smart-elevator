"""단일 엘리베이터 상태 머신 (LOOK 방식).

상태는 두 가지 사건으로만 바뀐다.
- 시간 경과: `tick(dt_ms)`. 이동, 문 열림/닫힘처럼 시간이 걸리는 일이 끝나면 다음 상태로 간다.
- 호출 입력: `press_car_button`, `assign_hall_call`. 대기 중이면 곧바로 출발하고,
  문이 열려 있는 층의 호출이면 그 자리에서 처리한다.

실제 시계(time.time), I/O, asyncio에 의존하지 않는다. 같은 입력을 같은 순서로 주면
항상 같은 결과가 나온다(결정적). 설계 이유는 docs/design.md 참고.
"""

from dataclasses import dataclass

from app.simulation.building import Building
from app.simulation.config import ElevatorConfig
from app.simulation.types import (
    DOOR_STATES,
    Call,
    CarCall,
    Direction,
    ElevatorState,
    HallCall,
)


@dataclass(frozen=True)
class StateChange:
    """상태가 바뀐 기록. 테스트와 나중의 운행 기록(DB 저장)에 쓴다."""

    time_ms: int
    state: ElevatorState
    floor: int
    direction: Direction


class Elevator:
    def __init__(
        self,
        building: Building,
        config: ElevatorConfig | None = None,
        start_floor: int | None = None,
    ) -> None:
        self.building = building
        self.config = config or ElevatorConfig()
        start = building.bottom if start_floor is None else start_floor
        building.validate_floor(start)

        self.now_ms = 0
        # 이동 중이면 "마지막으로 떠난 층", 그 밖에는 "지금 서 있는 층"
        self.floor = start
        self.state = ElevatorState.IDLE
        # 진행 방향(의도). 문이 열려 있는 동안에도 "이 다음에 어느 쪽으로 갈지"를 기억해야
        # 같은 방향 홀 호출만 태우고, 닫힌 뒤 어느 쪽으로 갈지 정할 수 있다.
        self.direction = Direction.NONE
        # 지금 하고 있는 일(이동, 문 동작)이 끝날 때까지 남은 시간
        self._remaining_ms = 0

        # 집합(set)이라 같은 호출을 여러 번 눌러도 한 번만 남는다.
        self._car_calls: set[int] = set()
        self._hall_calls: set[HallCall] = set()

        self.history: list[StateChange] = []
        self._record()

    # ------------------------------------------------------------------ 조회

    @property
    def car_calls(self) -> frozenset[int]:
        return frozenset(self._car_calls)

    @property
    def hall_calls(self) -> frozenset[HallCall]:
        return frozenset(self._hall_calls)

    @property
    def is_idle(self) -> bool:
        """대기 중이고 처리할 호출이 하나도 없는가."""
        return self.state is ElevatorState.IDLE and not self._car_calls and not self._hall_calls

    @property
    def position(self) -> float:
        """화면 표시용 위치(층 단위 실수). 이동 중이면 층 사이 값이 나온다."""
        if self.state is ElevatorState.MOVING_UP:
            return self.floor + self._travel_progress()
        if self.state is ElevatorState.MOVING_DOWN:
            return self.floor - self._travel_progress()
        return float(self.floor)

    def _travel_progress(self) -> float:
        travel = self.config.floor_travel_ms
        return (travel - self._remaining_ms) / travel

    # ------------------------------------------------------------------ 입력

    def press_car_button(self, floor: int) -> None:
        self.building.validate_floor(floor)
        self._car_calls.add(floor)
        self._on_new_call()

    def assign_hall_call(self, floor: int, direction: Direction) -> None:
        """이 카에 홀 호출을 맡긴다.

        홀 버튼은 건물에 있고, 여러 대 중 어느 카가 갈지는 배차 전략이 정한다
        (`Simulation.press_hall_button`). 카는 맡은 홀 호출만 처리한다.
        """
        self.building.validate_hall_call(floor, direction)
        self._hall_calls.add(HallCall(floor, direction))
        self._on_new_call()

    def register(self, call: Call) -> None:
        match call:
            case CarCall(floor=floor):
                self.press_car_button(floor)
            case HallCall(floor=floor, direction=direction):
                self.assign_hall_call(floor, direction)

    def _on_new_call(self) -> None:
        if self.state is ElevatorState.IDLE:
            self._decide()
            return
        if self.state in DOOR_STATES and self._absorb_calls_here():
            if self.state is ElevatorState.DOORS_OPEN:
                # 문 앞에서 버튼을 누르면 문이 열려 있는 시간이 다시 처음부터 시작된다.
                self._remaining_ms = self.config.door_dwell_ms
            elif self.state is ElevatorState.DOORS_CLOSING:
                self._reopen_doors()
        # 이동 중에 들어온 호출은 다음 층에 도착할 때 판단한다.

    # ------------------------------------------------------------------ 시간

    def tick(self, dt_ms: int) -> None:
        """dt_ms만큼 시간을 진행한다.

        dt_ms가 한 동작보다 길면 남은 시간을 다음 동작에 넘긴다. 그래서 tick을
        100ms씩 여러 번 하든 한 번에 크게 하든 결과가 같다.
        """
        if dt_ms < 0:
            raise ValueError("dt_ms는 0 이상이어야 합니다.")
        remaining = dt_ms
        while self.state is not ElevatorState.IDLE:
            if self._remaining_ms == 0:
                self._complete_phase()
                continue
            if remaining == 0:
                break
            step = min(remaining, self._remaining_ms)
            self._remaining_ms -= step
            remaining -= step
            self.now_ms += step
        # 대기 중이면 남은 시간은 그냥 흘러간다.
        self.now_ms += remaining

    def _complete_phase(self) -> None:
        """시간이 걸리는 동작 하나가 끝났을 때의 상태 전이."""
        match self.state:
            case ElevatorState.MOVING_UP | ElevatorState.MOVING_DOWN:
                self.floor += self.direction.step
                self._on_arrive()
            case ElevatorState.DOORS_OPENING:
                self._set_state(ElevatorState.DOORS_OPEN, self.config.door_dwell_ms)
            case ElevatorState.DOORS_OPEN:
                self._set_state(ElevatorState.DOORS_CLOSING, self.config.door_transition_ms)
            case ElevatorState.DOORS_CLOSING:
                self._decide()

    # ------------------------------------------------------------------ LOOK 판단

    def _on_arrive(self) -> None:
        """이동 중 한 층에 도착했을 때: 설지, 계속 갈지 정한다.

        단순화: 실제 엘리베이터는 감속 거리 때문에 층에 닿기 전에 정지를 결정하지만,
        여기서는 층에 도착하는 순간 결정한다.
        """
        d = self.direction
        stop_here = (
            self.floor in self._car_calls
            or HallCall(self.floor, d) in self._hall_calls
            # 진행 방향에 더 할 일이 없으면 이 층이 반환점이다 (반대 방향 홀 호출을 태운다).
            or not self._has_calls_beyond(d)
        )
        if not stop_here:
            self._remaining_ms = self.config.floor_travel_ms
            return
        if self._has_any_call_here():
            self._open_doors()
        else:
            # 호출을 취소하는 기능이 없어 실제로는 오지 않는 경우지만, 안전하게 다시 판단한다.
            self._decide()

    def _decide(self) -> None:
        """층에 서 있고 문이 닫힌 상태에서 다음 행동을 정한다 (LOOK).

        1. 진행 방향의 이 층 호출이 있으면 문을 연다.
        2. 진행 방향 앞쪽에 호출이 남아 있으면 그대로 간다.
        3. 앞쪽이 비었으면 이 층의 반대 방향 호출을 처리하거나, 방향을 바꿔 간다.
        4. 아무 호출도 없으면 대기한다.
        """
        d = self.direction
        if d is not Direction.NONE:
            if self._has_call_here(d):
                self._open_doors()
                return
            if self._has_calls_beyond(d):
                self._start_moving(d)
                return
        if self._has_any_call_here():
            self._open_doors()
            return
        next_direction = self._turn_direction()
        if next_direction is Direction.NONE:
            self.direction = Direction.NONE
            self._set_state(ElevatorState.IDLE, 0)
        else:
            self._start_moving(next_direction)

    def _turn_direction(self) -> Direction:
        """진행 방향 앞쪽에 할 일이 없을 때 새로 갈 방향."""
        d = self.direction
        if d is not Direction.NONE:
            return d.opposite if self._has_calls_beyond(d.opposite) else Direction.NONE
        # 대기 중에 처음 출발할 때: 가장 가까운 호출 쪽으로 간다. 거리가 같으면 위쪽.
        # 지금 층은 뺀다. 이 층 호출은 문을 열어 처리하지, 방향을 정하는 데 쓰지 않는다.
        targets = (self._car_calls | {c.floor for c in self._hall_calls}) - {self.floor}
        if not targets:
            return Direction.NONE
        nearest = min(targets, key=lambda f: (abs(f - self.floor), f < self.floor))
        return Direction.UP if nearest > self.floor else Direction.DOWN

    def _direction_at_stop(self) -> Direction:
        """문을 열 때, 이 층에서 태울 사람이 갈 방향(= 다음 진행 방향)을 정한다."""
        d = self.direction
        if d is not Direction.NONE:
            # 진행 방향으로 계속 갈 이유가 있으면 방향을 유지한다.
            if HallCall(self.floor, d) in self._hall_calls or self._has_calls_beyond(d):
                return d
            o = d.opposite
            if HallCall(self.floor, o) in self._hall_calls or self._has_calls_beyond(o):
                return o
            return Direction.NONE
        # 대기 중에 이 층 호출로 문을 연 경우: 이 층의 홀 호출 방향을 따른다.
        for candidate in (Direction.UP, Direction.DOWN):
            if HallCall(self.floor, candidate) in self._hall_calls:
                return candidate
        return self._turn_direction()

    # ------------------------------------------------------------------ 동작

    def _start_moving(self, direction: Direction) -> None:
        next_floor = self.floor + direction.step
        # 판단 로직이 맞다면 일어나지 않는다. 틀렸을 때 건물 밖으로 나가지 않게 막는다.
        assert self.building.bottom <= next_floor <= self.building.top, "건물 범위 밖 이동"
        self.direction = direction
        state = ElevatorState.MOVING_UP if direction is Direction.UP else ElevatorState.MOVING_DOWN
        self._set_state(state, self.config.floor_travel_ms)

    def _open_doors(self) -> None:
        self.direction = self._direction_at_stop()
        self._clear_served_calls()
        self._set_state(ElevatorState.DOORS_OPENING, self.config.door_transition_ms)

    def _reopen_doors(self) -> None:
        # 닫히던 만큼만 다시 열면 된다 (반쯤 닫혔으면 반만 열면 됨).
        closed_so_far = self.config.door_transition_ms - self._remaining_ms
        self._set_state(ElevatorState.DOORS_OPENING, closed_so_far)

    def _clear_served_calls(self) -> None:
        """이 층에서 문을 열면서 처리되는 호출을 지운다."""
        self._car_calls.discard(self.floor)
        if self.direction is not Direction.NONE:
            self._hall_calls.discard(HallCall(self.floor, self.direction))

    def _absorb_calls_here(self) -> bool:
        """문이 열려 있는(열리는, 닫히는) 층에 새로 들어온 호출을 바로 처리한다.

        반대 방향 홀 호출은 처리하지 않고 남겨 둔다. 그 사람은 이 카를 타면 안 되기 때문이다.
        """
        served = False
        if self.floor in self._car_calls:
            self._car_calls.discard(self.floor)
            served = True
        if self.direction is Direction.NONE:
            for candidate in (Direction.UP, Direction.DOWN):
                if HallCall(self.floor, candidate) in self._hall_calls:
                    self.direction = candidate
                    break
        call = HallCall(self.floor, self.direction)
        if call in self._hall_calls:
            self._hall_calls.discard(call)
            served = True
        return served

    def _set_state(self, state: ElevatorState, duration_ms: int) -> None:
        self._remaining_ms = duration_ms
        if state is not self.state:
            self.state = state
            self._record()

    def _record(self) -> None:
        self.history.append(StateChange(self.now_ms, self.state, self.floor, self.direction))

    # ------------------------------------------------------------------ 호출 검사

    def _has_call_here(self, direction: Direction) -> bool:
        return self.floor in self._car_calls or HallCall(self.floor, direction) in self._hall_calls

    def _has_any_call_here(self) -> bool:
        return self.floor in self._car_calls or any(c.floor == self.floor for c in self._hall_calls)

    def _has_calls_beyond(self, direction: Direction) -> bool:
        """진행 방향 앞쪽(지금 층 제외)에 처리할 호출이 있는가."""
        if direction is Direction.NONE:
            return False
        floors = self._car_calls | {c.floor for c in self._hall_calls}
        if direction is Direction.UP:
            return any(f > self.floor for f in floors)
        return any(f < self.floor for f in floors)
