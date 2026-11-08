"""단일 엘리베이터 상태 머신 (LOOK 방식).

상태는 두 가지 사건으로만 바뀐다.
- 시간 경과: `tick(dt_ms)`. 이동, 문 열림/닫힘처럼 시간이 걸리는 일이 끝나면 다음 상태로 간다.
- 호출 입력: `press_car_button`, `assign_hall_call`. 대기 중이면 곧바로 출발하고,
  문이 열려 있는 층의 호출이면 그 자리에서 처리한다.

실제 시계(time.time), I/O, asyncio에 의존하지 않는다. 같은 입력을 같은 순서로 주면
항상 같은 결과가 나온다(결정적). 설계 이유는 docs/design.md 참고.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.simulation.building import Building
from app.simulation.config import ElevatorConfig
from app.simulation.types import (
    DOOR_STATES,
    MOVING_STATES,
    Call,
    CarCall,
    Direction,
    ElevatorState,
    Fault,
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
        self._segment_ms = self.config.floor_travel_ms  # 지금 층간 이동 한 구간의 길이

        # 고장과 운행 중지 (6단계)
        self.fault: Fault | None = None
        # 이동 중에 운행에서 빼면 그 상태를 기억했다가, 복귀하면 이어서 간다 (층 사이에 멈춘 경우).
        self._resume_state: ElevatorState | None = None

        # 집합(set)이라 같은 호출을 여러 번 눌러도 한 번만 남는다.
        self._car_calls: set[int] = set()
        self._hall_calls: set[HallCall] = set()

        self.floors_traveled = 0  # 이동한 층 수 (운행 거리)
        self.history: list[StateChange] = []
        # 상태가 바뀔 때마다 불리는 함수. Simulation이 승객 처리에 쓴다.
        self.on_state_change: Callable[[Elevator, StateChange], None] | None = None
        # 이동 중 층에 닿을 때마다 불리는 함수 (서지 않고 지나가는 층 포함).
        # Monitor가 층간 이동 시간을 잰다.
        self.on_floor_reached: Callable[[Elevator], None] | None = None
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
    def remaining_ms(self) -> int:
        """지금 하고 있는 일(이동, 문 동작)이 끝날 때까지 남은 시간. 대기 중이면 0."""
        return self._remaining_ms

    @property
    def time_to_next_event(self) -> int | None:
        """다음 상태 전이까지 남은 시간. 스스로 바뀌지 않는 상태(대기, 운행 중지, 멈춤)면 None."""
        return None if self._frozen else self._remaining_ms

    @property
    def _frozen(self) -> bool:
        """시간이 흘러도 상태가 바뀌지 않는가."""
        if self.state in (ElevatorState.IDLE, ElevatorState.OUT_OF_SERVICE):
            return True
        # 층 사이 멈춤 고장: 이동 중으로 보이지만 남은 시간이 줄지 않는다.
        return self.fault is Fault.STALL and self.state in MOVING_STATES

    @property
    def in_service(self) -> bool:
        return self.state is not ElevatorState.OUT_OF_SERVICE

    @property
    def position(self) -> float:
        """화면 표시용 위치(층 단위 실수). 이동 중이면 층 사이 값이 나온다."""
        state = self._resume_state or self.state  # 층 사이에서 운행 중지된 카도 그 위치에 그린다
        if state is ElevatorState.MOVING_UP:
            return self.floor + self._travel_progress()
        if state is ElevatorState.MOVING_DOWN:
            return self.floor - self._travel_progress()
        return float(self.floor)

    def _travel_progress(self) -> float:
        return (self._segment_ms - self._remaining_ms) / self._segment_ms

    @property
    def _travel_ms(self) -> int:
        """한 층 이동 시간. 모터 저하 고장이면 2배."""
        factor = 2 if self.fault is Fault.SLOW_MOTOR else 1
        return self.config.floor_travel_ms * factor

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

    # ------------------------------------------------------------------ 고장과 운행 중지

    def inject_fault(self, fault: Fault) -> None:
        """고장을 주입한다. 카는 고장을 알리지 않고 이상하게 움직이기만 한다 (감지는 Monitor)."""
        self.fault = fault

    def repair(self) -> None:
        """고장을 고친다. 운행 중지 상태라면 복귀는 따로 해야 한다 (return_to_service)."""
        self.fault = None

    def take_out_of_service(self) -> list[HallCall]:
        """운행에서 뺀다. 맡고 있던 홀 호출을 돌려준다 (다른 카에 다시 배차하도록).

        카 호출(안에 탄 승객의 목적층)은 남긴다. 복귀하면 이어서 처리한다.
        """
        if self.state is ElevatorState.OUT_OF_SERVICE:
            return []
        if self.state in MOVING_STATES:
            self._resume_state = self.state
        dropped = sorted(self._hall_calls, key=lambda c: (c.floor, c.direction))
        self._hall_calls.clear()
        self.state = ElevatorState.OUT_OF_SERVICE
        self._record()
        return dropped

    def return_to_service(self) -> None:
        """운행에 복귀한다. 층 사이에서 멈췄던 카는 가던 방향으로 이어서 간다."""
        if self.state is not ElevatorState.OUT_OF_SERVICE:
            return
        resume, self._resume_state = self._resume_state, None
        if resume is not None:
            self.state = resume
            self._record()
            return
        self.state = ElevatorState.IDLE
        self.direction = Direction.NONE
        self._remaining_ms = 0
        self._record()
        self._decide()  # 안에 탄 승객의 목적층이 남아 있으면 바로 출발한다

    def cancel_hall_call(self, call: HallCall) -> None:
        """맡았던 홀 호출을 취소한다 (다른 카가 먼저 그 손님을 태운 경우).

        상태는 바로 바꾸지 않는다. 이동 중이면 다음 층에 도착할 때 다시 판단해서,
        더 갈 이유가 없으면 그 층에서 멈춘다.
        """
        self._hall_calls.discard(call)

    def register(self, call: Call) -> None:
        match call:
            case CarCall(floor=floor):
                self.press_car_button(floor)
            case HallCall(floor=floor, direction=direction):
                self.assign_hall_call(floor, direction)

    def _on_new_call(self) -> None:
        if self.state is ElevatorState.OUT_OF_SERVICE:
            return  # 호출은 기억만 하고, 복귀한 뒤 처리한다
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
        self.complete_due_phases()
        while remaining > 0:
            # 스스로 바뀌지 않는 상태(대기, 운행 중지, 멈춤)면 남은 시간은 그냥 흘러간다.
            step = remaining if self._frozen else min(remaining, self._remaining_ms)
            self.elapse(step)
            remaining -= step
            self.complete_due_phases()

    def elapse(self, dt_ms: int) -> None:
        """시계만 dt_ms 진행한다. 상태 전이는 하지 않는다.

        여러 대를 함께 돌릴 때 Simulation이 "모든 카의 시계를 같은 시각으로 맞춘 뒤 전이 처리"를
        하려고 tick을 두 단계(elapse → complete_due_phases)로 나눠 쓴다. 한 카의 전이(예: 승객이
        탐)가 다른 카에 영향을 줄 때, 다른 카도 이미 같은 시각에 있어야 하기 때문이다.
        """
        if not self._frozen:
            if dt_ms > self._remaining_ms:
                raise ValueError("다음 전이 시각을 넘어 시계를 진행할 수 없습니다.")
            self._remaining_ms -= dt_ms
        self.now_ms += dt_ms

    def complete_due_phases(self) -> None:
        """남은 시간이 0이 된 동작을 끝내고 다음 상태로 넘어간다 (연달아 끝나는 것까지)."""
        while not self._frozen and self._remaining_ms == 0:
            self._complete_phase()

    def _complete_phase(self) -> None:
        """시간이 걸리는 동작 하나가 끝났을 때의 상태 전이."""
        match self.state:
            case ElevatorState.MOVING_UP | ElevatorState.MOVING_DOWN:
                self.floor += self.direction.step
                self.floors_traveled += 1
                if self.on_floor_reached is not None:
                    self.on_floor_reached(self)
                self._on_arrive()
            case ElevatorState.DOORS_OPENING:
                self._set_state(ElevatorState.DOORS_OPEN, self.config.door_dwell_ms)
            case ElevatorState.DOORS_OPEN:
                self._set_state(ElevatorState.DOORS_CLOSING, self.config.door_transition_ms)
            case ElevatorState.DOORS_CLOSING:
                if self.fault is Fault.DOOR_SENSOR:
                    # 센서가 막힌 것으로 판단해 다시 연다. 고칠 때까지 열고 닫기를 되풀이한다.
                    self._set_state(ElevatorState.DOORS_OPENING, self.config.door_transition_ms)
                else:
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
            self._segment_ms = self._remaining_ms = self._travel_ms
            return
        if self._has_any_call_here():
            self._open_doors()
        else:
            # 가던 이유(홀 호출)가 취소된 경우: 이 층에서 멈추고 다시 판단한다.
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
        self._segment_ms = self._travel_ms
        self._set_state(state, self._segment_ms)

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

    def drain_history(self) -> list[StateChange]:
        """지금까지의 상태 변화 기록을 꺼내고 비운다.

        오래 도는 실시간 서버에서 기록이 메모리에 끝없이 쌓이지 않게, 꺼내서 DB에 저장하거나 버린다.
        """
        drained, self.history = self.history, []
        return drained

    def _record(self) -> None:
        change = StateChange(self.now_ms, self.state, self.floor, self.direction)
        self.history.append(change)
        if self.on_state_change is not None:
            # 승객 처리(타고 내리기)는 상태가 바뀌는 바로 그 시각에 해야 tick 크기와 무관하다.
            self.on_state_change(self, change)

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
