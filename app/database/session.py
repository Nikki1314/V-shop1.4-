"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

_IDENTITY_TABLES: Final[tuple[str, ...]] = (
    "categories",
    "products",
    "users",
    "orders",
)


def get_engine() -> AsyncEngine:
    """Return the global async engine, creating it if needed."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            hide_parameters=True,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the global async session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def init_db() -> None:
    """Initialize database connectivity."""
    get_session_factory()
    logger.info("Database engine initialized")


async def check_db_connection() -> None:
    """Check PostgreSQL connection."""
    async with get_session_factory()() as session:
        value = await session.scalar(text("SELECT 1"))
        if value != 1:
            raise RuntimeError("Database connectivity check failed")
    logger.info("Database connectivity check passed")


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    """Facts identifying the connected database."""

    system_identifier: str | None = None
    counts: dict[str, int | None] = field(default_factory=dict)

    @property
    def catalog_is_empty(self) -> bool:
        return not self.counts.get("categories") and not self.counts.get("products")


def sanitized_database_url() -> str:
    """Return database URL with password masked."""
    return make_url(get_settings().database_url).render_as_string(
        hide_password=True
    )


async def _probe(session: AsyncSession, sql: str) -> object | None:
    """Run a read-only database probe."""
    try:
        async with session.begin_nested():
            value = await session.scalar(text(sql))
            return value
    except SQLAlchemyError:
        return None


async def read_database_identity(
    session: AsyncSession,
) -> DatabaseIdentity:
    """Collect read-only database information."""
    raw_id = await _probe(
        session,
        "SELECT system_identifier FROM pg_control_system()",
    )

    counts: dict[str, int | None] = {}

    for table in _IDENTITY_TABLES:
        value = await _probe(
            session,
            f"SELECT count(*) FROM {table}",
        )
        counts[table] = int(value) if isinstance(value, int) else None

    return DatabaseIdentity(
        system_identifier=str(raw_id) if raw_id is not None else None,
        counts=counts,
    )


async def log_database_identity() -> DatabaseIdentity | None:
    """Log information about the connected database."""
    try:
        async with get_session_factory()() as session:
            identity = await read_database_identity(session)
    except Exception:
        logger.debug(
            "Database identity probe failed",
            exc_info=True,
        )
        return None

    logger.info(
        "Database identity: url=%s system_identifier=%s categories=%s "
        "products=%s users=%s orders=%s",
        sanitized_database_url(),
        identity.system_identifier,
        identity.counts.get("categories"),
        identity.counts.get("products"),
        identity.counts.get("users"),
        identity.counts.get("orders"),
    )

    if identity.catalog_is_empty:
        logger.warning(
            "Catalog is EMPTY at startup (categories=%s products=%s).",
            identity.counts.get("categories"),
            identity.counts.get("products"),
        )

    return identity


async def close_db() -> None:
    """Dispose of the database engine."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed")

    _engine = None
    _session_factory = None


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session with automatic commit and rollback."""
    session = get_session_factory()()

    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
