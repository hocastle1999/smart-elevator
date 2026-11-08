"""공유 시뮬레이션 하나를 실제 시간에 맞춰 돌리는 asyncio 루프.

엔진(app/simulation)은 실제 시계를 모른다. 이 파일이 "실제 시간 tick_ms마다
sim_ms_per_tick만큼 진행"을 맡는다. 그래서 엔진은 그대로 두고 배속만 바꿀 수 있다.

동시성: REST 핸들러와 이 루프는 같은 이벤트 루프(한 스레드)에서 돈다. 이벤트 루프는 await
지점에서만 다른 작업으로 넘어가고, tick과 호출 처리 안에는 await가 없다. 그래서 tick 도중에
호출이 끼어드는 일이 없고 락이 필요 없다. 단, 핸들러를 `def`(동기)로 만들면 FastAPI가 다른
스레드에서 실행하므로 이 전제가 깨진다. 시뮬레이션을 만지는 핸들러는 반드시 `async def`다.
"""

import asyncio
import contextlib
import logging
from collections import deque

from app.db.recorder import LiveRecorder
from app.server.broadcast import Broadcaster
from app.server.schemas import StateSnapshot, snapshot_of
from app.server.settings import ServerSettings
from app.simulation import Building, Simulation
from app.simulation.monitor import Monitor, ServiceEvent
from app.simulation.passengers import PassengerArrival
from app.simulation.traffic import PATTERNS, PassengerStream

logger = logging.getLogger(__name__)


