import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 1. Uvezi svoje postavke i modele
from config import get_settings
from models import Base # Tvoj Base iz kojeg nasljeđuje UserMapping
# Ako imaš druge modele, osiguraj da su importani ovdje ili u models.py

settings = get_settings()

# Konfiguracija logiranja
if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

# 2. Postavi Metadata (da Alembic vidi tvoje tablice)
target_metadata = Base.metadata

# 3. Dohvati URL iz tvoje konfiguracije
# Alembic treba string, pa dohvaćamo direktno iz settings
db_url = settings.DATABASE_URL

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = db_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()

async def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    
    # Kreiramo konfiguraciju koristeći URL iz settingsa
    configuration = context.config.get_section(context.config.config_ini_section)
    configuration["sqlalchemy.url"] = db_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())