"""이상 감지와 서비스 기록 (6단계).

카는 고장을 스스로 알리지 않는다. Monitor는 실제 관제 시스템처럼 카의 "동작"(상태 변화, 층 도착
시각)만 보고 이상을 찾는다. 그래서 고장이 난 뒤 감지되기까지 시간이 걸리고, 그동안 배차 전략은
고장 난 카에 계속 호출을 맡긴다. 감지가 빠를수록 피해가 줄어든다 (docs/experiments.md).

| 규칙 | 조건 | 심각도 | 조치 |
|---|---|---|---|
| DOOR_REOPEN_LOOP | 출발하지 않고 문이 연속 10번 다시 열림 | critical | 운행에서 뺌 |
| STALLED | 이동 중인데 다음 층에 정상 시간의 3배가 지나도 닿지 않음 | critical | 운행에서 뺌 |
| SLOW_TRAVEL | 층간 이동이 정상의 1.5배를 넘는 구간이 연속 3번 | warning | 기록만 (점검 권고) |

임계값은 정상 운행 데이터로 정했다. 4단계 실험 135회에서 출발 없이 연속으로 다시 열린 최대
횟수는 6번(출근 시간, 분당 30명)이었다. 3번으로 잡으면 붐비는 정상 운행을 고장으로 오판한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from app.simulation.types import MOVING_STATES, ElevatorState

if TYPE_CHECKING:  # 실행 시에는 import하지 않는다 (simulation.py가 이 파일을 import하므로 순환)
    from app.simulation.elevator import Elevator, StateChange
    from app.simulation.simulation import Simulation


class Rule(StrEnum):
    DOOR_REOPEN_LOOP = "DOOR_REOPEN_LOOP"
    STALLED = "STALLED"
    SLOW_TRAVEL = "SLOW_TRAVEL"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


class ServiceKind(StrEnum):
    """서비스 기록의 종류. 고장 주입부터 감지, 운행 중지, 수리, 복귀까지 한 줄로 남긴다."""

    FAULT = "FAULT"  # 고장 주입 (실제 건물이라면 "고장 발생". 시스템은 이 시점을 모른다)
    ALERT = "ALERT"  # 이상 감지
    OUT_OF_SERVICE = "OUT_OF_SERVICE"
    MAINTENANCE_REQUEST = "MAINTENANCE_REQUEST"  # 점검 요청 (맡은 일을 끝낸 뒤 운행 중지)
    REPAIR = "REPAIR"
    RETURN = "RETURN"  # 운행 복귀


@dataclass(frozen=True)
class ServiceEvent:
    time_ms: int
    car: int
    kind: ServiceKind
    detail: str = ""  # 고장 종류, 규칙 이름, 운행 중지 사유
    severity: Severity | None = None  # ALERT만
    message: str = ""


@dataclass(frozen=True)
class MonitorConfig:
    reopen_limit: int = 10
    stall_factor: float = 3.0
    slow_factor: float = 1.5
    slow_segments: int = 3
    # critical 알림이 나면 그 카를 자동으로 운행에서 빼고 호출을 다른 카에 넘긴다.
    auto_isolate: bool = True


CRITICAL_RULES = frozenset({Rule.DOOR_REOPEN_LOOP, Rule.STALLED})


class Monitor:
    def __init__(self, sim: Simulation, config: MonitorConfig | None = None) -> None:
        self.sim = sim
        self.config = config or MonitorConfig()
        n = len(sim.elevators)
        self._nominal_ms = sim.config.floor_travel_ms
        self._reopens = [0] * n  # 출발 없이 연속으로 다시 열린 횟수
        self._segment_start: list[int | None] = [None] * n  # 지금 층간 구간을 시작한 시각
        self._slow_streak = [0] * n
        # 같은 규칙으로 같은 카에 알림을 반복하지 않는다. 복귀하면 초기화한다.
        self._raised: list[set[Rule]] = [set() for _ in range(n)]
        sim.monitor = self

    @property
    def _stall_ms(self) -> int:
        return int(self.config.stall_factor * self._nominal_ms)

    # ------------------------------------------------------------------ Simulation이 부르는 곳

    def on_state_change(
        self, car: int, elevator: Elevator, change: StateChange, previous: ElevatorState
    ) -> None:
        state = change.state
        if state is ElevatorState.DOORS_OPENING and previous is ElevatorState.DOORS_CLOSING:
            self._reopens[car] += 1
            if self._reopens[car] >= self.config.reopen_limit:
                self._raise(
                    car,
                    Rule.DOOR_REOPEN_LOOP,
                    f"{change.floor}층에서 출발하지 않고 문이 {self._reopens[car]}번 다시 열림",
                )
        elif state in MOVING_STATES or state in (
            ElevatorState.IDLE,
            ElevatorState.OUT_OF_SERVICE,
        ):
            self._reopens[car] = 0
        # 층간 구간: 출발하는 순간 시작하고, 서면 끝난다.
        self._segment_start[car] = change.time_ms if state in MOVING_STATES else None

    def on_floor_reached(self, car: int, elevator: Elevator) -> None:
        start = self._segment_start[car]
        now = elevator.now_ms
        if start is not None:
            if now - start > self.config.slow_factor * self._nominal_ms:
                self._slow_streak[car] += 1
                if self._slow_streak[car] >= self.config.slow_segments:
                    self._raise(
                        car,
                        Rule.SLOW_TRAVEL,
                        f"층간 이동 {(now - start) / 1000:.1f}초"
                        f" (정상 {self._nominal_ms / 1000:.1f}초)",
                    )
            else:
                self._slow_streak[car] = 0
        self._segment_start[car] = now  # 다음 층까지의 구간이 시작된다 (서면 상태 변화가 지운다)

    def next_deadline(self) -> int | None:
        """멈춤을 판정할 가장 이른 시각. Simulation이 이 시각에 정확히 on_time을 부른다.

        정해진 시각에 판정해야 tick 크기와 무관하게 같은 시각에 알림이 난다.
        """
        deadlines = [
            start + self._stall_ms
            for car, start in enumerate(self._segment_start)
            if start is not None and Rule.STALLED not in self._raised[car]
        ]
        return min(deadlines, default=None)

    def on_time(self, now_ms: int) -> None:
        for car, start in enumerate(self._segment_start):
            if start is None or Rule.STALLED in self._raised[car]:
                continue
            if now_ms >= start + self._stall_ms:
                self._segment_start[car] = None
                self._raise(
                    car,
                    Rule.STALLED,
                    f"{(now_ms - start) / 1000:.0f}초 동안 다음 층에 닿지 않음",
                )

    def reset(self, car: int) -> None:
        """운행에 복귀한 카는 처음부터 다시 지켜본다."""
        self._raised[car].clear()
        self._reopens[car] = 0
        self._slow_streak[car] = 0
        self._segment_start[car] = None

    # ------------------------------------------------------------------ 알림

    def _raise(self, car: int, rule: Rule, message: str) -> None:
        if rule in self._raised[car]:
            return
        self._raised[car].add(rule)
        severity = Severity.CRITICAL if rule in CRITICAL_RULES else Severity.WARNING
        self.sim.log_service(ServiceKind.ALERT, car, rule.value, severity, message)
        if severity is Severity.CRITICAL and self.config.auto_isolate:
            self.sim.take_out_of_service(car, reason=rule.value)
