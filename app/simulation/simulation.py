"""시뮬레이션 실행기: 여러 대의 카를 함께 진행하고, 홀 호출을 배차 전략으로 나눠 준다."""

import heapq
from collections.abc import Sequence
from dataclasses import dataclass

from app.simulation.building import Building
from app.simulation.config import ElevatorConfig
from app.simulation.dispatch import DispatchStrategy, NearestCar
from app.simulation.elevator import Elevator
from app.simulation.types import Call, CarCall, Direction, HallCall


@dataclass(frozen=True)
class ScheduledCall:
    """time_ms 시각에 들어올 호출.

    카 호출은 특정 카 안에서 누르는 것이라 `car`(카 번호)가 필요하다. 홀 호출은 건물에서
    누르는 것이라 `car`를 무시하고 배차 전략이 카를 고른다.
    """

    time_ms: int
    call: Call
    car: int = 0


@dataclass(frozen=True)
class Assignment:
    """홀 호출을 어느 카에 맡겼는지의 기록."""

    time_ms: int
    call: HallCall
    car: int


class Simulation:
    def __init__(
        self,
        building: Building,
        num_elevators: int = 1,
        strategy: DispatchStrategy | None = None,
        config: ElevatorConfig | None = None,
        start_floors: Sequence[int] | None = None,
    ) -> None:
        if num_elevators < 1:
            raise ValueError("카는 1대 이상이어야 합니다.")
        if start_floors is None:
            start_floors = [building.bottom] * num_elevators
        if len(start_floors) != num_elevators:
            raise ValueError("start_floors의 개수가 카 대수와 다릅니다.")

        self.building = building
        self.strategy = strategy or NearestCar()
        self.elevators = [Elevator(building, config, floor) for floor in start_floors]
        self.assignments: list[Assignment] = []
        self.now_ms = 0
        # (시각, 넣은 순서, 예약). 같은 시각이면 넣은 순서대로 처리해 결과가 항상 같다.
        self._queue: list[tuple[int, int, ScheduledCall]] = []
        self._seq = 0

    # ------------------------------------------------------------------ 입력

    def press_hall_button(self, floor: int, direction: Direction) -> int:
        """건물의 홀 버튼. 맡은 카의 번호를 돌려준다.

        배차는 호출이 들어오는 순간 한 번만 한다(이후 다시 배정하지 않음). 이미 어느 카가
        맡고 있는 호출이면 다시 배차하지 않고 그 카 번호를 돌려준다.
        """
        self.building.validate_hall_call(floor, direction)
        call = HallCall(floor, direction)
        for index, car in enumerate(self.elevators):
            if call in car.hall_calls:
                return index

        index = self.strategy.select(call, self.elevators)
        if not 0 <= index < len(self.elevators):
            raise ValueError(f"배차 전략이 없는 카 번호를 골랐습니다: {index}")
        self.assignments.append(Assignment(self.now_ms, call, index))
        self.elevators[index].assign_hall_call(floor, direction)
        return index

    def press_car_button(self, car: int, floor: int) -> None:
        """car번 카 안에서 목적층 버튼을 누른다."""
        self._car(car).press_car_button(floor)

    def _car(self, index: int) -> Elevator:
        if not 0 <= index < len(self.elevators):
            raise ValueError(f"{index}번 카는 없습니다.")
        return self.elevators[index]

    def _apply(self, scheduled: ScheduledCall) -> None:
        match scheduled.call:
            case HallCall(floor=floor, direction=direction):
                self.press_hall_button(floor, direction)
            case CarCall(floor=floor):
                self.press_car_button(scheduled.car, floor)

    # ------------------------------------------------------------------ 예약

    def schedule(self, scheduled: ScheduledCall) -> None:
        if scheduled.time_ms < self.now_ms:
            raise ValueError("이미 지난 시각에는 호출을 예약할 수 없습니다.")
        if isinstance(scheduled.call, CarCall):
            self._car(scheduled.car)  # 없는 카 번호면 예약할 때 바로 알린다
        heapq.heappush(self._queue, (scheduled.time_ms, self._seq, scheduled))
        self._seq += 1

    def schedule_all(self, calls: Sequence[ScheduledCall]) -> None:
        for scheduled in calls:
            self.schedule(scheduled)

    # ------------------------------------------------------------------ 시간

    def step(self, dt_ms: int) -> None:
        """dt_ms만큼 진행한다.

        tick 사이에 예약된 호출도 tick 경계로 미루지 않고 그 시각에 정확히 넣는다.
        그래서 tick 크기를 바꿔도 결과가 같다. 같은 시각에 끝나는 동작(예: 문 닫힘 완료)이
        있으면 그 동작이 먼저 끝나고 호출이 들어간다. 배차 전략은 그 시각의 카 상태를 본다.
        """
        if dt_ms < 0:
            raise ValueError("dt_ms는 0 이상이어야 합니다.")
        end = self.now_ms + dt_ms
        while self._queue and self._queue[0][0] <= end:
            time_ms, _, scheduled = heapq.heappop(self._queue)
            self._advance_to(time_ms)
            self._apply(scheduled)
        self._advance_to(end)

    def run_until(self, end_ms: int, dt_ms: int = 100) -> None:
        """고정 간격 dt_ms로 end_ms까지 진행한다 (나중에 실시간 루프가 하는 방식과 같다)."""
        if dt_ms <= 0:
            raise ValueError("dt_ms는 양수여야 합니다.")
        while self.now_ms < end_ms:
            self.step(min(dt_ms, end_ms - self.now_ms))

    def _advance_to(self, time_ms: int) -> None:
        # 카끼리는 tick 동안 서로 영향을 주지 않으므로 번호 순서대로 진행해도 결과가 같다.
        for car in self.elevators:
            car.tick(time_ms - self.now_ms)
        self.now_ms = time_ms

    @property
    def is_idle(self) -> bool:
        """모든 카가 대기 중이고 남은 호출과 예약이 없는가."""
        return not self._queue and all(car.is_idle for car in self.elevators)
