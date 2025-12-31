from pathlib import Path
from typing import Any

from fastapi import FastAPI
from jinja2 import (Environment, FileSystemLoader, TemplateNotFound,
                    select_autoescape)
from loguru import logger

from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from models.events import NotificationEvent

settings = get_settings()

class NotificationService:
    def __init__(self, app: FastAPI):
        """初始化通知服务"""
        self.app = app
        self.client: TelethonClientWarper = app.state.telethon_client

        self.template_dir = Path.cwd() / "templates"
        if not self.template_dir.exists():
            self.template_dir = Path(__file__).parent.parent / "templates"
        if not self.template_dir.exists():
            logger.warning("未在以下位置找到通知模板目录：{}", self.template_dir)

        self.jinja_env = Environment(
            loader=FileSystemLoader(self.template_dir),
            autoescape=select_autoescape(['html', 'xml']),
            enable_async=True,  # 启用异步渲染
            trim_blocks=True,   # 自动去除块后的换行
            lstrip_blocks=True, # 自动去除块前的空白
        )

    async def _render(self, event_type: NotificationEvent, context: dict[str, Any]) -> str | None:
        """内部方法：根据事件类型渲染对应的 .j2 模板"""
        # 映射规则: NotificationEvent.SONARR_DOWNLOAD -> "sonarr_download.j2"
        template_name = f"{event_type.value}.j2"

        try:
            template = self.jinja_env.get_template(template_name)

            return await template.render_async(context)
        except TemplateNotFound:
            logger.info("缺少通知模板文件：{}", template_name)
            return None
        except Exception as e:
            logger.error("渲染模板 {} 时出错：{}", template_name, e)
            return None

    async def send_to_topic(
        self,
        topic_id: int,
        event_type: NotificationEvent,
        image: Any = None,
        buttons: list | None = None,
        **kwargs
    ) -> None:
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
            return

        context = kwargs.copy()
        context['event_type'] = event_type.value

        message_text = await self._render(event_type, context)
        if not message_text:
            return

        target_chat = settings.telegram_chat_id
        target_reply = None

        if topic_id < 0:
            target_chat = topic_id
            target_reply = None
        elif topic_id > 0:
            target_chat = settings.telegram_chat_id
            target_reply = topic_id

        try:
            await self.client.send_message(
                chat_id=target_chat,
                message=message_text,
                file=image,
                buttons=buttons,
                reply_to=target_reply,
                parse_mode='html',
                link_preview=False
            )
            logger.info("已发送通知：{}（目标：{}）", event_type.value, topic_id)
        except Exception as e:
            logger.error("无法发送通知 ({})：{}", event_type.value, e)

    async def send_to_user(
        self,
        user_id: int,
        event_type: NotificationEvent,
        image: Any = None,
        **kwargs
    ) -> None:
        """
        发送通知给特定用户 (私聊)
        """
        message_text = await self._render(event_type, kwargs)
        if not message_text:
            return

        try:
            await self.client.send_message(
                chat_id=user_id,
                message=message_text,
                file=image,
                parse_mode='html'
            )
            logger.debug("发送的用户通知：{} -> 用户 {}", event_type.value, user_id)
        except Exception as e:
            logger.error("无法向 {} 发送用户通知：{}", user_id, e)
