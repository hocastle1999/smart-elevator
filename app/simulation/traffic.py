"""호출 발생기. 시뮬레이션에서 무작위성은 이곳에만 있다.

전역 `random` 대신 시드로 만든 `random.Random` 객체를 쓴다. 전역 상태를 쓰면 다른 코드가
난수를 하나만 더 뽑아도 순서가 바뀌어, 같은 시드라도 결과가 달라질 수 있기 때문이다.
"""

import random
from dataclasses import dataclass

from app.simulation.building import Building
from app.simulation.passengers import PassengerArrival
from app.simulation.simulation import ScheduledCall
from app.simulation.types import Direction, HallCall


def random_hall_calls(
    building: Building, count: int, duration_ms: int, seed: int
) -> list[ScheduledCall]:
    """0 ~ duration_ms 사이 무작위 시각에 무작위 층의 홀 호출 count개를 만든다."""
    rng = random.Random(seed)
    calls: list[ScheduledCall] = []
    for _ in range(count):
        time_ms = rng.randrange(duration_ms)
        floor = rng.randint(building.bottom, building.top)
        if floor == building.top:
            direction = Direction.DOWN
        elif floor == building.bottom:
            direction = Direction.UP
        else:
            direction = rng.choice([Direction.UP, Direction.DOWN])
        calls.append(ScheduledCall(time_ms, HallCall(floor, direction)))
    # sorted는 안정 정렬이라 같은 시각이면 만든 순서가 유지된다.
    return sorted(calls, key=lambda c: c.time_ms)


@dataclass(frozen=True)
class TrafficPattern:
    """승객의 출발층과 목적층이 어떻게 나뉘는지.

    로비(1층)에서 위층으로 가는 비율, 위층에서 로비로 가는 비율, 나머지는 위층끼리(층간) 이동이다.
    """

    name: str
    lobby_to_upper: float
    upper_to_lobby: float

    def __post_init__(self) -> None:
        if not (0 <= self.lobby_to_upper and 0 <= self.upper_to_lobby):
            raise ValueError("비율은 0 이상이어야 합니다.")
        if self.lobby_to_upper + self.upper_to_lobby > 1:
            raise ValueError("두 비율의 합은 1 이하여야 합니다.")


# 사무실 건물의 대표적인 세 시간대 (비율은 교과서에서 흔히 쓰는 근사치)
UP_PEAK = TrafficPattern("up-peak", lobby_to_upper=0.85, upper_to_lobby=0.05)  # 출근
LUNCH = TrafficPattern("lunch", lobby_to_upper=0.40, upper_to_lobby=0.40)  # 점심
DOWN_PEAK = TrafficPattern("down-peak", lobby_to_upper=0.05, upper_to_lobby=0.85)  # 퇴근
PATTERNS = {p.name: p for p in (UP_PEAK, LUNCH, DOWN_PEAK)}


def poisson_passengers(
    building: Building,
    pattern: TrafficPattern,
    per_minute: float,
    duration_ms: int,
    seed: int,
) -> list[PassengerArrival]:
    """평균 분당 per_minute명이 포아송 과정으로 도착하는 승객 목록을 만든다.

    포아송 과정: 승객이 서로 독립적으로, 일정한 평균 비율로 도착한다는 모형이다. 이때 다음
    승객까지의 간격은 지수 분포를 따르므로 `expovariate`로 간격을 뽑아 더한다. 같은 간격(예:
    정확히 5초마다)으로 넣으면 실제처럼 몰렸다 비었다 하는 변동이 없어서 대기 시간을 과소평가한다.

    간격은 실수로 뽑지만 승객의 도착 시각은 정수 ms로 내림해 엔진의 정수 시간 규칙을 지킨다.
    """
    if building.num_floors < 3:
        raise ValueError("층간 이동을 만들려면 3개 층 이상이어야 합니다.")
    if per_minute <= 0:
        raise ValueError("per_minute는 양수여야 합니다.")
    rng = random.Random(seed)
    rate_per_ms = per_minute / 60_000
    lobby = building.bottom
    upper = range(lobby + 1, building.top + 1)
    arrivals: list[PassengerArrival] = []
    t = 0.0
    while True:
        t += rng.expovariate(rate_per_ms)
        if t >= duration_ms:
            break
        r = rng.random()
        if r < pattern.lobby_to_upper:
            origin, destination = lobby, rng.choice(upper)
        elif r < pattern.lobby_to_upper + pattern.upper_to_lobby:
            origin, destination = rng.choice(upper), lobby
        else:
            origin, destination = rng.sample(upper, 2)
        arrivals.append(PassengerArrival(int(t), origin, destination))
    return arrivals


class PassengerStream:
    """끝없이 이어지는 포아송 도착 승객 (실시간 데모용).

    `poisson_passengers`는 기간을 정해 목록을 한 번에 만든다. 실시간 서버는 끝이 없으므로
    필요한 만큼씩 다음 승객을 꺼낸다. 패턴을 하나 주거나, 여러 패턴을 cycle_ms마다 돌아가며 쓴다.
    """

    def __init__(
        self,
        building: Building,
        patterns: list[TrafficPattern],
        per_minute: float,
        seed: int,
        cycle_ms: int = 10 * 60_000,
    ) -> None:
        if building.num_floors < 3:
            raise ValueError("층간 이동을 만들려면 3개 층 이상이어야 합니다.")
        if per_minute <= 0 or not patterns:
            raise ValueError("per_minute는 양수, patterns는 하나 이상이어야 합니다.")
        self.building = building
        self.patterns = patterns
        self.cycle_ms = cycle_ms
        self._rng = random.Random(seed)
        self._rate_per_ms = per_minute / 60_000
        self._t = 0.0

    def pattern_at(self, time_ms: int) -> TrafficPattern:
        return self.patterns[(time_ms // self.cycle_ms) % len(self.patterns)]

    def next_arrival(self) -> PassengerArrival:
        self._t += self._rng.expovariate(self._rate_per_ms)
        time_ms = int(self._t)
        pattern = self.pattern_at(time_ms)
        lobby = self.building.bottom
        upper = range(lobby + 1, self.building.top + 1)
        r = self._rng.random()
        if r < pattern.lobby_to_upper:
            origin, destination = lobby, self._rng.choice(upper)
        elif r < pattern.lobby_to_upper + pattern.upper_to_lobby:
            origin, destination = self._rng.choice(upper), lobby
        else:
            origin, destination = self._rng.sample(upper, 2)
        return PassengerArrival(time_ms, origin, destination)
