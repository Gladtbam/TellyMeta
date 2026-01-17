import json
from datetime import datetime, timedelta
from typing import Any, TypeVar, overload

from loguru import logger
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from core.database import async_session
from models.orm import ApiCache

T = TypeVar("T", bound=BaseModel)

class CacheService:
    """缓存服务"""

    @overload
    @staticmethod
    async def get(key: str) -> dict | None: ...

    @overload
    @staticmethod
    async def get(key: str, model: type[T]) -> T | None: ...

    @staticmethod
    async def get(key: str, model: type[T] | None = None) -> T | dict | None:
        """获取缓存数据

        Args:
            key: 缓存键
            model: 可选的 Pydantic 模型类，如果提供则将结果转换为该模型

        Returns:
            如果缓存存在且未过期，返回数据（dict 或 model 实例）；否则返回 None
        """
        async with async_session() as session:
            try:
                stmt = select(ApiCache).where(
                    ApiCache.key == key,
                    ApiCache.expires_at > datetime.now()
                )
                result = await session.execute(stmt)
                cache = result.scalar_one_or_none()
            except SQLAlchemyError as e:
                logger.error("缓存检索期间发生数据库错误：{}", e)
                return None

            if cache:
                try:
                    data = json.loads(cache.value)
                    if model:
                        return model.model_validate(data)
                    return data
                except json.JSONDecodeError:
                    logger.error("解码缓存失败，键：{}", key)
                    return None
                except ValidationError as e:
                    logger.error("验证缓存模型失败，键：{}，错误：{}", key, e)
                    return None

            return None

    @staticmethod
    async def set(key: str, value: Any, ttl: int = 3600) -> None:
        """设置缓存数据

        Args:
            key: 缓存键
            value: 数据（必须可 JSON 序列化，或是 Pydantic 模型）
            ttl: 过期时间（秒），默认 1 小时
        """
        if isinstance(value, BaseModel):
            data = value.model_dump(mode='json')
        else:
            data = value

        try:
            json_str = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.error("序列化缓存数据失败，键：{}，错误：{}", key, e)
            return

        expires_at = datetime.now() + timedelta(seconds=ttl)

        async with async_session() as session:
            try:
                # 使用 upsert (sqlite specific)
                stmt = insert(ApiCache).values(
                    key=key,
                    value=json_str,
                    expires_at=expires_at,
                    created_at=datetime.now()
                ).on_conflict_do_update(
                    index_elements=['key'],
                    set_=dict(
                        value=json_str,
                        expires_at=expires_at,
                        created_at=datetime.now()
                    )
                )
                await session.execute(stmt)
                await session.commit()
            except SQLAlchemyError as e:
                logger.error("缓存设置期间发生数据库错误：{}", e)
                await session.rollback()

    @staticmethod
    async def delete(key: str) -> None:
        """删除缓存"""
        async with async_session() as session:
            try:
                await session.execute(delete(ApiCache).where(ApiCache.key == key))
                await session.commit()
            except SQLAlchemyError as e:
                logger.error("缓存删除期间发生数据库错误：{}", e)

    @staticmethod
    async def cleanup_expired() -> None:
        """清理过期缓存"""
        async with async_session() as session:
            try:
                await session.execute(
                    delete(ApiCache).where(ApiCache.expires_at <= datetime.now())
                )
                await session.commit()
            except SQLAlchemyError as e:
                logger.error("缓存清理期间发生数据库错误：{}", e)
