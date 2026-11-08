"""REST API와 WebSocket 테스트 (FastAPI TestClient)."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.server.settings import ServerSettings


def make_client(**overrides) -> TestClient:
    # 기본은 루프를 돌리지 않는다. 시간은 runner.tick_once()로 직접 진행한다.
    settings = ServerSettings(**{"run_loop": False, **overrides})
    return TestClient(create_app(settings))


@pytest.fixture
def client() -> TestClient:
    return make_client()


def runner_of(client: TestClient):
    return client.app.state.runner


# ---------------------------------------------------------------- 조회


def test_state_describes_building_and_cars(client):
    body = client.get("/api/state").json()
    assert body["num_floors"] == 10
    assert body["time_ms"] == 0
    assert len(body["elevators"]) == 3
    assert body["elevators"][0] == {
        "id": 0,
        "floor": 1,
        "position": 1.0,
        "state": "IDLE",
        "direction": "NONE",
        "car_calls": [],
        "hall_calls": [],
    }


def test_index_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_health_ok_when_loop_disabled(client):
    assert client.get("/health").json()["status"] == "ok"


def test_health_503_when_loop_should_run_but_does_not():
    # with 블록 없이 쓰면 lifespan이 돌지 않아 루프가 시작되지 않는다.
    client = TestClient(create_app(ServerSettings(run_loop=True)))
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json()["status"] == "loop_stopped"


# ---------------------------------------------------------------- 홀 호출


def test_hall_call_is_dispatched_and_returns_car(client):
    res = client.post("/api/hall-calls", json={"floor": 5, "direction": "UP"})
    assert res.status_code == 202
    car = res.json()["car"]
    state = client.get("/api/state").json()
    assert state["elevators"][car]["hall_calls"] == [{"floor": 5, "direction": "UP"}]
    assert state["elevators"][car]["state"] == "MOVING_UP"


def test_time_advances_with_ticks(client):
    client.post("/api/hall-calls", json={"floor": 2, "direction": "UP"})
    for _ in range(20):  # 100ms x 20 = 2000ms = 한 층 이동
        runner_of(client).tick_once()
    state = client.get("/api/state").json()
    assert state["time_ms"] == 2000
    assert state["elevators"][0]["state"] == "DOORS_OPENING"


@pytest.mark.parametrize(
    "body",
    [
        {"floor": 11, "direction": "UP"},  # 건물 밖
        {"floor": 10, "direction": "UP"},  # 맨 위층 올라감
        {"floor": 3, "direction": "NONE"},
        {"floor": 3, "direction": "SIDEWAYS"},
        {"floor": 3, "direction": "UP", "extra": 1},  # 모르는 필드
        {"floor": "three", "direction": "UP"},
    ],
)
def test_invalid_hall_call_is_422(client, body):
    assert client.post("/api/hall-calls", json=body).status_code == 422


# ---------------------------------------------------------------- 카 호출


def test_car_call_goes_to_chosen_car(client):
    res = client.post("/api/elevators/2/car-calls", json={"floor": 7})
    assert res.status_code == 202
    state = client.get("/api/state").json()
    assert state["elevators"][2]["car_calls"] == [7]
    assert state["elevators"][0]["car_calls"] == []


def test_car_call_to_unknown_car_is_404(client):
    assert client.post("/api/elevators/3/car-calls", json={"floor": 7}).status_code == 404


def test_car_call_out_of_range_is_422(client):
    assert client.post("/api/elevators/0/car-calls", json={"floor": 0}).status_code == 422


# ---------------------------------------------------------------- 빈도 제한


def test_too_many_calls_are_rejected_with_retry_after():
    client = make_client(calls_per_window=2, rate_window_ms=60_000)
    for floor in (2, 3):
        assert client.post("/api/hall-calls", json={"floor": floor, "direction": "UP"}).is_success
    res = client.post("/api/elevators/0/car-calls", json={"floor": 5})
    assert res.status_code == 429
    assert 0 < int(res.headers["Retry-After"]) <= 60


# ---------------------------------------------------------------- WebSocket


def test_websocket_sends_current_state_on_connect(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
    assert first["time_ms"] == 0
    assert first["viewers"] == 1


def test_websocket_streams_state_while_loop_runs():
    # 실제 루프를 짧은 간격, 빠른 배속으로 돌린다 (tick 10ms마다 시뮬레이션 1초).
    settings = ServerSettings(tick_ms=10, sim_ms_per_tick=1000)
    with TestClient(create_app(settings)) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        client.post("/api/hall-calls", json={"floor": 3, "direction": "DOWN"})
        for _ in range(500):
            state = ws.receive_json()
            if any(e["floor"] == 3 and e["state"] == "DOORS_OPEN" for e in state["elevators"]):
                break
        else:
            pytest.fail("카가 3층에 도착한 상태를 받지 못함")


def test_websocket_over_capacity_is_closed_with_1013():
    client = make_client(max_connections=1)
    with client.websocket_connect("/ws") as first:
        first.receive_json()
        with client.websocket_connect("/ws") as second, pytest.raises(WebSocketDisconnect) as exc:
            second.receive_json()
        assert exc.value.code == 1013


def test_viewer_count_drops_after_disconnect(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert client.get("/health").json()["viewers"] == 1
    assert client.get("/health").json()["viewers"] == 0
