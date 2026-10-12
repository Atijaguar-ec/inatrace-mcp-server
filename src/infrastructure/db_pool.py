"""
Resilient PostgreSQL Connection Pool (ISO 27001 Principle of Least Privilege).
Configured for read-only user 'inatrace_mcp_ro' with session-level read-only enforcement.
"""
import logging
from typing import Optional, Any
from src.config import settings

logger = logging.getLogger("inatrace-mcp.db")

try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    asyncpg = None
    ASYNCPG_AVAILABLE = False

_pool: Optional[Any] = None


async def init_db_pool() -> Optional[Any]:
    """
    Initializes the asyncpg connection pool with strict read-only security.
    """
    global _pool
    if not ASYNCPG_AVAILABLE:
        logger.warning("asyncpg is not installed. Database pool running in offline/mock mode.")
        return None

    if _pool is not None:
        return _pool

    try:
        logger.info(
            f"Connecting to PostgreSQL at {settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME} "
            f"as user '{settings.DB_USER}'..."
        )

        async def init_connection(conn):
            # Enforce read-only at the session level (ISO 27001 safeguard)
            await conn.execute("SET default_transaction_read_only = on;")

        _pool = await asyncpg.create_pool(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.DB_NAME,
            min_size=settings.DB_POOL_MIN,
            max_size=settings.DB_POOL_MAX,
            init=init_connection,
            timeout=10.0,
            command_timeout=30.0
        )
        logger.info("PostgreSQL read-only connection pool successfully created.")
        return _pool
    except Exception as exc:
        logger.error(f"Failed to initialize PostgreSQL pool: {exc}")
        _pool = None
        return None


async def close_db_pool():
    """Closes all connections in the pool gracefully."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL connection pool closed.")


def get_db_pool() -> Optional[Any]:
    """Returns the current connection pool instance."""
    return _pool


def set_db_pool(pool: Optional[Any]) -> None:
    """Sets or overrides the connection pool instance (useful for test mocks and lifecycle management)."""
    global _pool
    _pool = pool



async def check_db_health() -> bool:
    """Readiness probe for database connectivity."""
    pool = get_db_pool()
    if not pool:
        return False
    try:
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1;")
            return val == 1
    except Exception as exc:
        logger.error(f"Database health check failed: {exc}")
        return False
