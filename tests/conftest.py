import os
from collections.abc import Iterator
from pathlib import Path

# 앱 모듈을 import하기 "전에" 환경을 정한다 (app.main은 import할 때 앱을 만든다).
# - 일반 테스트는 DB 없이 돈다. .env의 DB_ENABLED=true가 테스트에 새지 않게 끈다.
# - DB 테스트는 db_settings 픽스처가 명시적으로 켜고, 이름이 _test로 끝나는 DB만 쓴다.
# 환경 변수는 .env보다 우선한다.
os.environ["DB_ENABLED"] = "false"
os.environ["ADMIN_TOKEN"] = ""  # 관리 API는 테스트가 명시적으로 켠다
os.environ["DB_NAME"] = os.environ.get("TEST_DB_NAME", "smart_elevator_test")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402

from app.db.config import DatabaseSettings, create_db_engine  # noqa: E402
from app.simulation import Building  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def building() -> Building:
    return Building(num_floors=10)


@pytest.fixture(scope="session")
def db_settings() -> DatabaseSettings:
    settings = DatabaseSettings(db_enabled=True)
    # 안전장치: 실수로 개발용 DB를 지우지 않도록 이름이 _test로 끝나는 DB만 허용한다.
    assert settings.db_name.endswith("_test"), (
        f"테스트는 *_test DB에서만 (현재: {settings.db_name})"
    )
    return settings


@pytest.fixture(scope="session")
def db_engine(db_settings: DatabaseSettings) -> Iterator[Engine]:
    """테스트 세션에서 한 번: 스키마를 마이그레이션으로 새로 만든다.

    base까지 내렸다가 head로 올리므로 마이그레이션의 왕복도 함께 검증된다.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_db_engine(db_settings)
    yield engine
    engine.dispose()


def _clear(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM simulation_runs"))  # 자식 테이블은 ON DELETE CASCADE


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Engine]:
    """DB 테스트 하나마다 빈 테이블에서 시작한다.

    저장 코드가 스스로 트랜잭션을 열고 커밋하므로(engine.begin), 롤백 대신 지워서 정리한다.
    """
    _clear(db_engine)
    yield db_engine
    _clear(db_engine)
