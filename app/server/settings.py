"""서버 설정. 환경 변수(SIM_*)로 바꿀 수 있고, 테스트는 객체를 직접 만들어 넘긴다."""

import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ServerSettings:
    num_floors: int = 10
    num_elevators: int = 3
    # 실제 시간으로 몇 ms마다 tick할지
    tick_ms: int = 100
    # tick 한 번에 시뮬레이션 시간을 몇 ms 진행할지. tick_ms와 같으면 실시간, 2배면 2배속.
    sim_ms_per_tick: int = 100
    # 공유 시뮬레이션 보호: 동시 WebSocket 접속 상한, 접속자(IP)별 호출 빈도 제한
    max_connections: int = 100
    calls_per_window: int = 10
    rate_window_ms: int = 10_000
    # 운행 기록을 DB에 묶어서 저장하는 간격 (DB_ENABLED일 때만)
    record_flush_ms: int = 5000
    # False면 시작할 때 루프를 돌리지 않는다 (테스트에서 tick을 직접 부를 때)
    run_loop: bool = True

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if not isinstance(value, bool) and value <= 0:
                raise ValueError(f"{f.name}은 양수여야 합니다: {value}")

    @classmethod
    def from_env(cls) -> "ServerSettings":
        """SIM_NUM_FLOORS, SIM_TICK_MS처럼 SIM_ + 대문자 필드 이름의 환경 변수를 읽는다."""
        values: dict[str, int | bool] = {}
        for f in fields(cls):
            raw = os.environ.get(f"SIM_{f.name.upper()}")
            if raw is None:
                continue
            values[f.name] = raw.lower() in ("1", "true", "yes") if f.type is bool else int(raw)
        return cls(**values)  # type: ignore[arg-type]
