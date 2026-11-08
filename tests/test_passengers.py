"""승객 모델 테스트: 타고 내리기, 정원, 방향, 호출 취소, 목적층 예약, 결정성."""

from collections.abc import Sequence

import pytest

from app.simulation import (
    STRATEGIES,
    Building,
    CallRequest,
    Direction,
    DispatchStrategy,
    Elevator,
    ElevatorConfig,
    ElevatorState,
    PassengerArrival,
    Simulation,
)
from app.simulation.traffic import PATTERNS, poisson_passengers
from tests.helpers import DOOR, DWELL, TRAVEL, stops

UP = Direction.UP


@pytest.fixture
def building() -> Building:
    return Building(num_floors=10)


class FixedCar(DispatchStrategy):
    """테스트용: 항상 정해진 카에 맡긴다."""

    name = "fixed"

    def __init__(self, car: int, uses_destination: bool = False) -> None:
        self.car = car
        self.uses_destination = uses_destination

    def select(self, request, elevators: Sequence[Elevator], context=None) -> int:
        return self.car


def run_passengers(sim: Simulation, *arrivals: PassengerArrival) -> None:
    sim.schedule_all(list(arrivals))
    sim.run_until_idle(dt_ms=500)


# ---------------------------------------------------------------- 한 승객의 여정


def test_single_passenger_times(building):
    sim = Simulation(building)
    run_passengers(sim, PassengerArrival(0, 1, 5))
    p = sim.passengers[0]
    assert p.car == 0
    assert p.wait_ms == DOOR  # 1층에 서 있던 카가 문을 여는 시간
    # 문 열림(1) + 대기(3) + 닫힘(1) + 4개 층 이동(8) + 문 열림(1) = 14초
    assert p.journey_ms == DOOR + DWELL + DOOR + 4 * TRAVEL + DOOR
    assert sim.is_idle


def test_passenger_arriving_while_doors_open_boards_immediately(building):
    sim = Simulation(building)
    sim.schedule(PassengerArrival(0, 1, 5))
    sim.step(DOOR + 1000)  # 문이 열린 지 1초
    sim.schedule(PassengerArrival(sim.now_ms, 1, 7))
    sim.step(0)
    late = sim.passengers[1]
    assert late.wait_ms == 0
    assert late.car == 0


def test_does_not_board_car_going_the_other_way(building):
    sim = Simulation(building, start_floors=[1])
    sim.press_car_button(0, 5)
    sim.press_car_button(0, 8)
    # 5층에서 내려가려는 승객: 올라가는 카가 5층에 서도 타지 않는다
    run_passengers(sim, PassengerArrival(0, 5, 2))
    p = sim.passengers[0]
    assert p.board_ms > 3 * (2 * TRAVEL)  # 8층에 갔다가 돌아온 뒤에 탄다
    assert stops(sim.elevators[0]) == [5, 8, 5, 2]


def test_capacity_leaves_extra_passenger_for_next_trip(building):
    config = ElevatorConfig(capacity=2)
    sim = Simulation(building, config=config)
    run_passengers(sim, *(PassengerArrival(0, 1, 6) for _ in range(3)))
    first, second, third = sim.passengers
    assert first.board_ms == second.board_ms == DOOR
    assert third.board_ms > first.alight_ms  # 정원 초과로 다음 운행에 탄다
    assert all(p.alight_ms is not None for p in sim.passengers)


# ---------------------------------------------------------------- 홀 호출 취소 (일반 방식)


def test_other_car_answering_cancels_assigned_hall_call(building):
    # 승객 호출은 0번 카에 맡겼지만, 위로 가던 1번 카가 먼저 그 층에 서서 태운다.
    sim = Simulation(building, num_elevators=2, strategy=FixedCar(0), start_floors=[1, 2])
    sim.press_car_button(1, 5)
    sim.press_car_button(1, 8)
    run_passengers(sim, PassengerArrival(0, 5, 9))
    assert sim.passengers[0].car == 1
    assert 5 not in stops(sim.elevators[0])  # 0번은 헛걸음하지 않고 중간에 멈춘다
    assert sim.elevators[0].history[-1].state is ElevatorState.IDLE


