"""고장, 운행 중지, 점검, 이상 감지 테스트 (6단계)."""

from collections.abc import Sequence

import pytest

from app.simulation import (
    STRATEGIES,
    Building,
    Direction,
    DispatchStrategy,
    Elevator,
    ElevatorState,
    HallCall,
    PassengerArrival,
    Simulation,
)
from app.simulation.monitor import Monitor, MonitorConfig, Rule, ServiceKind, Severity
from app.simulation.traffic import PATTERNS, poisson_passengers
from app.simulation.types import Fault
from tests.helpers import DOOR, DWELL, TRAVEL

S = ElevatorState
UP, DOWN = Direction.UP, Direction.DOWN


class FixedCar(DispatchStrategy):
    name = "fixed"

    def __init__(self, car: int) -> None:
        self.car = car

    def select(self, request, elevators: Sequence[Elevator], context=None) -> int:
        return min(self.car, len(elevators) - 1)


def kinds(sim: Simulation) -> list[tuple[int, ServiceKind, str]]:
    return [(e.car, e.kind, e.detail) for e in sim.service_log]


# ---------------------------------------------------------------- 고장 동작 (엔진)


def test_door_sensor_fault_reopens_forever(building):
    car = Elevator(building, start_floor=3)
    car.assign_hall_call(3, UP)
    car.inject_fault(Fault.DOOR_SENSOR)
    car.tick(10 * (2 * DOOR + DWELL))
    reopens = sum(
        1
        for a, b in zip(car.history, car.history[1:], strict=False)
        if a.state is S.DOORS_CLOSING and b.state is S.DOORS_OPENING
    )
    assert reopens >= 9
    assert car.floor == 3  # 출발하지 못한다
    car.repair()
    car.tick(2 * DOOR + DWELL)
    assert car.state is S.IDLE


def test_stall_freezes_car_between_floors(building):
    car = Elevator(building)
    car.press_car_button(5)
    car.tick(TRAVEL + 500)  # 2층을 지나 3층으로 가는 중
    car.inject_fault(Fault.STALL)
    position = car.position
    car.tick(60_000)
    assert car.state is S.MOVING_UP  # 겉으로는 이동 중
    assert car.position == position  # 실제로는 멈춤
    assert car.time_to_next_event is None
    car.repair()
    car.tick(4 * TRAVEL)
    assert car.floor == 5


def test_slow_motor_doubles_travel_time(building):
    car = Elevator(building)
    car.inject_fault(Fault.SLOW_MOTOR)
    car.press_car_button(3)
    car.tick(4 * TRAVEL - 1)
    assert car.state is S.MOVING_UP
    car.tick(4 * TRAVEL)
    assert car.floor == 3
    assert car.position == 3.0


def test_out_of_service_between_floors_resumes_same_way(building):
    car = Elevator(building)
    car.press_car_button(6)
    car.tick(TRAVEL + 1000)  # 2.5층
    dropped = car.take_out_of_service()
    assert dropped == []
    assert car.state is S.OUT_OF_SERVICE
    assert car.position == pytest.approx(2.5)  # 화면에는 멈춘 위치 그대로
    car.tick(60_000)
    assert car.position == pytest.approx(2.5)
    car.return_to_service()
    assert car.state is S.MOVING_UP
    car.tick(1000)
    assert car.floor == 3


def test_out_of_service_returns_hall_calls_and_keeps_car_calls(building):
    car = Elevator(building)
    car.press_car_button(7)
    car.assign_hall_call(4, DOWN)
    dropped = car.take_out_of_service()
    assert dropped == [HallCall(4, DOWN)]
    assert car.car_calls == {7}  # 안에 탄 승객의 목적층은 남는다
    car.press_car_button(9)  # 운행 중지 중에 누른 버튼은 기억만 한다
    assert car.state is S.OUT_OF_SERVICE


def test_return_from_idle_out_of_service_departs_for_car_calls(building):
    car = Elevator(building, start_floor=2)
    car.take_out_of_service()
    car.press_car_button(5)
    car.return_to_service()
    assert car.state is S.MOVING_UP


# ---------------------------------------------------------------- 운행 중지와 재배차 (시뮬레이션)


def test_hall_calls_move_to_other_car_when_taken_out(building):
    sim = Simulation(building, num_elevators=2, start_floors=[1, 10])
    sim.press_hall_button(3, UP)  # 0번(1층)이 가깝다
    assert HallCall(3, UP) in sim.elevators[0].hall_calls
    sim.take_out_of_service(0, reason="TEST")
    assert HallCall(3, UP) in sim.elevators[1].hall_calls
    assert kinds(sim) == [(0, ServiceKind.OUT_OF_SERVICE, "TEST")]


def test_dispatch_skips_out_of_service_car(building):
    sim = Simulation(building, num_elevators=2, start_floors=[1, 10])
    sim.take_out_of_service(0, reason="TEST")
    assert sim.press_hall_button(2, UP) == 1  # 1층의 0번이 가깝지만 운행 중지


def test_no_car_available_defers_call_until_return(building):
    sim = Simulation(building, num_elevators=1)
    sim.take_out_of_service(0, reason="TEST")
    assert sim.press_hall_button(4, UP) is None
    assert not sim.is_idle  # 처리하지 못한 호출이 남아 있다
    sim.return_to_service(0)
    assert HallCall(4, UP) in sim.elevators[0].hall_calls