class SimulationRunner:
    def __init__(
        self,
        settings: ServerSettings,
        broadcaster: Broadcaster,
        recorder: LiveRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.broadcaster = broadcaster
        # 운행 기록 저장 (5단계). DB가 없으면 None이고, 시뮬레이션은 그대로 돈다.
        self.recorder = recorder
        self.simulation = Simulation(
            Building(num_floors=settings.num_floors), num_elevators=settings.num_elevators
        )
        # 이상 감지 (6단계): critical 알림이 나면 그 카를 자동으로 운행에서 뺀다.
        self.monitor = Monitor(self.simulation)
        # 화면에 보여 줄 최근 서비스 기록 (고장, 감지, 운행 중지 등)
        self.recent_service: deque[ServiceEvent] = deque(maxlen=20)
        # 자동 승객 (데모): 아무도 누르지 않아도 엘리베이터가 움직이게 한다.
        self.stream = self._make_stream(settings)
        self._next_arrival: PassengerArrival | None = (
            self.stream.next_arrival() if self.stream else None
        )
        self.recent_waits: deque[int] = deque(maxlen=50)  # 최근 내린 승객들의 대기 시간
        self.lagged_ticks = 0  # 제시간에 돌지 못한 tick 수 (서버 과부하 지표)
        self._task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> StateSnapshot:
        waits = self.recent_waits
        recent_wait_s = round(sum(waits) / len(waits) / 1000, 1) if waits else None
        return snapshot_of(
            self.simulation,
            viewers=self.broadcaster.count,
            service=list(self.recent_service),
            recent_wait_s=recent_wait_s,
        )

    @staticmethod
    def _make_stream(settings: ServerSettings) -> PassengerStream | None:
        if settings.auto_per_minute <= 0:
            return None
        if settings.auto_pattern == "cycle":
            patterns = [PATTERNS["up-peak"], PATTERNS["lunch"], PATTERNS["down-peak"]]
        elif settings.auto_pattern in PATTERNS:
            patterns = [PATTERNS[settings.auto_pattern]]
        else:
            raise ValueError(f"알 수 없는 교통 패턴: {settings.auto_pattern}")
        building = Building(num_floors=settings.num_floors)
        return PassengerStream(building, patterns, settings.auto_per_minute, settings.auto_seed)

    def _feed_passengers(self, until_ms: int) -> None:
        """이번 tick 안에 도착할 승객을 미리 예약한다 (정확한 시각에 들어가도록)."""
        while self._next_arrival is not None and self._next_arrival.time_ms <= until_ms:
            self.simulation.schedule(self._next_arrival)
            assert self.stream is not None
            self._next_arrival = self.stream.next_arrival()

    def tick_once(self) -> None:
        """시뮬레이션을 한 tick 진행하고 모든 접속자에게 새 상태를 보낸다."""
        self._feed_passengers(self.simulation.now_ms + self.settings.sim_ms_per_tick)
        self.simulation.step(self.settings.sim_ms_per_tick)
        finished = self.simulation.drain_finished_passengers()
        self.recent_waits.extend(p.wait_ms for p in finished if p.wait_ms is not None)
        # 오래 도는 서버라 기록을 쌓아 두지 않는다: 상태 변화는 저장 버퍼로 옮기거나(DB가 있으면)
        # 버리고, 배차 기록도 비운다. 그대로 두면 메모리가 끝없이 늘어난다.
        service = self.simulation.drain_service_log()
        self.recent_service.extend(service)
        if self.recorder is not None and self.recorder.run_id is not None:
            self.recorder.collect(self.simulation, service, finished)
        else:
            for car in self.simulation.elevators:
                car.drain_history()
        self.simulation.assignments.clear()
        # 접속자가 몇 명이든 직렬화는 한 번만 한다.
        self.broadcaster.publish(self.snapshot().model_dump_json())

    async def run(self) -> None:
        """tick_ms마다 tick_once를 부른다.

        `sleep(tick_ms)`만 반복하면 tick 처리 시간만큼 매번 늦어져 오차가 쌓인다. 그래서
        "다음 tick 예정 시각"을 정해 두고 그때까지만 잔다. 서버가 밀려 예정 시각을 넘겼으면
        밀린 tick을 몰아서 돌리지 않고 기준을 지금으로 다시 잡는다 (몰아서 돌리면 화면이 순간
        이동하고, 과부하일 때 일을 더 늘린다). 그만큼 시뮬레이션이 실제보다 느려진다.
        """
        loop = asyncio.get_running_loop()
        interval = self.settings.tick_ms / 1000
        next_at = loop.time()
        while True:
            try:
                self.tick_once()
            except Exception:
                # 엔진 버그면 매 tick 같은 오류가 날 것이므로 루프를 멈추고 /health로 드러낸다.
                logger.exception("시뮬레이션 tick 실패, 루프를 멈춥니다.")
                raise
            next_at += interval
            delay = next_at - loop.time()
            if delay < 0:
                self.lagged_ticks += 1
                next_at = loop.time()
                delay = 0
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ 운행 기록 저장

    async def start_recording(self) -> None:
        """실시간 실행을 DB에 만들고 주기적 저장을 시작한다.

        DB에 접속할 수 없으면 기록 없이 시뮬레이션만 돈다. 데모가 DB 장애로 멈추면 안 되기 때문이다.
        """
        if self.recorder is None:
            return
        try:
            await asyncio.to_thread(self.recorder.start_run, self.simulation)
        except Exception as e:
            logger.exception("운행 기록을 시작하지 못했습니다. 기록 없이 진행합니다.")
            self.recorder.last_error = repr(e)
            return
        self._flush_task = asyncio.create_task(self._flush_forever(), name="recorder-flush")

    async def _flush_forever(self) -> None:
        while True:
            await asyncio.sleep(self.settings.record_flush_ms / 1000)
            await self.flush()

    async def flush(self) -> None:
        """버퍼의 기록을 저장한다. DB 쓰기는 스레드에서 해서 이벤트 루프를 막지 않는다."""
        recorder = self.recorder
        if recorder is None or recorder.run_id is None:
            return
        rows, service = recorder.take()  # 루프 스레드에서 꺼낸다. 이후 기록은 새 버퍼로 간다.
        trips = recorder.take_trips()
        if not rows and not service and not trips:
            return
        try:
            await asyncio.to_thread(recorder.write, rows, self.simulation.now_ms, service, trips)
            recorder.last_error = None
        except Exception as e:
            # DB가 잠시 죽어도 시뮬레이션은 계속 돈다. 기록은 버퍼로 되돌려 다음에 다시 시도한다.
            logger.warning("운행 기록 저장 실패, 다음에 다시 시도합니다: %r", e)
            recorder.give_back(rows, service, trips)
            recorder.last_error = repr(e)

    # ------------------------------------------------------------------ 시작과 정지

    def start(self) -> None:
        if not self.is_running:
            self._task = asyncio.create_task(self.run(), name="simulation-loop")

    async def stop(self) -> None:
        for task in (self._task, self._flush_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = self._flush_task = None
        await self.flush()  # 남은 기록을 마지막으로 저장한다
