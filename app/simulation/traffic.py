"""호출 발생기. 시뮬레이션에서 무작위성은 이곳에만 있다.

전역 `random` 대신 시드로 만든 `random.Random` 객체를 쓴다. 전역 상태를 쓰면 다른 코드가
난수를 하나만 더 뽑아도 순서가 바뀌어, 같은 시드라도 결과가 달라질 수 있기 때문이다.
교통 패턴(출근, 점심, 퇴근 피크와 포아송 도착)은 4단계에서 추가한다.
"""

import random

from app.simulation.building import Building
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
