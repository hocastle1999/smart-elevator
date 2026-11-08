"""FastAPI 앱. 실행: uvicorn app.main:app"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.server.api import router
from app.server.broadcast import Broadcaster
from app.server.rate_limit import RateLimiter
from app.server.runner import SimulationRunner
from app.server.settings import ServerSettings

INDEX_HTML = Path(__file__).parent / "static" / "index.html"


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    """설정을 받아 앱을 만든다. 테스트는 설정을 바꿔 새 앱을 만든다."""
    settings = settings or ServerSettings.from_env()
    broadcaster = Broadcaster(settings.max_connections)
    runner = SimulationRunner(settings, broadcaster)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # 서버가 뜰 때 공유 시뮬레이션 루프를 시작하고, 내려갈 때 멈춘다.
        if settings.run_loop:
            runner.start()
        yield
        await runner.stop()

    app = FastAPI(title="Smart Elevator", lifespan=lifespan)
    app.state.settings = settings
    app.state.runner = runner
    app.state.rate_limiter = RateLimiter(settings.calls_per_window, settings.rate_window_ms / 1000)
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(INDEX_HTML)

    return app


app = create_app()
