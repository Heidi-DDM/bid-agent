"""Alembic 迁移环境：目标元数据来自 runtime.db.models（analysis_jobs 等）。"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from runtime.core.config import database_url
from runtime.db import models  # noqa: F401  # 注册 Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 数据库连接串只从环境变量读取，避免把真实凭据写入迁移配置
config.set_main_option("sqlalchemy.url", database_url())

target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()