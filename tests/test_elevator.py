"""단일 엘리베이터 상태 머신 테스트.

시간 계산 기준 (기본 설정): 한 층 이동 2000ms, 문 열림 1000ms, 열린 채 3000ms, 닫힘 1000ms.
"""

import pytest

from app.simulation import Building, Direction, Elevator, ElevatorConfig, ElevatorState
from tests.helpers import DOOR, DWELL, TRAVEL, run_until_idle, stops

S = ElevatorState


def make(building: Building, start: int = 1) -> Elevator:
    return Elevator(building, start_floor=start)


# ---------------------------------------------------------------- 대기와 출발


def test_starts_idle_with_doors_closed(building):
    e = make(building)
    assert e.state is S.IDLE
    assert e.floor == 1
    assert e.direction is Direction.NONE


def test_idle_stays_idle_without_calls(building):
    e = make(building)
    e.tick(60_000)
    assert e.state is S.IDLE
    assert e.now_ms == 60_000
    assert len(e.history) == 1  # 처음 IDLE 기록 하나뿐


def test_call_while_idle_departs_immediately(building):
    e = make(building)
    e.press_car_button(3)
    assert e.state is S.MOVING_UP
    assert e.direction is Direction.UP


def test_hall_call_below_while_idle_moves_down(building):
    e = make(building, start=7)
    e.assign_hall_call(2, Direction.UP)
    assert e.state is S.MOVING_DOWN
    assert e.direction is Direction.DOWN


def test_arrives_after_travel_time_per_floor(building):
    e = make(building)
    e.press_car_button(3)
    e.tick(2 * TRAVEL - 1)
    assert e.state is S.MOVING_UP  # 1ms 모자람
    e.tick(1)
    assert e.state is S.DOORS_OPENING
    assert e.floor == 3


def test_full_door_cycle_then_idle(building):
    e = make(building)
    e.press_car_button(2)
    e.tick(TRAVEL)
    assert e.state is S.DOORS_OPENING
    e.tick(DOOR)
    assert e.state is S.DOORS_OPEN
    e.tick(DWELL)
    assert e.state is S.DOORS_CLOSING
    e.tick(DOOR)
    assert e.state is S.IDLE
    assert e.direction is Direction.NONE
    assert not e.car_calls


def test_call_at_current_floor_while_idle_opens_doors(building):
    e = make(building, start=4)
    e.assign_hall_call(4, Direction.DOWN)
    assert e.state is S.DOORS_OPENING
    assert e.direction is Direction.DOWN
    assert not e.hall_calls  # 문을 열면서 처리됨
    assert len(e.history) == 2  # IDLE -> DOORS_OPENING, 이동 없음


