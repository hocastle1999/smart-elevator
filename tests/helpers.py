"""테스트 공용 도우미."""

from app.simulation import Elevator, ElevatorState

# 기본 설정: 한 층 이동 2000ms, 문 열림/닫힘 1000ms, 열린 채 대기 3000ms
TRAVEL = 2000
DOOR = 1000
DWELL = 3000


def stops(elevator: Elevator) -> list[int]:
    """문을 열기 시작한 층을 순서대로 돌려준다 (= 정지한 층)."""
    return [s.floor for s in elevator.history if s.state is ElevatorState.DOORS_OPENING]


def run_until_idle(elevator: Elevator, step_ms: int = 100, limit_ms: int = 10_000_000) -> None:
    """모든 호출을 처리하고 대기 상태가 될 때까지 진행한다."""
    start = elevator.now_ms
    while not elevator.is_idle:
        elevator.tick(step_ms)
        assert elevator.now_ms - start < limit_ms, "호출이 끝나지 않음 (무한 대기?)"
