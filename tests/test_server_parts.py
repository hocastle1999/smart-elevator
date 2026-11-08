"""실시간 서버 부품 테스트: 빈도 제한, 방송 큐, 설정, 시뮬레이션 루프의 시간 처리."""

import asyncio
import time

import pytest

from app.server.broadcast import Broadcaster, Client
from app.server.rate_limit import RateLimiter, client_ip
from app.server.runner import SimulationRunner
from app.server.settings import ServerSettings
from app.simulation import Direction


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------- 빈도 제한


def test_rate_limiter_allows_up_to_limit_then_rejects():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_s=10, clock=clock)
    assert [limiter.check("a") for _ in range(3)] == [0, 0, 0]
    clock.now += 4
    assert limiter.check("a") == pytest.approx(6)  # 첫 요청이 윈도를 벗어나기까지 6초


def test_rate_limiter_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_s=10, clock=clock)
    limiter.check("a")
    clock.now += 5
    limiter.check("a")
    clock.now += 5  # 첫 요청이 정확히 10초 전 -> 윈도 밖
    assert limiter.check("a") == 0
    assert limiter.check("a") > 0  # 두 번째 요청(5초 전)은 아직 윈도 안


def test_rate_limiter_counts_each_client_separately():
    limiter = RateLimiter(limit=1, window_s=10, clock=FakeClock())
    assert limiter.check("a") == 0
    assert limiter.check("b") == 0
    assert limiter.check("a") > 0


def test_rate_limiter_forgets_idle_clients():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_s=1, clock=clock)
    for i in range(1000):
        limiter.check(f"ip{i}")
    clock.now += 2
    limiter.check("new")
    assert len(limiter._hits) == 1


# ---------------------------------------------------------------- 방송 큐


def test_slow_client_keeps_only_latest_message():
    client = Client(websocket=None)  # type: ignore[arg-type]
    for message in ("s1", "s2", "s3"):
        client.offer(message)
    assert client.queue.qsize() == 1
    assert client.queue.get_nowait() == "s3"
    assert client.dropped == 2


def test_broadcaster_rejects_over_capacity():
    broadcaster = Broadcaster(max_connections=2)
    a = broadcaster.add(object())  # type: ignore[arg-type]
    broadcaster.add(object())  # type: ignore[arg-type]
    assert broadcaster.add(object()) is None  # type: ignore[arg-type]
    broadcaster.remove(a)
    assert broadcaster.add(object()) is not None  # type: ignore[arg-type]


def test_publish_reaches_every_client():
    broadcaster = Broadcaster(max_connections=10)
    clients = [broadcaster.add(object()) for _ in range(3)]  # type: ignore[arg-type]
    broadcaster.publish("hello")
    assert [c.queue.get_nowait() for c in clients] == ["hello"] * 3


# ---------------------------------------------------------------- 설정


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("SIM_NUM_FLOORS", "20")
    monkeypatch.setenv("SIM_RUN_LOOP", "false")
    settings = ServerSettings.from_env()
    assert settings.num_floors == 20
    assert settings.run_loop is False
    assert settings.num_elevators == ServerSettings().num_elevators


@pytest.mark.parametrize("field", ["tick_ms", "num_elevators", "calls_per_window"])
def test_settings_reject_non_positive(field):
    with pytest.raises(ValueError):
        ServerSettings(**{field: 0})


# ---------------------------------------------------------------- 시뮬레이션 루프


def test_runner_does_not_catch_up_when_lagging():
    """tick이 예정보다 늦어도 밀린 tick을 몰아서 돌리지 않는다.

    tick마다 시뮬레이션은 정확히 sim_ms_per_tick씩만 진행한다.
    """
    settings = ServerSettings(tick_ms=1, sim_ms_per_tick=100)
    runner = SimulationRunner(settings, Broadcaster(max_connections=1))
    ticks = 0
    original = runner.tick_once

    def slow_tick() -> None:
        nonlocal ticks
        ticks += 1
        original()
        time.sleep(0.005)  # 1ms 간격보다 오래 걸리는 tick

    runner.tick_once = slow_tick  # type: ignore[method-assign]

    async def scenario() -> None:
        runner.start()
        await asyncio.sleep(0.1)
        await runner.stop()

    asyncio.run(scenario())
    assert ticks > 0
    assert runner.lagged_ticks > 0
    assert runner.simulation.now_ms == ticks * 100


def test_runner_stops_cleanly_and_reports_not_running():
    runner = SimulationRunner(ServerSettings(tick_ms=5), Broadcaster(max_connections=1))

    async def scenario() -> None:
        runner.start()
        await asyncio.sleep(0.03)
        assert runner.is_running
        await runner.stop()

    asyncio.run(scenario())
    assert not runner.is_running
    assert runner.simulation.now_ms > 0


def test_runner_does_not_keep_history_in_memory():
    """오래 도는 서버: DB가 없어도 상태 변화와 배차 기록을 tick마다 비워 메모리가 늘지 않는다."""
    runner = SimulationRunner(ServerSettings(), Broadcaster(max_connections=1))
    runner.simulation.press_hall_button(5, Direction.UP)
    for _ in range(100):
        runner.tick_once()
    assert all(not car.history for car in runner.simulation.elevators)
    assert runner.simulation.assignments == []


# ---------------------------------------------------------------- 프록시 뒤의 접속자 IP


@pytest.mark.parametrize(
    ("peer", "forwarded", "hops", "expected"),
    [
        ("10.0.0.1", None, 0, "10.0.0.1"),  # 프록시 없음: 접속한 상대 그대로
        ("10.0.0.1", "203.0.113.5", 0, "10.0.0.1"),  # 프록시 없으면 헤더를 믿지 않는다
        ("10.0.0.1", "203.0.113.5", 1, "203.0.113.5"),  # 프록시 1단: 오른쪽 끝
        ("10.0.0.1", "6.6.6.6, 203.0.113.5", 1, "203.0.113.5"),  # 위조한 왼쪽 값은 무시
        ("10.0.0.1", "6.6.6.6, 203.0.113.5, 172.16.0.9", 2, "203.0.113.5"),  # 프록시 2단
        ("10.0.0.1", "", 1, "10.0.0.1"),  # 헤더가 비면 상대 IP
        (None, None, 0, "unknown"),
    ],
)
def test_client_ip_trusts_only_proxy_appended_values(peer, forwarded, hops, expected):
    assert client_ip(peer, forwarded, hops) == expected


def test_settings_allow_zero_proxy_hops_but_not_negative():
    assert ServerSettings(trusted_proxy_hops=0).trusted_proxy_hops == 0
    with pytest.raises(ValueError):
        ServerSettings(trusted_proxy_hops=-1)
