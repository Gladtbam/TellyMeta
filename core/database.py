import sqlite3
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.config import get_data_dir, get_settings

DATA_DIR = get_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def normalize_async_database_url(raw_url: str | None) -> str:
    """规范化异步数据库连接字符串，未配置时默认回退到本地 SQLite"""
    url = (raw_url or "").strip()
    if not url:
        return f"sqlite+aiosqlite:///{DATA_DIR / 'tellymeta.db'}"
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    if url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
        return "sqlite+aiosqlite://" + url[len("sqlite://") :]
    return url


def is_sqlite_url(url: str) -> bool:
    """判断连接字符串是否为 SQLite"""
    return url.lower().startswith("sqlite")


def get_sync_database_url(url: str) -> str:
    """将异步连接字符串转换为 APScheduler 等组件所需的同步 SQLAlchemy URL"""
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url[len("sqlite+aiosqlite://") :]
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


DATABASE_URL = normalize_async_database_url(get_settings().database_url)
IS_SQLITE = is_sqlite_url(DATABASE_URL)

async_engine = create_async_engine(
    url=DATABASE_URL,
    echo=False,
    future=True,
    pool_size=20,
    max_overflow=40,
)

async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine, class_=AsyncSession, expire_on_commit=False
)

if IS_SQLITE:

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
        except Exception as e:  # pylint: disable=broad-exception-caught
            await session.rollback()
            logger.exception("数据库会话错误：{}", e)
            raise
        finally:
            await session.close()  # 关闭会话


async def backup_database() -> None:
    """备份数据库（仅 SQLite 模式执行本地文件备份）"""
    if not IS_SQLITE:
        logger.debug("当前使用非 SQLite 数据库，跳过本地文件备份")
        return

    import aiosqlite

    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_file = backup_dir / f"tellymeta_{datetime.now().strftime('%Y%m%d')}.db"
    db_path = DATA_DIR / "tellymeta.db"

    try:
        async with aiosqlite.connect(db_path) as src_db:
            async with aiosqlite.connect(backup_file) as dst_db:
                await src_db.backup(dst_db)
        logger.info("数据库备份成功: {}", backup_file.name)
    except (sqlite3.Error, OSError) as e:
        logger.exception("备份数据库时出错: {}", e)
        return

    from repositories.config_repo import ConfigRepository

    retention_days = ConfigRepository.get_ops_config_from_cache()[
        "db_backup_retention_days"
    ]
    if retention_days <= 0:
        return

    for file in backup_dir.iterdir():
        try:
            if (
                file.is_file()
                and file.name.endswith(".db")
                and file.stat().st_mtime
                < datetime.now().timestamp() - 86400 * retention_days
            ):
                file.unlink()
                logger.info("删除过期备份文件: {}", file.name)
        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.exception("删除过期备份文件时出错: {}", e)
