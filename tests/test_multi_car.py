"""여러 대 시뮬레이션 테스트: 홀 호출 배차, 카 호출, 전략 교체, 결정성."""

from collections.abc import Sequence

import pytest

from app.simulation import (
    Assignment,
    Building,
    CarCall,
    Direction,
    DispatchStrategy,
    Elevator,
    ElevatorState,
    HallCall,
    ScheduledCall,
    Simulation,
)
from app.simulation.traffic import random_hall_calls

UP, DOWN = Direction.UP, Direction.DOWN


@pytest.fixture
def building() -> Building:
    return Building(num_floors=10)


class AlwaysLastCar(DispatchStrategy):
    """테스트용 전략: 항상 마지막 카를 고른다."""

    name = "always-last"

    def select(self, call: HallCall, elevators: Sequence[Elevator]) -> int:
        return len(elevators) - 1


class BrokenStrategy(DispatchStrategy):
    name = "broken"

    def select(self, call: HallCall, elevators: Sequence[Elevator]) -> int:
        return len(elevators)  # 없는 카


# ---------------------------------------------------------------- 홀 호출 배차


def test_hall_call_goes_to_exactly_one_car(building):
    sim = Simulation(building, num_elevators=2, start_floors=[1, 10])
    assert sim.press_hall_button(9, DOWN) == 1
    assert sim.elevators[1].hall_calls == {HallCall(9, DOWN)}
    assert sim.elevators[0].is_idle  # 다른 카는 움직이지 않는다


def test_assignment_is_recorded_with_time_and_car(building):
    sim = Simulation(building, num_elevators=2, start_floors=[1, 10])
    sim.step(5000)
    sim.press_hall_button(2, UP)
    assert sim.assignments == [Assignment(5000, HallCall(2, UP), 0)]


def test_pending_hall_call_is_not_dispatched_twice(building):
    sim = Simulation(building, num_elevators=3, start_floors=[1, 5, 10])
    first = sim.press_hall_button(7, DOWN)
    sim.step(1000)
    assert sim.press_hall_button(7, DOWN) == first
    assert len(sim.assignments) == 1


def test_each_car_runs_independently(building):
    sim = Simulation(building, num_elevators=2, start_floors=[1, 10])
    sim.press_hall_button(3, UP)  # 0번(1층)이 가깝다
    sim.press_hall_button(8, DOWN)  # 1번(10층)이 가깝다
    assert sim.elevators[0].state is ElevatorState.MOVING_UP
    assert sim.elevators[1].state is ElevatorState.MOVING_DOWN
    sim.run_until(60_000)
    assert sim.is_idle
    assert sim.elevators[0].floor == 3
    assert sim.elevators[1].floor == 8


def test_invalid_hall_call_is_rejected_before_dispatch(building):
    sim = Simulation(building, num_elevators=2)
    with pytest.raises(ValueError):
        sim.press_hall_button(10, UP)
    assert sim.assignments == []


# ---------------------------------------------------------------- 카 호출


def test_car_call_goes_to_that_car_only(building):
    sim = Simulation(building, num_elevators=2)
    sim.press_car_button(1, 7)
    assert sim.elevators[1].car_calls == {7}
    assert sim.elevators[0].is_idle


def test_unknown_car_is_rejected(building):
    sim = Simulation(building, num_elevators=2)
    with pytest.raises(ValueError):
        sim.press_car_button(2, 5)
    with pytest.raises(ValueError):
        sim.schedule(ScheduledCall(100, CarCall(5), car=2))


def test_scheduled_car_call_uses_car_number(building):
    sim = Simulation(building, num_elevators=2)
    sim.schedule(ScheduledCall(100, CarCall(4), car=1))
    sim.step(100)
    assert sim.elevators[1].state is ElevatorState.MOVING_UP
    assert sim.elevators[0].is_idle


# ---------------------------------------------------------------- 전략 교체 (Strategy 패턴)


def test_strategy_can_be_swapped_without_changing_simulation(building):
    sim = Simulation(building, num_elevators=3, strategy=AlwaysLastCar())
    for floor in (2, 5, 8):
        sim.press_hall_button(floor, UP)
    assert {a.car for a in sim.assignments} == {2}


def test_strategy_returning_unknown_car_is_rejected(building):
    sim = Simulation(building, num_elevators=2, strategy=BrokenStrategy())
    with pytest.raises(ValueError):
        sim.press_hall_button(5, UP)


def test_default_strategy_is_nearest_car(building):
    assert Simulation(building).strategy.name == "nearest-car"


# ---------------------------------------------------------------- 생성 검증


def test_needs_at_least_one_car(building):
    with pytest.raises(ValueError):
        Simulation(building, num_elevators=0)


def test_start_floors_must_match_car_count(building):
    with pytest.raises(ValueError):
        Simulation(building, num_elevators=2, start_floors=[1])


def test_all_cars_start_at_bottom_by_default(building):
    sim = Simulation(building, num_elevators=3)
    assert [car.floor for car in sim.elevators] == [1, 1, 1]


# ---------------------------------------------------------------- 결정성


def _run(seed: int, dt_ms: int = 100) -> Simulation:
    building = Building(num_floors=15)
    sim = Simulation(building, num_elevators=3)
    sim.schedule_all(random_hall_calls(building, count=60, duration_ms=300_000, seed=seed))
    sim.run_until(1_200_000, dt_ms=dt_ms)
    return sim


def _snapshot(sim: Simulation) -> tuple:
    return (sim.assignments, [car.history for car in sim.elevators])


def test_same_seed_gives_identical_multi_car_run():
    assert _snapshot(_run(seed=11)) == _snapshot(_run(seed=11))


def test_multi_car_result_does_not_depend_on_tick_size():
    assert _snapshot(_run(seed=5, dt_ms=1)) == _snapshot(_run(seed=5, dt_ms=500))


@pytest.mark.parametrize("seed", range(10))
def test_all_calls_are_served_with_multiple_cars(seed):
    sim = _run(seed)
    assert sim.is_idle
    assert len(sim.assignments) > 0
    # 호출이 한 카에 몰리지 않고 나뉘어 배차된다
    assert len({a.car for a in sim.assignments}) > 1
