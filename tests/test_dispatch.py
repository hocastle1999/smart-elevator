"""배차 전략 테스트: Nearest Car의 적합도(FS) 계산과 카 선택.

10층 건물이라 N = 9. 대기 중 FS = N + 1 - d = 10 - d,
같은 방향으로 오는 중 FS = N + 2 - d = 11 - d, 멀어지는 중 FS = 1.
"""

import pytest

from app.simulation import (
    Building,
    CallRequest,
    Direction,
    DispatchStrategy,
    Elevator,
    ElevatorState,
    NearestCar,
)

UP, DOWN = Direction.UP, Direction.DOWN


@pytest.fixture
def building() -> Building:
    return Building(num_floors=10)


def idle_car(building: Building, floor: int) -> Elevator:
    return Elevator(building, start_floor=floor)


def moving_car(building: Building, start: int, target: int, elapsed_ms: int = 0) -> Elevator:
    """start층에서 target층을 향해 출발한 카."""
    car = Elevator(building, start_floor=start)
    car.press_car_button(target)
    car.tick(elapsed_ms)
    return car


fs = NearestCar.suitability


# ---------------------------------------------------------------- 적합도(FS)


def test_idle_car_scores_by_distance(building):
    assert fs(CallRequest(5, UP), idle_car(building, 1)) == 10 - 4


def test_approaching_car_same_direction_scores_highest(building):
    car = moving_car(building, start=1, target=9)
    assert fs(CallRequest(5, UP), car) == 11 - 4


def test_approaching_car_opposite_direction_scores_like_idle(building):
    car = moving_car(building, start=1, target=9)
    assert fs(CallRequest(5, DOWN), car) == 10 - 4


def test_car_moving_away_scores_one_regardless_of_distance(building):
    car = moving_car(building, start=5, target=9)
    assert fs(CallRequest(4, UP), car) == 1  # 1개 층 거리지만 멀어지는 중


def test_moving_car_cannot_serve_the_floor_it_just_left(building):
    car = moving_car(building, start=3, target=9, elapsed_ms=1000)  # 3층과 4층 사이
    assert car.state is ElevatorState.MOVING_UP
    assert fs(CallRequest(3, UP), car) == 1
    assert fs(CallRequest(4, UP), car) == pytest.approx(11 - 0.5)  # 다음 층은 설 수 있다


def test_distance_uses_position_between_floors(building):
    car = moving_car(building, start=1, target=9, elapsed_ms=1000)  # 1.5층
    assert fs(CallRequest(5, UP), car) == pytest.approx(11 - 3.5)


def test_car_with_doors_open_at_call_floor_scores_top(building):
    car = idle_car(building, 5)
    car.assign_hall_call(5, UP)  # 5층에서 문을 열고 위로 갈 예정
    assert car.state is ElevatorState.DOORS_OPENING
    assert fs(CallRequest(5, UP), car) == 11


# ---------------------------------------------------------------- 카 선택


def test_selects_nearest_idle_car(building):
    cars = [idle_car(building, 1), idle_car(building, 9)]
    assert NearestCar().select(CallRequest(7, DOWN), cars) == 1


def test_tie_goes_to_lowest_numbered_car(building):
    cars = [idle_car(building, 1), idle_car(building, 9)]
    assert NearestCar().select(CallRequest(5, UP), cars) == 0  # 둘 다 4개 층 거리


def test_prefers_car_coming_in_same_direction_over_idle_car(building):
    coming = moving_car(building, start=3, target=9)  # 2개 층 아래에서 올라오는 중
    idle = idle_car(building, 8)  # 3개 층 위에서 대기
    assert NearestCar().select(CallRequest(5, UP), [idle, coming]) == 1


def test_closer_idle_car_beats_far_car_coming_same_direction(building):
    coming = moving_car(building, start=1, target=9)
    idle = idle_car(building, 6)
    assert NearestCar().select(CallRequest(7, UP), [coming, idle]) == 1


def test_never_picks_car_moving_away_when_another_can_come(building):
    away = moving_car(building, start=4, target=9, elapsed_ms=1000)  # 바로 위에서 멀어지는 중
    far = idle_car(building, 10)
    assert NearestCar().select(CallRequest(3, DOWN), [away, far]) == 1


def test_select_without_cars_is_rejected():
    with pytest.raises(ValueError):
        NearestCar().select(CallRequest(2, UP), [])


def test_strategy_must_implement_select():
    class Incomplete(DispatchStrategy):
        pass

    with pytest.raises(TypeError):
        Incomplete()
