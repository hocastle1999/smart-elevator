"""ETA 추정, ETA 비용 전략, 교통 패턴 생성기, 통계 계산 테스트."""

import pytest

from app.simulation import Building, CallRequest, Direction, Elevator, EtaCost
from app.simulation.dispatch import door_cycle_ms, estimate_arrival_ms
from app.simulation.passengers import Passenger, percentile, summarize
from app.simulation.traffic import (
    DOWN_PEAK,
    UP_PEAK,
    TrafficPattern,
    poisson_passengers,
)
from tests.helpers import DOOR, DWELL, TRAVEL

UP, DOWN = Direction.UP, Direction.DOWN
CYCLE = 2 * DOOR + DWELL


@pytest.fixture
def building() -> Building:
    return Building(num_floors=10)


# ---------------------------------------------------------------- ETA 추정


def test_eta_of_idle_car_is_travel_time(building):
    car = Elevator(building, start_floor=2)
    assert estimate_arrival_ms(car, 6, UP) == 4 * TRAVEL


def test_eta_counts_door_time_for_each_stop_on_the_way(building):
    car = Elevator(building, start_floor=1)
    car.press_car_button(3)
    car.press_car_button(5)
    assert door_cycle_ms(car) == CYCLE
    # 1→3 (정지) →5 (정지) →7
    assert estimate_arrival_ms(car, 7, UP) == 6 * TRAVEL + 2 * CYCLE


def test_eta_of_car_with_open_doors_at_floor_is_zero(building):
    car = Elevator(building, start_floor=4)
    car.assign_hall_call(4, UP)
    assert estimate_arrival_ms(car, 4, UP) == 0


def test_eta_of_car_moving_away_includes_round_trip(building):
    car = Elevator(building, start_floor=5)
    car.press_car_button(8)
    car.tick(TRAVEL)  # 6층에 도착 (통과)
    # 7, 8층(정지) 갔다가 4층으로: 남은 이동 + 8층 문 시간 + 8→4
    eta = estimate_arrival_ms(car, 4, DOWN)
    assert eta == 2 * TRAVEL + CYCLE + 4 * TRAVEL


def test_eta_opposite_call_at_turnaround_floor(building):
    car = Elevator(building, start_floor=1)
    car.press_car_button(6)
    # 6층 위로 할 일이 없으므로 6층에서 내려가는 손님은 6층 정지 때 태운다
    assert estimate_arrival_ms(car, 6, DOWN) == 5 * TRAVEL


def test_eta_strategy_avoids_busy_car(building):
    busy = Elevator(building, start_floor=4)  # 가깝지만 정지가 많다
    for floor in (5, 6, 7):
        busy.press_car_button(floor)
    free = Elevator(building, start_floor=1)
    # busy: 4→8 이동 4층 + 정지 3번 = 8s + 15s, free: 1→8 7층 = 14s
    assert EtaCost().select(CallRequest(8, UP), [busy, free]) == 1


# ---------------------------------------------------------------- 교통 패턴


def test_poisson_same_seed_same_passengers(building):
    a = poisson_passengers(building, UP_PEAK, 10, 600_000, seed=5)
    assert a == poisson_passengers(building, UP_PEAK, 10, 600_000, seed=5)
    assert a != poisson_passengers(building, UP_PEAK, 10, 600_000, seed=6)


def test_poisson_rate_is_close_to_requested():
    building = Building(num_floors=10)
    arrivals = poisson_passengers(building, UP_PEAK, 10, 60 * 60_000, seed=1)
    assert 540 <= len(arrivals) <= 660  # 기대값 600, 표준편차 약 24.5
    times = [a.time_ms for a in arrivals]
    assert times == sorted(times)
    assert all(isinstance(t, int) for t in times)


@pytest.mark.parametrize(("pattern", "lobby_origin"), [(UP_PEAK, True), (DOWN_PEAK, False)])
def test_pattern_shapes_origins(pattern, lobby_origin):
    building = Building(num_floors=10)
    arrivals = poisson_passengers(building, pattern, 30, 60 * 60_000, seed=2)
    share = sum(a.origin == 1 for a in arrivals) / len(arrivals)
    assert (share > 0.75) if lobby_origin else (share < 0.15)
    assert all(a.origin != a.destination for a in arrivals)


def test_pattern_ratios_must_be_valid():
    with pytest.raises(ValueError):
        TrafficPattern("bad", 0.7, 0.5)


def test_poisson_needs_three_floors():
    with pytest.raises(ValueError):
        poisson_passengers(Building(num_floors=2), UP_PEAK, 10, 1000, seed=1)


# ---------------------------------------------------------------- 통계


def test_percentile_nearest_rank():
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 95) == 95
    assert percentile(values, 50) == 50
    assert percentile([7], 95) == 7
    assert percentile([3, 1, 2], 100) == 3


def test_percentile_of_empty_is_rejected():
    with pytest.raises(ValueError):
        percentile([], 95)


def test_summarize_ignores_unfinished_passengers():
    done = Passenger(0, 0, 1, 5, board_ms=2000, alight_ms=10_000)
    riding = Passenger(1, 0, 1, 5, board_ms=4000)
    waiting = Passenger(2, 0, 1, 5)
    stats = summarize([done, riding, waiting], floors_traveled=8, stops=2)
    assert stats.passengers == 3
    assert stats.served == 1
    assert stats.mean_wait_s == 3.0  # (2 + 4) / 2
    assert stats.mean_journey_s == 10.0