def test_cancelled_call_stops_moving_car_at_next_floor(building):
    car = Elevator(building)
    car.assign_hall_call(6, Direction.DOWN)
    car.tick(TRAVEL + 500)  # 2층을 지나 3층으로 가는 중
    car.cancel_hall_call(car_call := next(iter(car.hall_calls)))
    assert car_call.floor == 6
    car.tick(TRAVEL)
    assert car.state is ElevatorState.IDLE
    assert car.floor == 3


# ---------------------------------------------------------------- 목적층 예약 방식


def test_destination_passenger_boards_only_assigned_car(building):
    strategy = FixedCar(1, uses_destination=True)
    sim = Simulation(building, num_elevators=2, strategy=strategy, start_floors=[2, 1])
    sim.press_car_button(0, 5)
    sim.press_car_button(0, 8)
    run_passengers(sim, PassengerArrival(0, 5, 9))
    p = sim.passengers[0]
    assert 5 in stops(sim.elevators[0])  # 0번이 먼저 5층에 섰지만
    assert p.car == 1  # 배정된 1번을 기다려 탄다


def test_destination_dispatch_groups_same_destination(building):
    sim = Simulation(building, num_elevators=2, strategy=STRATEGIES["destination"]())
    sim.schedule_all(
        [PassengerArrival(0, 1, 9), PassengerArrival(0, 1, 9), PassengerArrival(0, 1, 4)]
    )
    sim.step(0)
    to9a, to9b, to4 = sim.passengers
    assert to9a.assigned_car == to9b.assigned_car == 0  # 같은 목적층은 한 카에
    assert to4.assigned_car == 1  # 정지를 늘리지 않도록 다른 카에
    assert sim.destinations(0) == {9}
    assert sim.load(0) == 2


def test_destination_dispatch_skips_full_car(building):
    config = ElevatorConfig(capacity=1)
    sim = Simulation(building, num_elevators=2, strategy=STRATEGIES["destination"](), config=config)
    sim.schedule_all([PassengerArrival(0, 1, 9), PassengerArrival(0, 1, 9)])
    sim.step(0)
    assert [p.assigned_car for p in sim.passengers] == [0, 1]


def test_destination_strategy_without_destination_acts_like_eta(building):
    cars = [Elevator(building, start_floor=1), Elevator(building, start_floor=8)]
    strategy = STRATEGIES["destination"]()
    assert strategy.select(CallRequest(7, UP), cars) == 1


# ---------------------------------------------------------------- 결정성과 완료


def _poisson_run(strategy: str, pattern: str, dt_ms: int, seed: int = 3) -> Simulation:
    building = Building(num_floors=12)
    sim = Simulation(building, num_elevators=3, strategy=STRATEGIES[strategy]())
    sim.schedule_all(poisson_passengers(building, PATTERNS[pattern], 15, 10 * 60_000, seed))
    sim.run_until_idle(dt_ms=dt_ms)
    return sim


def _record(sim: Simulation) -> tuple:
    return (
        [(p.board_ms, p.alight_ms, p.car) for p in sim.passengers],
        [car.history for car in sim.elevators],
    )


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
def test_passenger_run_does_not_depend_on_tick_size(strategy):
    assert _record(_poisson_run(strategy, "lunch", 100)) == _record(
        _poisson_run(strategy, "lunch", 60_000)
    )


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
@pytest.mark.parametrize("pattern", sorted(PATTERNS))
def test_every_passenger_reaches_destination(strategy, pattern):
    sim = _poisson_run(strategy, pattern, 60_000)
    assert sim.passengers
    for p in sim.passengers:
        assert p.alight_ms is not None
        assert p.arrival_ms <= p.board_ms <= p.alight_ms
        if sim.strategy.uses_destination:
            assert p.car == p.assigned_car


def test_invalid_passenger_is_rejected(building):
    sim = Simulation(building)
    sim.schedule(PassengerArrival(0, 3, 3))
    with pytest.raises(ValueError):
        sim.step(0)