def test_maintenance_waits_until_car_finishes_its_work(building):
    sim = Simulation(building, num_elevators=2)
    sim.schedule(PassengerArrival(0, 1, 6))
    sim.step(0)
    rider_car = sim.passengers[0].car if sim.passengers[0].car is not None else 0
    sim.request_maintenance(rider_car)
    assert sim.elevators[rider_car].in_service  # 승객을 내려놓을 때까지 운행
    assert sim.press_hall_button(8, DOWN) != rider_car  # 새 호출은 받지 않는다
    sim.run_until_idle(dt_ms=1000)
    assert sim.passengers[0].alight_ms is not None
    assert not sim.elevators[rider_car].in_service
    assert (rider_car, ServiceKind.OUT_OF_SERVICE, "MAINTENANCE") in kinds(sim)


def test_destination_passenger_is_reassigned_when_car_taken_out(building):
    strategy = STRATEGIES["destination"]()
    sim = Simulation(building, num_elevators=2, strategy=strategy, start_floors=[5, 1])
    sim.schedule(PassengerArrival(0, 8, 2))
    sim.step(0)
    p = sim.passengers[0]
    first = p.assigned_car
    sim.take_out_of_service(first, reason="TEST")
    assert p.assigned_car == 1 - first
    sim.return_to_service(first)
    sim.run_until_idle(dt_ms=1000)
    assert p.car == 1 - first and p.alight_ms is not None


# ---------------------------------------------------------------- 이상 감지


def test_stall_is_detected_at_exact_deadline_and_isolated(building):
    sim = Simulation(building, num_elevators=2)
    Monitor(sim)
    sim.press_car_button(0, 8)
    sim.step(TRAVEL + 500)
    sim.inject_fault(0, Fault.STALL)
    sim.step(60_000)
    alerts = [e for e in sim.service_log if e.kind is ServiceKind.ALERT]
    assert [(a.car, a.detail, a.severity) for a in alerts] == [
        (0, Rule.STALLED.value, Severity.CRITICAL)
    ]
    # 2층을 지난 시각(TRAVEL)부터 정상 이동 시간의 3배가 지나면 판정
    assert alerts[0].time_ms == TRAVEL + 3 * TRAVEL
    assert not sim.elevators[0].in_service


def test_stall_detection_time_does_not_depend_on_tick_size(building):
    def alert_time(dt: int) -> int:
        sim = Simulation(building)
        Monitor(sim)
        sim.press_car_button(0, 8)
        sim.step(3000)
        sim.inject_fault(0, Fault.STALL)
        for _ in range(60_000 // dt):
            sim.step(dt)
        return next(e.time_ms for e in sim.service_log if e.kind is ServiceKind.ALERT)

    assert alert_time(7) == alert_time(100) == alert_time(60_000)


def test_door_sensor_fault_is_detected_and_calls_move(building):
    sim = Simulation(building, num_elevators=2, start_floors=[3, 10])
    Monitor(sim)
    sim.press_hall_button(3, UP)  # 0번이 3층에서 문을 연다
    sim.inject_fault(0, Fault.DOOR_SENSOR)
    sim.step(120_000)
    assert (0, ServiceKind.ALERT, Rule.DOOR_REOPEN_LOOP.value) in kinds(sim)
    assert not sim.elevators[0].in_service
    assert sim.press_hall_button(5, DOWN) == 1


def test_slow_motor_is_warning_only(building):
    sim = Simulation(building, num_elevators=2)
    Monitor(sim)
    sim.inject_fault(0, Fault.SLOW_MOTOR)
    sim.press_car_button(0, 9)
    sim.run_until_idle(dt_ms=1000)
    alerts = [e for e in sim.service_log if e.kind is ServiceKind.ALERT]
    assert [(a.detail, a.severity) for a in alerts] == [(Rule.SLOW_TRAVEL.value, Severity.WARNING)]
    assert sim.elevators[0].in_service  # 경고만 하고 운행은 계속


def test_auto_isolate_can_be_turned_off(building):
    sim = Simulation(building)
    Monitor(sim, MonitorConfig(auto_isolate=False))
    sim.press_car_button(0, 8)
    sim.step(1000)
    sim.inject_fault(0, Fault.STALL)
    sim.step(60_000)
    assert any(e.kind is ServiceKind.ALERT for e in sim.service_log)
    assert sim.elevators[0].in_service


def test_monitor_is_reset_after_return(building):
    sim = Simulation(building)
    Monitor(sim)
    sim.press_car_button(0, 8)
    sim.step(1000)
    sim.inject_fault(0, Fault.STALL)
    sim.step(30_000)
    sim.return_to_service(0)  # 고치지 않고 복귀시키면
    sim.step(30_000)
    alerts = [e for e in sim.service_log if e.kind is ServiceKind.ALERT]
    assert len(alerts) == 2  # 다시 감지한다


@pytest.mark.parametrize("pattern", ["up-peak", "lunch"])
def test_no_false_alarm_in_heavy_normal_traffic(pattern):
    """임계값 근거: 정상 운행은 가장 붐벼도 연속 재열림이 6번 이하였다 (임계값 10)."""
    building = Building(num_floors=15)
    sim = Simulation(building, num_elevators=4, strategy=STRATEGIES["nearest-car"]())
    Monitor(sim)
    sim.schedule_all(poisson_passengers(building, PATTERNS[pattern], 30, 30 * 60_000, seed=0))
    sim.run_until_idle()
    assert [e for e in sim.service_log if e.kind is ServiceKind.ALERT] == []
