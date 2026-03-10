from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from .settings import DATABASE_URL


def _ensure_async_postgres_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(
    _ensure_async_postgres_url(DATABASE_URL),
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,       # recycle connections every 30 min
    pool_size=10,
    max_overflow=15,
    pool_timeout=30,         # fail after 30s instead of waiting forever
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
