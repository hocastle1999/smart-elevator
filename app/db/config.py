"""DB 접속 설정. .env(DB_*)에서 읽는다. 비밀번호는 .env에만 둔다."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, Engine, create_engine


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # DB 없이도 시뮬레이션과 화면은 돌아야 한다(공개 데모). 그래서 기본은 꺼 둔다.
    db_enabled: bool = False
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "smart_elevator"
    db_user: str = "smart_elevator_app"
    # SecretStr: 로그나 오류 메시지에 설정 객체가 찍혀도 '**********'로 가려진다.
    db_password: SecretStr = SecretStr("")

    @property
    def database_url(self) -> URL:
        # 문자열을 이어 붙이지 않고 URL.create를 쓴다: 비밀번호의 @, :, / 등을 알아서 처리한다.
        return URL.create(
            drivername="mysql+pymysql",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )

    @property
    def connect_args(self) -> dict[str, object]:
        """연결마다 세션 시간대를 UTC로 고정한다 (로컬 MySQL은 PC 시간대를 따르기 때문).

        앱 엔진과 Alembic(migrations/env.py)이 같은 값을 쓴다.
        """
        return {"init_command": "SET time_zone = '+00:00'"}


def create_db_engine(settings: DatabaseSettings) -> Engine:
    """create_engine은 이 시점에 접속하지 않는다. 첫 쿼리 때 연결 풀에서 연결을 만든다.

    pool_pre_ping: MySQL이 오래 쉬던 연결을 끊은 경우(wait_timeout)에 대비해, 꺼내기 전에
    살아 있는지 확인한다.
    """
    return create_engine(
        settings.database_url, pool_pre_ping=True, connect_args=settings.connect_args
    )
