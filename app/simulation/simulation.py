"""시뮬레이션 실행기: 여러 대를 함께 진행하고, 호출을 배차 전략으로 나눠 주고, 승객을 태운다."""

import heapq
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.simulation.building import Building
from app.simulation.config import ElevatorConfig
from app.simulation.dispatch import CallRequest, DispatchStrategy, NearestCar
from app.simulation.elevator import Elevator, StateChange
from app.simulation.monitor import Monitor, ServiceEvent, ServiceKind, Severity
from app.simulation.passengers import Passenger, PassengerArrival, RunStats, summarize
from app.simulation.types import (
    DOOR_STATES,
    Call,
    CarCall,
    Direction,
    ElevatorState,
    Fault,
    HallCall,
)

S = ElevatorState


@dataclass(frozen=True)
class ScheduledCall:
    """time_ms 시각에 들어올 호출.

    카 호출은 특정 카 안에서 누르는 것이라 `car`(카 번호)가 필요하다. 홀 호출은 건물에서
    누르는 것이라 `car`를 무시하고 배차 전략이 카를 고른다.
    """

    time_ms: int
    call: Call
    car: int = 0


@dataclass(frozen=True)
class Assignment:
    """홀 호출(또는 승객 요청)을 어느 카에 맡겼는지의 기록."""

    time_ms: int
    call: HallCall
    car: int


Event = ScheduledCall | PassengerArrival


class _CandidateContext:
    """배차 후보(운행 중인 카)만 전략에 넘길 때, 전략이 보는 번호(후보 목록 안의 위치)를
    실제 카 번호로 바꿔 승객 정보를 찾아 준다."""

    def __init__(self, sim: "Simulation", candidates: list[int]) -> None:
        self.sim = sim
        self.candidates = candidates

    def destinations(self, car: int) -> frozenset[int]:
        return self.sim.destinations(self.candidates[car])

    def load(self, car: int) -> int:
        return self.sim.load(self.candidates[car])


