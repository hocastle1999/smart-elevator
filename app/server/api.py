"""REST API와 WebSocket 엔드포인트.

모든 핸들러는 async def다. 시뮬레이션을 이벤트 루프 스레드에서만 만지기 위해서다 (runner.py 참고).
"""

import math

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, status

from app.server.admin import require_admin
from app.server.broadcast import serve_client
from app.server.rate_limit import RateLimiter, client_ip
from app.server.runner import SimulationRunner
from app.server.schemas import (
    CarCallRequest,
    FaultRequest,
    HallCallRequest,
    HallCallResponse,
    StateSnapshot,
)

router = APIRouter()


def _runner(request: Request) -> SimulationRunner:
    return request.app.state.runner


def _check_rate(request: Request) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    hops = request.app.state.settings.trusted_proxy_hops
    key = client_ip(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        hops,
    )
    retry_after = limiter.check(key)
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            # 어떤 IP 기준으로 제한했는지 알려 준다 (호출한 사람 자신의 IP라 노출 문제가 없고,
            # 배포 환경에서 프록시 설정이 맞는지 확인하는 데 쓴다).
            detail=f"호출이 너무 잦습니다. 잠시 후 다시 시도하세요. (기준 IP: {key})",
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


# ---------------------------------------------------------------------- 관리 (6단계)


def _admin_car(request: Request, car: int) -> SimulationRunner:
    require_admin(request)
    runner = _runner(request)
    if not 0 <= car < len(runner.simulation.elevators):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{car}번 카는 없습니다.")
    return runner


@router.post("/api/elevators/{car}/fault", status_code=status.HTTP_202_ACCEPTED, tags=["admin"])
async def inject_fault(car: int, body: FaultRequest, request: Request) -> Response:
    """고장을 주입한다 (데모, 관리자). 시스템은 감지 규칙으로 알아챌 때까지 모른다."""
    _admin_car(request, car).simulation.inject_fault(car, body.fault)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/api/elevators/{car}/repair", status_code=status.HTTP_202_ACCEPTED, tags=["admin"])
async def repair(car: int, request: Request) -> Response:
    """고장을 고친다. 운행 중지 상태면 복귀(/return)는 따로 한다."""
    _admin_car(request, car).simulation.repair(car)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/api/elevators/{car}/maintenance", status_code=status.HTTP_202_ACCEPTED, tags=["admin"]
)
async def request_maintenance(car: int, request: Request) -> Response:
    """점검 요청: 새 호출을 받지 않고, 맡은 일을 끝내면 운행에서 뺀다."""
    _admin_car(request, car).simulation.request_maintenance(car)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/api/elevators/{car}/return", status_code=status.HTTP_202_ACCEPTED, tags=["admin"])
async def return_to_service(car: int, request: Request) -> Response:
    """운행에 복귀시킨다. 그동안 배차하지 못한 호출을 다시 배차한다."""
    _admin_car(request, car).simulation.return_to_service(car)
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
