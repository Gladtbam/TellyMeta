import logging
from collections.abc import Callable
from datetime import datetime
from functools import partial

from fastapi import FastAPI
from telethon import TelegramClient, errors
from telethon.events.common import EventBuilder
from telethon.tl import functions
from telethon.tl.types import (ChannelParticipantCreator,
                               ChannelParticipantsAdmins, ChatBannedRights,
                               Message)

from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class TelethonClientWarper:
    _handlers: list[tuple[Callable, EventBuilder]] = []

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.client = TelegramClient(
            'bot',
            settings.telegram_api_id,
            settings.telegram_api_hash
        )
        self.chat_id = settings.telegram_chat_id
        self._register_handlers()

    @staticmethod
    def handler(event_builder: EventBuilder):
        """静态装饰器
        用于注册事件处理器
        """
        def decorator(func: Callable):
            TelethonClientWarper._handlers.append((func, event_builder))
            return func
        return decorator

    def _register_handlers(self) -> None:
        """注册所有处理器"""
        for func, event_builder in self._handlers:
            handler_with_app = partial(func, self.app)
            self.client.add_event_handler(handler_with_app, event_builder)
            logger.info("Registered handler: %s for event: %s", func.__name__, event_builder)

    async def connect(self) -> None:
        """连接到Telegram"""
        if not self.client.is_connected():
            try:
                if settings.telegram_bot_token:
                    await self.client.start(bot_token=settings.telegram_bot_token) # type: ignore
                else:
                    await self.client.start() # type: ignore
                await self.client(functions.updates.GetStateRequest())
            except errors.SessionPasswordNeededError:
                logger.error("Two-step verification is enabled. Please provide the password.")
                raise

    async def disconnect(self) -> None:
        """断开与Telegram的连接"""
        if self.client.is_connected():
            await self.client.disconnect() # type: ignore

    async def is_connected(self) -> bool:
        """检查是否已连接到Telegram
        Returns:
            bool: 如果已连接则返回True，否则返回False
        """
        return self.client.is_connected()

    async def run_until_disconnected(self) -> None:
        """运行客户端直到断开连接"""
        if not self.client.is_connected():
            await self.connect()
        try:
            self.client.run_until_disconnected()
        except Exception as e:
            logger.error("Error while running Telethon client: %s", e)
            raise

    async def get_chat_creator_id(self):
        """获取频道/群组的创建者"""
        if not self.client.is_connected():
            await self.connect()
        try:
            async for participant in self.client.iter_participants(self.chat_id):
                if isinstance(participant.participant, ChannelParticipantCreator):
                    return participant.id
            logger.warning("No creator found for chat ID: %s", self.chat_id)
            return None
        except Exception as e:
            logger.error("Failed to get channel creator for %s: %s", self.chat_id, e)
            return None

    async def get_chat_admin_ids(self):
        """获取频道/群组的管理员列表"""
        admin_ids = []
        if not self.client.is_connected():
            await self.connect()
        try:
            async for participant in self.client.iter_participants(self.chat_id):
                if isinstance(participant.participant, ChannelParticipantsAdmins):
                    admin_ids.append(participant.id)
            return admin_ids
        except Exception as e:
            logger.error("Failed to get channel admins for %s: %s", self.chat_id, e)
            return admin_ids

    async def send_message(self, chat_id: str | int, message: str) -> Message:
        """发送消息到指定的聊天ID
        Args:
            message (str): 要发送的消息内容
            chat_id (str): 目标聊天ID
        """
        if not self.client.is_connected():
            await self.connect()
        try:
            msg = await self.client.send_message(chat_id, message)
            return msg
        except errors.FloodWaitError as e:
            logger.error("Failed to send message: %s", e)
            raise

    async def ban_user(self, user_id: int, until_date: datetime | None) -> None:
        """封禁用户
        Args:
            user_id (int): 要封禁的用户ID
            until_date (int): 封禁截止时间的Unix时间戳
        """
        rights = ChatBannedRights(
            until_date=until_date,
            send_messages = True,
            send_media = True,
            send_stickers = True,
            send_gifs = True,
            send_games = True,
            send_inline = True,
            embed_links = True,
            send_polls = True,
            change_info = True,
            invite_users = True,
            pin_messages = True,
            manage_topics = True,
            send_photos = True,
            send_videos = True,
            send_roundvideos = True,
            send_audios = True,
            send_voices = True,
            send_docs = True,
            send_plain = True,
        )
        if not self.client.is_connected():
            await self.connect()
        try:
            channel = await self.client.get_input_entity(self.chat_id)
            participant = await self.client.get_input_entity(user_id)
    
            await self.client(functions.channels.EditBannedRequest(
                channel=channel, # type: ignore
                participant=participant,
                banned_rights=rights
            ))
        except errors.FloodWaitError as e:
            logger.error("Failed to ban user %d: %s", user_id, e)
            raise
        
    async def unban_user(self, user_id: int) -> None:
        """解封用户
        Args:
            user_id (int): 要解封的用户ID
        """
        rights = ChatBannedRights(until_date=None)
        if not self.client.is_connected():
            await self.connect()
        try:
            channel = await self.client.get_input_entity(self.chat_id)
            participant = await self.client.get_input_entity(user_id)
    
            await self.client(functions.channels.EditBannedRequest(
                channel=channel, # type: ignore
                participant=participant,
                banned_rights=rights
            ))
        except errors.FloodWaitError as e:
            logger.error("Failed to unban user %d: %s", user_id, e)
            raise

    async def kick_participant(self, user_id: int) -> None:
        """将用户移出频道/群组
        Args:
            user_id (int): 要移出的用户ID
        """
        if not self.client.is_connected():
            await self.connect()
        try:
            await self.client.kick_participant(self.chat_id, user_id)
        except errors.FloodWaitError as e:
            logger.error("Failed to kick user %d: %s", user_id, e)
            raise
