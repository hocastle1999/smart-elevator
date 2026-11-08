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

from app.server.broadcast import Broadcaster
from app.server.schemas import StateSnapshot, snapshot_of
from app.server.settings import ServerSettings
from app.simulation import Building, Simulation

logger = logging.getLogger(__name__)


class SimulationRunner:
    def __init__(self, settings: ServerSettings, broadcaster: Broadcaster) -> None:
        self.settings = settings
        self.broadcaster = broadcaster
        self.simulation = Simulation(
            Building(num_floors=settings.num_floors), num_elevators=settings.num_elevators
        )
        self.lagged_ticks = 0  # 제시간에 돌지 못한 tick 수 (서버 과부하 지표)
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> StateSnapshot:
        return snapshot_of(self.simulation, viewers=self.broadcaster.count)

    def tick_once(self) -> None:
        """시뮬레이션을 한 tick 진행하고 모든 접속자에게 새 상태를 보낸다."""
        self.simulation.step(self.settings.sim_ms_per_tick)
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

    def start(self) -> None:
        if not self.is_running:
            self._task = asyncio.create_task(self.run(), name="simulation-loop")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None
