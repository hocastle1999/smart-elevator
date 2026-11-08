"""엘리베이터 동작 시간 설정."""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ElevatorConfig:
    """모든 시간은 정수 밀리초(ms)다.

    float 초를 쓰면 0.1초를 10번 더해도 1.0이 되지 않는 부동소수점 오차 때문에
    "도착했는가"를 비교하는 순간 한 tick씩 어긋날 수 있다. 정수는 그런 오차가 없다.
    """

    floor_travel_ms: int = 2000  # 한 층 이동하는 데 걸리는 시간
    door_transition_ms: int = 1000  # 문이 열리는(닫히는) 데 걸리는 시간
    door_dwell_ms: int = 3000  # 문이 완전히 열린 채 머무는 시간

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            # 0이면 한 tick 안에서 상태가 끝없이 바뀔 수 있으므로 양수만 허용한다.
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{f.name}은 양의 정수(ms)여야 합니다: {value!r}")
