from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
import sqlite3

import aiosqlite
from fastapi import HTTPException
from loguru import logger
from sqlalchemy import MetaData, event
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
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()

naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    """Sqlalchemy模型的基类。"""
    metadata = MetaData(naming_convention=naming_convention)

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
        except HTTPException:
            await session.rollback()
            raise
        except Exception as e:
            await session.rollback()
            logger.exception("数据库会话错误：{}", e)
            raise
        finally:
            await session.close()   # 关闭会话

async def backup_database() -> None:
    """备份数据库"""
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_file = backup_dir / f"tellymeta_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    db_path = DATA_DIR / "tellymeta.db"

    try:
        async with aiosqlite.connect(db_path) as src_db:
            async with aiosqlite.connect(backup_file) as dst_db:
                await src_db.backup(dst_db)
        logger.info("数据库备份成功: {}", backup_file.name)
    except (sqlite3.Error, OSError) as e:
        logger.exception("备份数据库时出错: {}", e)
        return

    for file in backup_dir.iterdir():
        try:
            if file.is_file() and file.name.endswith(".db") and file.stat().st_mtime < datetime.now().timestamp() - 86400 * 7:
                file.unlink()
                logger.info("删除过期备份文件: {}", file.name)
        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.exception("删除过期备份文件时出错: {}", e)
