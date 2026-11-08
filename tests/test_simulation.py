"""시뮬레이션 실행기: 결정성(같은 입력 → 같은 결과)과 호출 예약 테스트."""

import pytest

from app.simulation import (
    Building,
    CarCall,
    Direction,
    ElevatorState,
    HallCall,
    ScheduledCall,
    Simulation,
)
from app.simulation.traffic import random_hall_calls


def _run(seed: int, dt_ms: int = 100, num_floors: int = 15) -> Simulation:
    building = Building(num_floors=num_floors)
    sim = Simulation(building)
    sim.schedule_all(random_hall_calls(building, count=40, duration_ms=300_000, seed=seed))
    sim.run_until(1_200_000, dt_ms=dt_ms)
    return sim


def test_same_seed_gives_same_calls():
    building = Building(num_floors=15)
    a = random_hall_calls(building, count=50, duration_ms=60_000, seed=42)
    b = random_hall_calls(building, count=50, duration_ms=60_000, seed=42)
    assert a == b


def test_different_seed_gives_different_calls():
    building = Building(num_floors=15)
    a = random_hall_calls(building, count=50, duration_ms=60_000, seed=1)
    b = random_hall_calls(building, count=50, duration_ms=60_000, seed=2)
    assert a != b


def test_random_calls_are_valid_and_sorted():
    building = Building(num_floors=5)
    calls = random_hall_calls(building, count=200, duration_ms=10_000, seed=7)
    assert [c.time_ms for c in calls] == sorted(c.time_ms for c in calls)
    for c in calls:
        building.validate_hall_call(c.call.floor, c.call.direction)  # 예외가 없어야 한다


def test_same_seed_gives_identical_run():
    assert _run(seed=42).elevators[0].history == _run(seed=42).elevators[0].history


def test_result_does_not_depend_on_tick_size():
    assert (
        _run(seed=3, dt_ms=1).elevators[0].history == _run(seed=3, dt_ms=250).elevators[0].history
    )


@pytest.mark.parametrize("seed", range(20))
def test_all_calls_are_eventually_served(seed):
    """어떤 호출도 영원히 남지 않는다 (LOOK은 한쪽 끝까지 가면 반드시 돌아온다)."""
    sim = _run(seed)
    assert sim.is_idle


@pytest.mark.parametrize("seed", range(5))
def test_invariants_hold_at_every_tick(seed):
    building = Building(num_floors=8)
    sim = Simulation(building)
    sim.schedule_all(random_hall_calls(building, count=30, duration_ms=120_000, seed=seed))
    for _ in range(4000):
        sim.step(50)
        e = sim.elevators[0]
        assert building.bottom <= e.floor <= building.top
        assert building.bottom <= e.position <= building.top
        if e.state is ElevatorState.MOVING_UP:
            assert e.direction is Direction.UP
        if e.state is ElevatorState.MOVING_DOWN:
            assert e.direction is Direction.DOWN
        if e.state is ElevatorState.IDLE:
            assert e.direction is Direction.NONE


def test_scheduled_call_is_applied_at_exact_time_between_ticks():
    sim = Simulation(Building(num_floors=5))
    sim.schedule(ScheduledCall(150, CarCall(3)))
    sim.run_until(1000, dt_ms=100)
    departed = [s for s in sim.elevators[0].history if s.state is ElevatorState.MOVING_UP]
    assert departed[0].time_ms == 150  # tick 경계(200ms)로 밀리지 않는다


def test_calls_at_same_time_are_applied_in_scheduled_order():
    def first_direction(calls):
        sim = Simulation(Building(num_floors=9), start_floors=[5])
        sim.schedule_all(calls)
        sim.step(0)
        return sim.elevators[0].direction

    up = ScheduledCall(0, HallCall(8, Direction.DOWN))
    down = ScheduledCall(0, HallCall(2, Direction.UP))
    assert first_direction([up, down]) is Direction.UP
    assert first_direction([down, up]) is Direction.DOWN


def test_cannot_schedule_in_the_past():
    sim = Simulation(Building(num_floors=5))
    sim.step(1000)
    with pytest.raises(ValueError):
        sim.schedule(ScheduledCall(500, CarCall(2)))
