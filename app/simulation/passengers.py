"""승객 모델과 운행 결과 통계 (4단계).

승객은 출발층에 도착해 버튼을 누르고, 알맞은 카가 문을 열면 타서 목적층 버튼을 누르고,
목적층에서 내린다. 시각을 모두 기록해 대기 시간과 이동 시간을 잰다.

단순화: 타고 내리는 데 걸리는 시간은 없다(문이 열려 있는 시간 안에 끝난다고 본다).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.simulation.types import Direction


@dataclass(frozen=True)
class PassengerArrival:
    """time_ms 시각에 origin층에 도착해 destination층으로 가려는 승객 (예약용)."""

    time_ms: int
    origin: int
    destination: int


@dataclass
class Passenger:
    id: int
    arrival_ms: int
    origin: int
    destination: int
    # 목적층 예약 방식에서 이 승객에게 배정된 카. 일반 방식은 오는 카 아무거나 탄다.
    assigned_car: int | None = None
    car: int | None = None  # 실제로 탄 카
    board_ms: int | None = None
    alight_ms: int | None = None

    @property
    def direction(self) -> Direction:
        return Direction.UP if self.destination > self.origin else Direction.DOWN

    @property
    def wait_ms(self) -> int | None:
        """도착부터 탈 때까지 (대기 시간)."""
        return None if self.board_ms is None else self.board_ms - self.arrival_ms

    @property
    def journey_ms(self) -> int | None:
        """도착부터 내릴 때까지 (전체 이동 시간)."""
        return None if self.alight_ms is None else self.alight_ms - self.arrival_ms


def percentile(values: Sequence[int], p: float) -> int:
    """nearest-rank 방식의 백분위수: 정렬했을 때 앞에서 ceil(p% × n)번째 값.

    보간(두 값 사이를 나눠 계산)하지 않아 결과가 항상 실제로 관측된 값이고, 손으로 검산하기 쉽다.
    """
    if not values:
        raise ValueError("값이 없습니다.")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class RunStats:
    """한 번의 시뮬레이션 결과 요약. 시간은 초 단위."""

    passengers: int
    served: int  # 목적층에서 내린 승객 수
    mean_wait_s: float
    p95_wait_s: float
    max_wait_s: float
    mean_journey_s: float
    p95_journey_s: float
    floors_traveled: int  # 모든 카가 이동한 층 수 합 (에너지, 마모의 대리 지표)
    stops: int  # 모든 카가 문을 연 횟수 합


def summarize(passengers: Sequence[Passenger], floors_traveled: int, stops: int) -> RunStats:
    waits = [p.wait_ms for p in passengers if p.wait_ms is not None]
    journeys = [p.journey_ms for p in passengers if p.journey_ms is not None]

    def mean_s(values: list[int]) -> float:
        return sum(values) / len(values) / 1000 if values else 0.0

    def p95_s(values: list[int]) -> float:
        return percentile(values, 95) / 1000 if values else 0.0

    return RunStats(
        passengers=len(passengers),
        served=len(journeys),
        mean_wait_s=mean_s(waits),
        p95_wait_s=p95_s(waits),
        max_wait_s=max(waits) / 1000 if waits else 0.0,
        mean_journey_s=mean_s(journeys),
        p95_journey_s=p95_s(journeys),
        floors_traveled=floors_traveled,
        stops=stops,
    )
