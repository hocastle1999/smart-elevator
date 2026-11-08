from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.config import DatabaseSettings
from app.db.tables import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata

# 접속 정보는 alembic.ini가 아니라 .env에서 읽는다. (비밀번호를 git에 올리지 않기 위해)
settings = DatabaseSettings()


def run_migrations_offline() -> None:
    """DB에 접속하지 않고 SQL만 출력한다. (alembic upgrade head --sql)"""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """DB에 접속해 마이그레이션을 실제로 적용한다."""
    connectable = create_engine(
        settings.database_url, poolclass=pool.NullPool, connect_args=settings.connect_args
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
