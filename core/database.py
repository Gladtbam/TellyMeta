from pathlib import Path
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = f"sqlite+aiosqlite:///{Path(__file__).parent.parent / 'bot.db'}"

async_engine = create_async_engine(DATABASE_URL, echo=False, future=True)

async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False)

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
            raise e
        finally:
            await session.close()   # 关闭会话
