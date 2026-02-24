from collections.abc import Callable, Coroutine
from functools import wraps

from fastapi import FastAPI
from loguru import logger
from telethon import events

from bot.utils import safe_reply
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
                logger.exception("Error in {} for event {}: {}", func.__name__, getattr(event, 'id', 'N/A'), e)
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

        sender_id = getattr(event, 'sender_id', None)
        if sender_id not in app.state.admin_ids:
            if isinstance(event, events.CallbackQuery.Event):
                await event.answer("没有权限执行此操作。", alert=True)
            else:
                await safe_reply(event, f"[你](tg://user?id={sender_id})没有权限执行此操作。")
            return
        return await func(*args, **kwargs)
    return wrapper

def require_real_reply(func):
    """
    装饰器，确保只有真实回复才能执行被装饰的函数。
    用于兼容群组和论坛的回复消息。
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        event = args[1] if len(args) > 1 and hasattr(args[1], 'sender_id') else None
        if event is None:
            logger.error("必须提供事件对象")
            return

        if not isinstance(event, events.NewMessage.Event):
            return await func(*args, **kwargs)

        msg_id_to_fetch = None
        if getattr(event, 'is_reply', False):
            reply_to = getattr(getattr(event, 'message', None), 'reply_to', None)
            if reply_to:
                if getattr(reply_to, 'forum_topic', False):
                    reply_to_top_id = getattr(reply_to, 'reply_to_top_id', None)
                    reply_to_msg_id = getattr(reply_to, 'reply_to_msg_id', None)
                    if reply_to_top_id is not None and reply_to_msg_id != reply_to_top_id:
                        msg_id_to_fetch = reply_to_msg_id
                else:
                    msg_id_to_fetch = getattr(reply_to, 'reply_to_msg_id', None)

        if not msg_id_to_fetch:
            await safe_reply(event, "❌ 请回复某个特定成员的消息。")
            return

        # 使用我们严谨过滤出的目标 msg ID 回调拉取数据，摒弃 Telethon 内置黑盒获取导致的错位
        app = args[0] if len(args) > 0 and hasattr(args[0], 'state') else None
        client = app.state.telethon_client.client if app else getattr(event, 'client', None)

        if not client:
            logger.error("无法获取 Telethon client 实例")
            return

        reply_msg = await client.get_messages(event.chat_id, ids=msg_id_to_fetch)

        if not getattr(reply_msg, 'sender_id', None):
            await safe_reply(event, "无法获取回复的用户信息。")
            return

        kwargs['target_user_id'] = reply_msg.sender_id
        return await func(*args, **kwargs)
    return wrapper
