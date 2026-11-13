from typing import Any

from fastapi import FastAPI
from loguru import logger
from telethon.errors import RPCError

from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from core.template_manager import template_manager
from models.events import NotificationEvent

settings = get_settings()


class NotificationService:
    def __init__(self, app: FastAPI):
        """初始化通知服务"""
        self.app = app
        self.client: TelethonClientWarper = app.state.telethon_client

    async def _render(
        self, event_type: NotificationEvent, context: dict[str, Any]
    ) -> str | None:
        """内部方法：根据事件类型渲染对应的 .j2 模板"""
        # 映射规则: NotificationEvent.SONARR_DOWNLOAD -> "sonarr_download.j2"
        template_name = f"{event_type.value}.j2"

        return await template_manager.render(template_name, context)

    async def send_to_topic(
        self,
        topic_id: int,
        event_type: NotificationEvent,
        image: Any = None,
        buttons: list | None = None,
        **kwargs,
    ) -> Any:
        """
        发送通知到指定的 Topic 或 Channel (通用)

        Args:
            topic_id: 目标 ID (从 Server 配置中读取传入)
                      < 0: 直接发送到该 ID (作为 Channel/Group ID)
                      > 0: 发送到主群 (settings.telegram_chat_id) 并 Reply 该 Topic ID
            event_type: 事件类型 (决定模板)
            image: 图片/海报
            buttons: 按钮
            **kwargs: 模板变量 (需包含 server_name 等)
        """
        if not topic_id:
            return None

        context = kwargs.copy()
        context["event_type"] = event_type.value

        message_text = await self._render(event_type, context)
        if not message_text:
            return None

        target_chat = settings.telegram_chat_id
        target_reply = None

        if topic_id < 0:
            target_chat = topic_id
            target_reply = None
        elif topic_id > 0:
            target_chat = settings.telegram_chat_id
            target_reply = topic_id

        try:
            sent_msg = await self.client.send_message(
                chat_id=target_chat,
                message=message_text,
                file=image,
                buttons=buttons,
                reply_to=target_reply,
                parse_mode="html",
                link_preview=False,
            )
            logger.info("已发送通知：{}（目标：{}）", event_type.value, topic_id)
            return sent_msg
        except RPCError as e:
            logger.error("Telegram API 错误: {}", e)
            return None
        except Exception as e:
            logger.error("无法发送通知 ({})：{}", event_type.value, e)
            return None

    async def send_to_user(
        self, user_id: int, event_type: NotificationEvent, image: Any = None, **kwargs
    ) -> bool:
        """发送模板通知给特定用户 (私聊)，返回是否发送成功"""
        message_text = await self._render(event_type, kwargs)
        if not message_text:
            logger.warning(
                "用户私聊通知模板渲染为空: user_id={}, event={}",
                user_id,
                event_type.value,
            )
            return False

        try:
            await self.client.send_message(
                chat_id=user_id, message=message_text, file=image, parse_mode="html"
            )
            logger.debug("发送的用户通知：{} -> 用户 {}", event_type.value, user_id)
            return True
        except Exception as e:
            logger.error(
                "无法向 {} 发送用户通知 ({})：{}", user_id, event_type.value, e
            )
            return False

    async def send_text_to_user(
        self, user_id: int, message_text: str, image: Any = None
    ) -> bool:
        """发送自定义文本通知给特定用户 (私聊)，返回是否发送成功"""
        if not message_text:
            return False

        try:
            await self.client.send_message(
                chat_id=user_id, message=message_text, file=image, parse_mode="html"
            )
            logger.debug("发送的用户文本通知 -> 用户 {}", user_id)
            return True
        except Exception as e:
            logger.error("无法向 {} 发送用户文本通知：{}", user_id, e)
            return False
