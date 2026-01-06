from collections.abc import AsyncGenerator
from pathlib import Path

from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)
from sqlalchemy.orm import DeclarativeBase

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR / 'tellymeta.db'}"

async_engine = create_async_engine(
    url=DATABASE_URL,
    echo=False,
    future=True,
    pool_size=20,
    max_overflow=40
)

async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False
)

@event.listens_for(async_engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()

class Base(DeclarativeBase):
    """Sqlalchemy模型的基类。"""
    def __repr__(self) -> str:
        """返回模型的列名和对应的值。"""
        return f"{self.__class__.__name__}({', '.join(f'{col.name}={getattr(self, col.name)}' for col in self.__table__.columns)})"

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """提供异步数据库会话的生成器。
    用于依赖注入，确保每次请求都能获取到一个新的会话，并在请求结束时关闭会话。
    Yields:
        AsyncSession: 异步数据库会话对象。
    Raises:
        Exception: 如果会话操作失败，将回滚事务并抛出异常。
    """
    async with async_session() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.exception("数据库会话错误：{}", e)
        finally:
            await session.close()   # 关闭会话
