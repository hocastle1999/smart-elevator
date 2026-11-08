"""REST API와 WebSocket 엔드포인트.

모든 핸들러는 async def다. 시뮬레이션을 이벤트 루프 스레드에서만 만지기 위해서다 (runner.py 참고).
"""

import math

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, status

from app.server.broadcast import serve_client
from app.server.rate_limit import RateLimiter
from app.server.runner import SimulationRunner
from app.server.schemas import (
    CarCallRequest,
    HallCallRequest,
    HallCallResponse,
    StateSnapshot,
)

router = APIRouter()


def _runner(request: Request) -> SimulationRunner:
    return request.app.state.runner


def _check_rate(request: Request) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    # 프록시 뒤에서는 uvicorn --proxy-headers로 실제 접속자 IP가 들어온다.
    key = request.client.host if request.client else "unknown"
    retry_after = limiter.check(key)
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="호출이 너무 잦습니다. 잠시 후 다시 시도하세요.",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )


@router.get("/health")
async def health(request: Request, response: Response) -> dict:
    runner = _runner(request)
    ok = runner.is_running or not runner.settings.run_loop
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if ok else "loop_stopped",
        "sim_time_ms": runner.simulation.now_ms,
        "viewers": runner.broadcaster.count,
        "lagged_ticks": runner.lagged_ticks,
        "recording": _recording_status(runner),
    }


def _recording_status(runner: SimulationRunner) -> dict | None:
    recorder = runner.recorder
    if recorder is None:
        return None
    return {
        "run_id": recorder.run_id,
        "written_events": recorder.written,
        "dropped_events": recorder.dropped,
        "last_error": recorder.last_error,
    }


@router.get("/api/state", response_model=StateSnapshot)
async def get_state(request: Request) -> StateSnapshot:
    return _runner(request).snapshot()


@router.post(
    "/api/hall-calls",
    response_model=HallCallResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def press_hall_button(body: HallCallRequest, request: Request) -> HallCallResponse:
    """층의 홀 버튼. 배차된 카 번호를 돌려준다.

    202(Accepted): 호출을 받아 배차까지 했지만, 카가 도착하는 것은 나중이기 때문이다.
    """
    _check_rate(request)
    try:
        car = _runner(request).simulation.press_hall_button(body.floor, body.direction)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    return HallCallResponse(car=car)


@router.post("/api/elevators/{car}/car-calls", status_code=status.HTTP_202_ACCEPTED)
async def press_car_button(car: int, body: CarCallRequest, request: Request) -> Response:
    """car번 카 안의 목적층 버튼."""
    simulation = _runner(request).simulation
    if not 0 <= car < len(simulation.elevators):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{car}번 카는 없습니다.")
    _check_rate(request)
    try:
        simulation.press_car_button(car, body.floor)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.websocket("/ws")
async def state_stream(websocket: WebSocket) -> None:
    """접속하면 현재 상태를 바로 한 번 보내고, 이후 tick마다 새 상태를 보낸다."""
    runner: SimulationRunner = websocket.app.state.runner
    await websocket.accept()
    client = runner.broadcaster.add(websocket)
    if client is None:
        # 1013 Try Again Later: 서버가 꽉 찼으니 나중에 다시 접속하라는 표준 코드
        await websocket.close(code=1013, reason="접속자가 너무 많습니다.")
        return
    try:
        client.offer(runner.snapshot().model_dump_json())
        await serve_client(client)
    finally:
        runner.broadcaster.remove(client)