class Simulation:
    def __init__(
        self,
        building: Building,
        num_elevators: int = 1,
        strategy: DispatchStrategy | None = None,
        config: ElevatorConfig | None = None,
        start_floors: Sequence[int] | None = None,
    ) -> None:
        if num_elevators < 1:
            raise ValueError("카는 1대 이상이어야 합니다.")
        if start_floors is None:
            start_floors = [building.bottom] * num_elevators
        if len(start_floors) != num_elevators:
            raise ValueError("start_floors의 개수가 카 대수와 다릅니다.")

        self.building = building
        self.config = config or ElevatorConfig()
        self.strategy = strategy or NearestCar()
        self.elevators = [Elevator(building, self.config, floor) for floor in start_floors]
        self.assignments: list[Assignment] = []
        self.now_ms = 0
        # (시각, 넣은 순서, 사건). 같은 시각이면 넣은 순서대로 처리해 결과가 항상 같다.
        self._queue: list[tuple[int, int, Event]] = []
        self._seq = 0

        # 승객 (4단계). 승객을 쓰지 않는 시뮬레이션(실시간 서버의 버튼 호출)에서는 비어 있다.
        self.passengers: list[Passenger] = []
        self._waiting: dict[int, list[Passenger]] = defaultdict(list)  # 층 -> 기다리는 승객
        self._riding: list[list[Passenger]] = [[] for _ in self.elevators]
        self._prev_state = [car.state for car in self.elevators]
        for index, car in enumerate(self.elevators):
            car.on_state_change = self._listener(index)
            car.on_floor_reached = self._floor_listener(index)

        # 고장, 감지, 점검 (6단계)
        self.monitor: Monitor | None = None  # Monitor(sim)으로 붙인다
        self.service_log: list[ServiceEvent] = []
        self._maintenance: set[int] = set()  # 점검 요청: 새 호출을 받지 않고, 일을 끝내면 운행 중지
        # 운행 중인 카가 하나도 없어 배차하지 못한 요청. 카가 복귀하면 배차한다.
        self._deferred_calls: list[HallCall] = []
        self._deferred_passengers: list[Passenger] = []

    # ------------------------------------------------------------------ 버튼 입력

    def press_hall_button(self, floor: int, direction: Direction) -> int | None:
        """건물의 홀 버튼(방향만 누름). 맡은 카의 번호를 돌려준다.

        배차는 호출이 들어오는 순간 한 번만 한다(이후 다시 배정하지 않음). 이미 어느 카가
        맡고 있는 호출이면 다시 배차하지 않고 그 카 번호를 돌려준다. 운행 중인 카가 없으면
        None을 돌려주고, 카가 복귀할 때 배차한다.
        """
        self.building.validate_hall_call(floor, direction)
        call = HallCall(floor, direction)
        for index, car in enumerate(self.elevators):
            if call in car.hall_calls:
                return index
        index = self._dispatch(CallRequest(floor, direction))
        if index is None:
            if call not in self._deferred_calls:
                self._deferred_calls.append(call)
            return None
        self.assignments.append(Assignment(self.now_ms, call, index))
        self.elevators[index].assign_hall_call(floor, direction)
        return index

    def press_car_button(self, car: int, floor: int) -> None:
        """car번 카 안에서 목적층 버튼을 누른다."""
        self._car(car).press_car_button(floor)

    def is_maintenance_requested(self, car: int) -> bool:
        return car in self._maintenance

    def is_available(self, car: int) -> bool:
        """배차 대상인가: 운행 중이고 점검 요청이 없는 카."""
        return self.elevators[car].in_service and car not in self._maintenance

    def _dispatch(self, request: CallRequest) -> int | None:
        """운행 중인 카 중에서 전략이 고른 카 번호. 후보가 없으면 None."""
        candidates = [i for i in range(len(self.elevators)) if self.is_available(i)]
        if not candidates:
            return None
        cars = [self.elevators[i] for i in candidates]
        local = self.strategy.select(request, cars, _CandidateContext(self, candidates))
        if not 0 <= local < len(cars):
            raise ValueError(f"배차 전략이 없는 카 번호를 골랐습니다: {local}")
        return candidates[local]

    def _car(self, index: int) -> Elevator:
        if not 0 <= index < len(self.elevators):
            raise ValueError(f"{index}번 카는 없습니다.")
        return self.elevators[index]

    # ------------------------------------------------------------------ 배차 전략용 정보

    def destinations(self, car: int) -> frozenset[int]:
        riding = {p.destination for p in self._riding[car]}
        assigned = {p.destination for p in self._assigned_waiting(car)}
        return frozenset(riding | assigned)

    def load(self, car: int) -> int:
        return len(self._riding[car]) + sum(1 for _ in self._assigned_waiting(car))

    def _assigned_waiting(self, car: int):
        for queue in self._waiting.values():
            for p in queue:
                if p.assigned_car == car:
                    yield p

    # ------------------------------------------------------------------ 승객

    def _arrive(self, arrival: PassengerArrival) -> None:
        self.building.validate_floor(arrival.origin)
        self.building.validate_floor(arrival.destination)
        if arrival.origin == arrival.destination:
            raise ValueError("출발층과 목적층이 같습니다.")
        p = Passenger(len(self.passengers), self.now_ms, arrival.origin, arrival.destination)
        self.passengers.append(p)
        self._waiting[p.origin].append(p)
        self._request_service(p)
        # 이미 이 층에서 문을 열고 있는 카가 있으면 바로 탄다.
        for index, car in enumerate(self.elevators):
            if car.state is S.DOORS_OPEN and car.floor == p.origin:
                self._board(index, car)

    def _request_service(self, p: Passenger) -> None:
        if self.strategy.uses_destination:
            # 목적층 예약: 승객마다 따로 배차하고, 승객은 배정된 카만 탄다.
            index = self._dispatch(CallRequest(p.origin, p.direction, p.destination))
            p.assigned_car = index
            if index is None:
                self._deferred_passengers.append(p)
                return
            call = HallCall(p.origin, p.direction)
            self.assignments.append(Assignment(self.now_ms, call, index))
            self.elevators[index].assign_hall_call(p.origin, p.direction)
        else:
            self.press_hall_button(p.origin, p.direction)

    def _listener(self, index: int):
        def on_change(car: Elevator, change: StateChange) -> None:
            # 처리 중에 같은 카의 상태가 또 바뀔 수 있으므로(예: 다시 문 열기) 먼저 기록한다.
            previous = self._prev_state[index]
            self._prev_state[index] = change.state
            if change.state is S.DOORS_OPENING:
                self._answer_hall_call(index, change)
            elif change.state is S.DOORS_OPEN:
                self._alight(index, car)
                self._board(index, car)
            elif previous is S.DOORS_CLOSING:
                self._rerequest_left_behind(change.floor)
            if self.monitor is not None:
                self.monitor.on_state_change(index, car, change, previous)
            # 점검 요청된 카는 맡은 일을 모두 끝내고 대기 상태가 되는 순간 운행에서 뺀다.
            if index in self._maintenance and car.is_idle:
                self.take_out_of_service(index, reason="MAINTENANCE")

        return on_change

    def _floor_listener(self, index: int):
        def on_floor(car: Elevator) -> None:
            if self.monitor is not None:
                self.monitor.on_floor_reached(index, car)

        return on_floor

    def _answer_hall_call(self, index: int, change: StateChange) -> None:
        """일반 방식: 이 카가 이 층의 이 방향 손님을 태우므로, 다른 카가 같은 호출로 오지 않게
        취소한다 (실제 엘리베이터도 한 대가 도착하면 홀 버튼 불이 꺼진다).

        목적층 예약 방식은 승객이 배정된 카만 타므로 취소하지 않는다.
        """
        if self.strategy.uses_destination or change.direction is Direction.NONE:
            return
        call = HallCall(change.floor, change.direction)
        for other_index, other in enumerate(self.elevators):
            if other_index != index:
                other.cancel_hall_call(call)

    def _alight(self, index: int, car: Elevator) -> None:
        riding = self._riding[index]
        for p in [p for p in riding if p.destination == car.floor]:
            riding.remove(p)
            p.alight_ms = self.now_ms

    def _board(self, index: int, car: Elevator) -> None:
        """문이 열린 카에, 이 층에서 같은 방향으로 가는 승객을 도착 순서대로 정원까지 태운다."""
        queue = self._waiting[car.floor]
        eligible = [
            p for p in queue if not self.strategy.uses_destination or p.assigned_car == index
        ]
        if not eligible:
            return
        if car.direction is Direction.NONE:
            # 방향 없이 열린 카(내릴 손님만 있던 카): 기다리던 손님이 버튼을 누른 것으로 처리한다.
            car.assign_hall_call(car.floor, eligible[0].direction)
        riding = self._riding[index]
        boarded = False
        for p in eligible:
            if len(riding) >= self.config.capacity:
                break  # 남은 손님은 카가 떠날 때 다시 부른다 (_rerequest_left_behind)
            if p.direction is not car.direction:
                continue
            queue.remove(p)
            p.car = index
            p.board_ms = self.now_ms
            riding.append(p)
            car.press_car_button(p.destination)
            boarded = True
        if boarded:
            # 이 카가 태웠으니 같은 호출로 오던 다른 카는 오지 않아도 된다.
            self._answer_hall_call(
                index, StateChange(self.now_ms, car.state, car.floor, car.direction)
            )

    def _rerequest_left_behind(self, floor: int) -> None:
        """카가 떠난 층에 남은 승객(정원 초과 등)의 호출이 아무 카에도 없으면 다시 부른다."""
        for p in list(self._waiting[floor]):
            call = HallCall(floor, p.direction)
            if self.strategy.uses_destination:
                if p.assigned_car is None:
                    continue  # 배차를 기다리는 승객 (운행 중인 카가 없음)
                if call not in self.elevators[p.assigned_car].hall_calls:
                    self._request_service(p)
            elif not any(call in car.hall_calls for car in self.elevators):
                self.press_hall_button(floor, p.direction)

    # ------------------------------------------------------------------ 고장, 운행 중지, 점검

    def log_service(
        self,
        kind: ServiceKind,
        car: int,
        detail: str = "",
        severity: Severity | None = None,
        message: str = "",
    ) -> None:
        self.service_log.append(ServiceEvent(self.now_ms, car, kind, detail, severity, message))

    def drain_service_log(self) -> list[ServiceEvent]:
        """서비스 기록을 꺼내고 비운다 (오래 도는 서버에서 메모리에 쌓이지 않게)."""
        drained, self.service_log = self.service_log, []
        return drained

    def inject_fault(self, car: int, fault: Fault) -> None:
        self._car(car).inject_fault(fault)
        self.log_service(ServiceKind.FAULT, car, fault.value)

    def repair(self, car: int) -> None:
        self._car(car).repair()
        self.log_service(ServiceKind.REPAIR, car)

    def take_out_of_service(self, car: int, reason: str) -> None:
        """카를 운행에서 빼고, 그 카가 맡던 홀 호출을 다른 카에 다시 배차한다.

        안에 탄 승객은 그대로 남는다(복귀하면 목적층으로 간다). 문을 열고 있던 층에서 기다리던
        승객은 다시 호출한다.
        """
        elevator = self._car(car)
        if not elevator.in_service:
            return
        was_at_floor = elevator.state in DOOR_STATES
        dropped = elevator.take_out_of_service()
        self.log_service(ServiceKind.OUT_OF_SERVICE, car, reason)
        if self.strategy.uses_destination:
            for p in list(self._assigned_waiting(car)):
                p.assigned_car = None
                self._request_service(p)
        else:
            for call in dropped:
                self.press_hall_button(call.floor, call.direction)
        if was_at_floor:
            self._rerequest_left_behind(elevator.floor)

    def request_maintenance(self, car: int) -> None:
        """점검 요청: 새 호출은 받지 않고, 맡은 일을 끝내면 운행에서 뺀다 (승객을 내려놓고 멈춤)."""
        elevator = self._car(car)
        if car in self._maintenance or not elevator.in_service:
            return
        self._maintenance.add(car)
        self.log_service(ServiceKind.MAINTENANCE_REQUEST, car)
        if elevator.is_idle:
            self.take_out_of_service(car, reason="MAINTENANCE")

    def return_to_service(self, car: int) -> None:
        """운행에 복귀시키고, 그동안 배차하지 못한 요청을 다시 배차한다."""
        elevator = self._car(car)
        self._maintenance.discard(car)
        if elevator.in_service:
            return
        # 감지기를 먼저 초기화한다. 복귀하면서 바로 "이동 중"으로 바뀌면 감지기가 그 순간부터
        # 다시 재기 시작하는데, 초기화를 나중에 하면 그 시작 시각을 지워 멈춤을 놓친다.
        if self.monitor is not None:
            self.monitor.reset(car)
        elevator.return_to_service()
        self.log_service(ServiceKind.RETURN, car)
        calls, self._deferred_calls = self._deferred_calls, []
        for call in calls:
            self.press_hall_button(call.floor, call.direction)
        passengers, self._deferred_passengers = self._deferred_passengers, []
        for p in passengers:
            self._request_service(p)

    # ------------------------------------------------------------------ 예약

    def schedule(self, event: Event) -> None:
        if event.time_ms < self.now_ms:
            raise ValueError("이미 지난 시각에는 예약할 수 없습니다.")
        if isinstance(event, ScheduledCall) and isinstance(event.call, CarCall):
            self._car(event.car)  # 없는 카 번호면 예약할 때 바로 알린다
        heapq.heappush(self._queue, (event.time_ms, self._seq, event))
        self._seq += 1

    def schedule_all(self, events: Sequence[Event]) -> None:
        for event in events:
            self.schedule(event)

    def _apply(self, event: Event) -> None:
        match event:
            case PassengerArrival():
                self._arrive(event)
            case ScheduledCall(call=HallCall(floor=floor, direction=direction)):
                self.press_hall_button(floor, direction)
            case ScheduledCall(call=CarCall(floor=floor), car=car):
                self.press_car_button(car, floor)

    # ------------------------------------------------------------------ 시간

    def step(self, dt_ms: int) -> None:
        """dt_ms만큼 진행한다.

        예약된 사건(호출, 승객 도착)은 tick 경계로 미루지 않고 그 시각에 정확히 넣는다.
        같은 시각에 끝나는 동작(예: 문 닫힘 완료)이 있으면 그 동작이 먼저 끝나고 사건이 들어간다.
        """
        if dt_ms < 0:
            raise ValueError("dt_ms는 0 이상이어야 합니다.")
        end = self.now_ms + dt_ms
        while self._queue and self._queue[0][0] <= end:
            time_ms, _, event = heapq.heappop(self._queue)
            self._advance_to(time_ms)
            self._apply(event)
        self._advance_to(end)

    def run_until(self, end_ms: int, dt_ms: int = 100) -> None:
        """고정 간격 dt_ms로 end_ms까지 진행한다 (실시간 루프가 하는 방식과 같다)."""
        if dt_ms <= 0:
            raise ValueError("dt_ms는 양수여야 합니다.")
        while self.now_ms < end_ms:
            self.step(min(dt_ms, end_ms - self.now_ms))

    def run_until_idle(self, limit_ms: int = 24 * 3600 * 1000, dt_ms: int = 60_000) -> None:
        """예약된 사건과 승객을 모두 처리할 때까지 진행한다 (실험용)."""
        while not self.is_idle:
            if self.now_ms >= limit_ms:
                raise RuntimeError("제한 시간 안에 모든 승객을 처리하지 못했습니다.")
            self.step(dt_ms)

    def _advance_to(self, time_ms: int) -> None:
        """모든 카를 time_ms까지 사건 단위로 함께 진행한다.

        한 카의 상태 전이(예: 승객이 탐, 홀 호출 취소)가 다른 카에 영향을 주므로, 가장 이른
        전이 시각까지 모든 카의 시계를 같이 옮긴 뒤 전이를 처리한다. 그래서 tick 크기와 무관하게
        같은 시각에 같은 일이 일어난다. 같은 시각의 전이는 카 번호 순서로 처리한다.
        """
        while True:
            pending = [c.time_to_next_event for c in self.elevators]
            due = [self.now_ms + t for t in pending if t is not None]
            if self.monitor is not None:
                # 감지 규칙의 판정 시각(예: 멈춤)도 사건이다. 정확히 그 시각에 판정해야 결정적이다.
                deadline = self.monitor.next_deadline()
                if deadline is not None:
                    due.append(max(deadline, self.now_ms))
            next_ms = min(due, default=None)
            if next_ms is None or next_ms > time_ms:
                break
            self._move_clock(next_ms - self.now_ms)
            if self.monitor is not None:
                self.monitor.on_time(self.now_ms)
        self._move_clock(time_ms - self.now_ms)

    def _move_clock(self, dt_ms: int) -> None:
        for car in self.elevators:
            car.elapse(dt_ms)
        self.now_ms += dt_ms
        for car in self.elevators:
            car.complete_due_phases()

    # ------------------------------------------------------------------ 결과

    @property
    def is_idle(self) -> bool:
        """모든 카가 대기 중이고 남은 호출, 예약, 승객이 없는가."""
        return (
            not self._queue
            and all(car.is_idle or not car.in_service for car in self.elevators)
            and not any(self._waiting.values())
            and not any(self._riding)
            and not self._deferred_calls
        )

    def stats(self) -> RunStats:
        stops = sum(1 for car in self.elevators for s in car.history if s.state is S.DOORS_OPENING)
        floors = sum(car.floors_traveled for car in self.elevators)
        return summarize(self.passengers, floors_traveled=floors, stops=stops)
