from collections.abc import Callable, Coroutine
from functools import wraps

from fastapi import FastAPI
from loguru import logger

from core.database import async_session


def provide_db_session(func):
    """
    为被装饰的函数提供一个数据库会话。
    被装饰的函数必须接受一个名为 `session: AsyncSession` 的关键字参数。
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        event = args[1] if len(args) > 1 and hasattr(args[1], 'sender_id') else None

        async with async_session() as session:
            try:
                kwargs['session'] = session
                return await func(*args, **kwargs)
            except Exception as e:
                await session.rollback()
                logger.error("Error in %s for event %s: %s", func.__name__, getattr(event, 'id', 'N/A'), e)
                raise e
            finally:
                await session.close()
    return wrapper

def require_admin(func):
    """
    装饰器，确保只有管理员用户才能执行被装饰的函数。
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        app = args[0] if len(args) > 0 and isinstance(args[0], FastAPI) else None
        event = args[1] if len(args) > 1 and hasattr(args[1], 'sender_id') else None
        if app is None or event is None:
            logger.error("必须提供 FastAPI 实例和事件对象")
            return

        if getattr(event, 'sender_id', None) not in app.state.admin_ids:
            await event.reply("你没有权限执行此操作。")
            return
        return await func(*args, **kwargs)
    return wrapper