def test_position_is_fractional_while_moving(building):
    e = make(building)
    e.press_car_button(5)
    e.tick(TRAVEL + TRAVEL // 2)  # 2층을 지나 3층으로 가는 중간
    assert e.position == pytest.approx(2.5)
    assert e.floor == 2


def test_history_records_time_of_each_transition(building):
    e = make(building)
    e.press_car_button(2)
    run_until_idle(e)
    assert [(s.time_ms, s.state) for s in e.history] == [
        (0, S.IDLE),
        (0, S.MOVING_UP),
        (TRAVEL, S.DOORS_OPENING),
        (TRAVEL + DOOR, S.DOORS_OPEN),
        (TRAVEL + DOOR + DWELL, S.DOORS_CLOSING),
        (TRAVEL + 2 * DOOR + DWELL, S.IDLE),
    ]


# ---------------------------------------------------------------- LOOK: 진행 방향 우선


def test_stops_for_same_direction_hall_call_on_the_way(building):
    e = make(building)
    e.press_car_button(6)
    e.tick(1000)  # 1층과 2층 사이
    e.assign_hall_call(4, Direction.UP)
    run_until_idle(e)
    assert stops(e) == [4, 6]


def test_passes_opposite_direction_hall_call_and_serves_it_later(building):
    e = make(building)
    e.press_car_button(6)
    e.assign_hall_call(4, Direction.DOWN)
    e.tick(3 * TRAVEL)  # 4층 도착 시각
    assert e.floor == 4
    assert e.state is S.MOVING_UP  # 내려가는 손님은 태우지 않고 지나간다
    run_until_idle(e)
    assert stops(e) == [6, 4]


def test_opposite_hall_call_at_farthest_floor_is_turnaround_point(building):
    e = make(building)
    e.assign_hall_call(5, Direction.DOWN)
    e.tick(4 * TRAVEL)
    assert e.state is S.DOORS_OPENING
    assert e.floor == 5
    assert e.direction is Direction.DOWN  # 여기서 방향이 바뀐다
    assert not e.hall_calls


def test_does_not_stop_at_floors_without_calls(building):
    e = make(building)
    e.press_car_button(5)
    run_until_idle(e)
    assert stops(e) == [5]
    moving = [s for s in e.history if s.state is S.MOVING_UP]
    assert len(moving) == 1  # 중간 층에서 멈췄다 다시 출발하지 않는다


def test_reverses_after_finishing_calls_ahead(building):
    e = make(building, start=5)
    e.press_car_button(8)
    e.press_car_button(2)  # 반대쪽 호출은 위쪽을 다 끝낸 뒤
    run_until_idle(e)
    assert stops(e) == [8, 2]


def test_call_behind_while_moving_waits_for_reversal(building):
    e = make(building)
    e.press_car_button(8)
    e.tick(2 * TRAVEL + 500)  # 3층을 떠나 4층으로 가는 중
    e.assign_hall_call(3, Direction.UP)  # 이미 지나친 층, 같은 방향
    run_until_idle(e)
    assert stops(e) == [8, 3]


def test_look_turns_at_last_call_not_at_top_floor(building):
    """SCAN이라면 맨 위층(10층)까지 갔다 내려오지만, LOOK은 마지막 호출(6층)에서 돈다."""
    e = make(building)
    e.press_car_button(6)
    e.press_car_button(2)
    e.tick(TRAVEL)  # 2층 정지
    e.press_car_button(3)
    run_until_idle(e)
    assert max(s.floor for s in e.history) == 6
    assert stops(e) == [2, 3, 6]


def test_idle_picks_nearest_call_first(building):
    e = make(building, start=5)
    e.press_car_button(5)  # 문 열기 (다른 호출이 없어 방향 없음)
    assert e.direction is Direction.NONE
    e.press_car_button(8)
    e.press_car_button(4)  # 8층(3개 층)보다 4층(1개 층)이 가깝다
    run_until_idle(e)
    assert stops(e) == [5, 4, 8]


# ---------------------------------------------------------------- 맨 위층, 맨 아래층


def test_hall_call_up_at_top_floor_is_rejected(building):
    e = make(building)
    with pytest.raises(ValueError):
        e.assign_hall_call(10, Direction.UP)


def test_hall_call_down_at_bottom_floor_is_rejected(building):
    e = make(building)
    with pytest.raises(ValueError):
        e.assign_hall_call(1, Direction.DOWN)


@pytest.mark.parametrize("floor", [0, 11, -1])
def test_out_of_range_floor_is_rejected(building, floor):
    e = make(building)
    with pytest.raises(ValueError):
        e.press_car_button(floor)


def test_rejected_call_does_not_change_state(building):
    e = make(building)
    with pytest.raises(ValueError):
        e.assign_hall_call(10, Direction.UP)
    assert e.state is S.IDLE
    assert not e.hall_calls


def test_travels_to_top_floor_and_comes_back(building):
    e = make(building)
    e.assign_hall_call(10, Direction.DOWN)
    run_until_idle(e)
    assert stops(e) == [10]
    e.press_car_button(1)
    run_until_idle(e)
    assert stops(e) == [10, 1]
    assert all(1 <= s.floor <= 10 for s in e.history)


def test_at_top_floor_with_calls_below_moves_down(building):
    e = make(building, start=10)
    e.press_car_button(9)
    assert e.state is S.MOVING_DOWN


# ---------------------------------------------------------------- 문이 열린 동안 들어온 호출


def _open_at(building: Building, floor: int, direction: Direction) -> Elevator:
    """floor층에서 direction 방향 손님을 위해 문이 완전히 열린 상태를 만든다."""
    e = make(building, start=floor)
    e.assign_hall_call(floor, direction)
    e.tick(DOOR)
    assert e.state is S.DOORS_OPEN
    return e


def test_same_floor_same_direction_call_while_open_extends_dwell(building):
    e = _open_at(building, 3, Direction.UP)
    e.tick(2000)
    e.assign_hall_call(3, Direction.UP)
    assert not e.hall_calls  # 등록하지 않고 바로 처리
    e.tick(DWELL - 1)
    assert e.state is S.DOORS_OPEN  # 대기 시간이 처음부터 다시 시작됨
    e.tick(1)
    assert e.state is S.DOORS_CLOSING


def test_car_call_for_current_floor_while_open_is_absorbed(building):
    e = _open_at(building, 3, Direction.UP)
    e.press_car_button(3)
    assert not e.car_calls


def test_opposite_call_at_same_floor_while_open_waits(building):
    e = _open_at(building, 5, Direction.UP)
    e.press_car_button(8)  # 탄 손님이 8층을 누름
    e.assign_hall_call(5, Direction.DOWN)  # 내려갈 손님은 이 카를 타면 안 된다
    assert e.hall_calls  # 남아 있음
    run_until_idle(e)
    assert stops(e) == [5, 8, 5]


def test_call_for_other_floor_while_open_is_served_after_closing(building):
    e = _open_at(building, 3, Direction.UP)
    e.press_car_button(7)
    assert e.state is S.DOORS_OPEN  # 문이 열린 채로 출발하지 않는다
    e.tick(DWELL + DOOR)
    assert e.state is S.MOVING_UP
    run_until_idle(e)
    assert stops(e) == [3, 7]


def test_same_floor_call_while_closing_reopens_doors(building):
    e = _open_at(building, 3, Direction.UP)
    e.tick(DWELL + 400)  # 닫히기 시작한 지 400ms
    assert e.state is S.DOORS_CLOSING
    e.assign_hall_call(3, Direction.UP)
    assert e.state is S.DOORS_OPENING
    e.tick(399)  # 닫힌 만큼(400ms)만 다시 열면 된다
    assert e.state is S.DOORS_OPENING
    e.tick(1)
    assert e.state is S.DOORS_OPEN


def test_other_floor_call_while_closing_does_not_reopen(building):
    e = _open_at(building, 3, Direction.UP)
    e.tick(DWELL + 400)
    e.press_car_button(6)
    assert e.state is S.DOORS_CLOSING
    e.tick(DOOR - 400)
    assert e.state is S.MOVING_UP


def test_hall_call_while_open_without_direction_sets_direction(building):
    e = make(building, start=4)
    e.press_car_button(4)  # 다른 호출이 없어 방향 없이 문을 연다
    assert e.direction is Direction.NONE
    e.assign_hall_call(4, Direction.DOWN)
    assert e.direction is Direction.DOWN
    assert not e.hall_calls


# ---------------------------------------------------------------- 같은 호출 중복


def test_duplicate_car_call_is_stored_once(building):
    e = make(building)
    e.press_car_button(4)
    e.press_car_button(4)
    assert e.car_calls == {4}
    run_until_idle(e)
    assert stops(e) == [4]


def test_duplicate_hall_call_is_stored_once(building):
    e = make(building)
    e.assign_hall_call(6, Direction.DOWN)
    e.tick(TRAVEL)
    e.assign_hall_call(6, Direction.DOWN)
    assert len(e.hall_calls) == 1
    run_until_idle(e)
    assert stops(e) == [6]


def test_car_call_and_hall_call_same_floor_stop_once(building):
    e = make(building)
    e.press_car_button(5)
    e.assign_hall_call(5, Direction.UP)
    run_until_idle(e)
    assert stops(e) == [5]


# ---------------------------------------------------------------- 시간 처리


def test_large_tick_equals_many_small_ticks(building):
    def scenario(step_ms: int) -> list:
        e = make(building)
        e.press_car_button(9)
        e.press_car_button(3)
        e.assign_hall_call(6, Direction.DOWN)
        for _ in range(60_000 // step_ms):
            e.tick(step_ms)
        return e.history

    assert scenario(1) == scenario(100) == scenario(7) == scenario(60_000)


def test_negative_tick_is_rejected(building):
    with pytest.raises(ValueError):
        make(building).tick(-1)


def test_custom_config_changes_timing(building):
    config = ElevatorConfig(floor_travel_ms=500, door_transition_ms=200, door_dwell_ms=1000)
    e = Elevator(building, config)
    e.press_car_button(3)
    e.tick(1000)
    assert e.state is S.DOORS_OPENING


@pytest.mark.parametrize(
    "kwargs",
    [{"floor_travel_ms": 0}, {"door_transition_ms": -1}, {"door_dwell_ms": 1.5}],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ElevatorConfig(**kwargs)


def test_building_needs_at_least_two_floors():
    with pytest.raises(ValueError):
        Building(num_floors=1)
