"""FastAPI 앱. 실행: uvicorn app.main:app"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.db.config import DatabaseSettings, create_db_engine
from app.db.recorder import LiveRecorder
from app.server.api import router
from app.server.broadcast import Broadcaster
from app.server.rate_limit import RateLimiter
from app.server.runner import SimulationRunner
from app.server.settings import ServerSettings
from app.server.stats_api import router as stats_router

INDEX_HTML = Path(__file__).parent / "static" / "index.html"


def create_app(
    settings: ServerSettings | None = None, db_settings: DatabaseSettings | None = None
) -> FastAPI:
    """설정을 받아 앱을 만든다. 테스트는 설정을 바꿔 새 앱을 만든다."""
    settings = settings or ServerSettings.from_env()
    db_settings = db_settings or DatabaseSettings()
    # DB는 선택 사항이다. 없으면 기록과 통계 API만 빠지고 시뮬레이션과 화면은 그대로 돈다.
    engine = create_db_engine(db_settings) if db_settings.db_enabled else None
    broadcaster = Broadcaster(settings.max_connections)
    recorder = LiveRecorder(engine) if engine is not None else None
    runner = SimulationRunner(settings, broadcaster, recorder)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # 서버가 뜰 때 공유 시뮬레이션 루프를 시작하고, 내려갈 때 멈춘다.
        if settings.run_loop:
            await runner.start_recording()
            runner.start()
        yield
        await runner.stop()
        if engine is not None:
            engine.dispose()  # 연결 풀의 연결을 닫는다

    app = FastAPI(title="Smart Elevator", lifespan=lifespan)
    app.state.settings = settings
    app.state.runner = runner
    app.state.db_engine = engine
    app.state.rate_limiter = RateLimiter(settings.calls_per_window, settings.rate_window_ms / 1000)
    app.include_router(router)
    app.include_router(stats_router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(INDEX_HTML)

    return app


app = create_app()
