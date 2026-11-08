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
        "in_service": True,
        "maintenance_requested": False,
        "fault": None,
    }
    assert body["service_log"] == []


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


# ---------------------------------------------------------------- 관리 API (6단계)

TOKEN = "test-admin-token"


@pytest.fixture
def admin_client() -> TestClient:
    from app.server.admin import AdminSettings

    settings = ServerSettings(run_loop=False)
    return TestClient(create_app(settings, admin_settings=AdminSettings(admin_token=TOKEN)))


def admin(client: TestClient, path: str, body: dict | None = None, token: str = TOKEN):
    return client.post(path, json=body or {}, headers={"X-Admin-Token": token})


def test_admin_api_is_closed_without_token_setting(client):
    assert admin(client, "/api/elevators/0/fault", {"fault": "STALL"}).status_code == 403


def test_admin_api_rejects_wrong_token(admin_client):
    res = admin(admin_client, "/api/elevators/0/fault", {"fault": "STALL"}, token="wrong")
    assert res.status_code == 401
    assert admin_client.post("/api/elevators/0/repair").status_code == 401  # 헤더 없음


def test_fault_and_repair_are_visible_in_state(admin_client):
    assert (
        admin(admin_client, "/api/elevators/1/fault", {"fault": "DOOR_SENSOR"}).status_code == 202
    )
    car = admin_client.get("/api/state").json()["elevators"][1]
    assert car["fault"] == "DOOR_SENSOR" and car["in_service"]  # 감지 전에는 운행 중
    assert admin(admin_client, "/api/elevators/1/repair").status_code == 202
    assert admin_client.get("/api/state").json()["elevators"][1]["fault"] is None


def test_maintenance_and_return(admin_client):
    admin(admin_client, "/api/elevators/2/maintenance")
    car = admin_client.get("/api/state").json()["elevators"][2]
    assert car["in_service"] is False and car["state"] == "OUT_OF_SERVICE"  # 대기 중이라 바로
    res = admin_client.post("/api/hall-calls", json={"floor": 5, "direction": "UP"})
    assert res.json()["car"] != 2
    admin(admin_client, "/api/elevators/2/return")
    assert admin_client.get("/api/state").json()["elevators"][2]["in_service"] is True


def test_admin_api_validates_input(admin_client):
    assert admin(admin_client, "/api/elevators/9/repair").status_code == 404
    assert admin(admin_client, "/api/elevators/0/fault", {"fault": "FIRE"}).status_code == 422


def test_service_log_appears_in_state_after_tick(admin_client):
    admin(admin_client, "/api/elevators/0/fault", {"fault": "SLOW_MOTOR"})
    runner_of(admin_client).tick_once()
    log = admin_client.get("/api/state").json()["service_log"]
    assert [(e["car"], e["kind"], e["detail"]) for e in log] == [(0, "FAULT", "SLOW_MOTOR")]


def test_hall_call_without_any_car_in_service_returns_null(admin_client):
    for car in range(3):
        admin(admin_client, f"/api/elevators/{car}/maintenance")
    res = admin_client.post("/api/hall-calls", json={"floor": 4, "direction": "UP"})
    assert res.status_code == 202 and res.json()["car"] is None
