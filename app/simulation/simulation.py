"""시뮬레이션 실행기: 시간을 진행하면서 예약된 호출을 정확한 시각에 넣는다."""

import heapq
from dataclasses import dataclass

from app.simulation.building import Building
from app.simulation.config import ElevatorConfig
from app.simulation.elevator import Elevator
from app.simulation.types import Call


@dataclass(frozen=True)
class ScheduledCall:
    """time_ms 시각에 들어올 호출."""

    time_ms: int
    call: Call


class Simulation:
    def __init__(
        self,
        building: Building,
        config: ElevatorConfig | None = None,
        start_floor: int | None = None,
    ) -> None:
        self.building = building
        self.elevator = Elevator(building, config, start_floor)
        # (시각, 넣은 순서, 호출). 같은 시각이면 넣은 순서대로 처리해 결과가 항상 같다.
        self._queue: list[tuple[int, int, Call]] = []
        self._seq = 0

    @property
    def now_ms(self) -> int:
        return self.elevator.now_ms

    def schedule(self, scheduled: ScheduledCall) -> None:
        if scheduled.time_ms < self.now_ms:
            raise ValueError("이미 지난 시각에는 호출을 예약할 수 없습니다.")
        heapq.heappush(self._queue, (scheduled.time_ms, self._seq, scheduled.call))
        self._seq += 1

    def schedule_all(self, calls: list[ScheduledCall]) -> None:
        for scheduled in calls:
            self.schedule(scheduled)

    def step(self, dt_ms: int) -> None:
        """dt_ms만큼 진행한다.

        tick 사이에 예약된 호출도 tick 경계로 미루지 않고 그 시각에 정확히 넣는다.
        그래서 tick 크기를 바꿔도 결과가 같다. 같은 시각에 끝나는 동작(예: 문 닫힘 완료)이
        있으면 그 동작이 먼저 끝나고 호출이 들어간다.
        """
        if dt_ms < 0:
            raise ValueError("dt_ms는 0 이상이어야 합니다.")
        end = self.now_ms + dt_ms
        while self._queue and self._queue[0][0] <= end:
            time_ms, _, call = heapq.heappop(self._queue)
            self.elevator.tick(time_ms - self.now_ms)
            self.elevator.register(call)
        self.elevator.tick(end - self.now_ms)

    def run_until(self, end_ms: int, dt_ms: int = 100) -> None:
        """고정 간격 dt_ms로 end_ms까지 진행한다 (나중에 실시간 루프가 하는 방식과 같다)."""
        if dt_ms <= 0:
            raise ValueError("dt_ms는 양수여야 합니다.")
        while self.now_ms < end_ms:
            self.step(min(dt_ms, end_ms - self.now_ms))
