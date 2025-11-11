from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from loguru import logger
from telethon import TelegramClient, errors
from telethon.errors.rpcerrorlist import (ChannelInvalidError,
                                          ChannelPublicGroupNaError, PeerIdInvalidError,
                                          UserIdInvalidError, UserNotParticipantError)
from telethon.events.common import EventBuilder
from telethon.tl import functions
from telethon.tl.types import (Channel, ChannelParticipantCreator,
                               ChannelParticipantsAdmins, Chat,
                               ChatBannedRights, ForumTopic, ForumTopicDeleted,
                               Message, User)
from telethon.tl.types.messages import ChatFull, ForumTopics

from core.config import get_settings

settings = get_settings()

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

class TelethonClientWarper:
    _handlers: list[tuple[Callable, EventBuilder]] = []

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.client = TelegramClient(
            str( DATA_DIR / "telegram"),
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
            logger.info("Registered handler: {} for event: {}", func.__name__, event_builder)

    async def connect(self) -> None:
        """连接到Telegram"""
        if not self.client.is_connected():
            try:
                if settings.telegram_bot_token:
                    await self.client.start(bot_token=settings.telegram_bot_token) # type: ignore
                else:
                    await self.client.start() # type: ignore
                # await self.client(functions.updates.GetStateRequest())
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
            logger.error("运行 Telethon 客户端时出错：{}", e)
            raise

    async def get_user_name(self, user_id: int | str, need_username: bool = False) -> str | None | Literal[False]:
        """获取用户的名称或用户名
        Args:
            user_id (int | str): 用户的ID或用户名
            need_username (bool): 是否需要返回用户名，默认为False
        Returns:
            str | bool: 返回用户名或名称，如果不存在则返回False
        """
        if not self.client.is_connected():
            await self.connect()
        try:
            entity = await self.client.get_entity(user_id)
            if isinstance(entity, User):
                if entity.bot:
                    logger.warning("用户ID {} 对应的是机器人，非用户实体。", user_id)
                    return False
                if need_username:
                    return entity.username if entity.username else False
                return entity.first_name
            if isinstance(entity, (Channel, Chat)):
                logger.warning("用户ID {} 对应的是频道/群组，非用户实体。", user_id)
                return False
        except errors.UsernameNotOccupiedError as e:
            logger.error("用户名 {} 不存在：{}", user_id, e)
            return False
        except errors.UserIdInvalidError as e:
            logger.error("用户ID {} 无效：{}", user_id, e)
            return False

    async def get_chat_creator_id(self) -> int | None:
        """获取频道/群组的创建者"""
        if not self.client.is_connected():
            await self.connect()
        try:
            async for participant in self.client.iter_participants(self.chat_id):
                if isinstance(participant.participant, ChannelParticipantCreator):
                    return participant.id
            logger.warning("找不到频道/群组的创建者：{}", self.chat_id)
            return None
        except Exception as e:
            logger.error("无法获取 {} 的创建者：{}", self.chat_id, e)
            return None

    async def get_chat_admin_ids(self) -> list[User]:
        """获取频道/群组的管理员列表"""
        admin_ids = []
        if not self.client.is_connected():
            await self.connect()
        try:
            async for participant in self.client.iter_participants(
                self.chat_id,
                filter=ChannelParticipantsAdmins()
            ):
                admin_ids.append(participant)
            return admin_ids
        except Exception as e:
            logger.error("无法获取 {} 的管理员：{}", self.chat_id, e)
            return admin_ids

    async def get_group_topics(self) -> functions.List[ForumTopicDeleted | ForumTopic] | int:
        """获取群组的所有话题"""
        if not self.client.is_connected():
            await self.connect()
        try:
            channel = await self.client.get_entity(self.chat_id)
            if isinstance(channel, Channel) and channel.forum:
                topics: ForumTopics = await self.client(functions.channels.GetForumTopicsRequest(
                    channel=channel, # type: ignore
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100
                ))
                return topics.topics

            if isinstance(channel, Channel) and channel.megagroup :
                logger.info("频道 ID：{} 是超级群组，不支持话题。", self.chat_id)
                full_channel: ChatFull = await self.client(
                    functions.channels.GetFullChannelRequest(channel=channel) # type: ignore
                )
                return getattr(full_channel.full_chat, 'linked_chat_id', self.chat_id)

            if isinstance(channel, Chat):
                logger.info("聊天 ID：{} 是聊天，不支持主题。", self.chat_id)
                return self.chat_id
            return self.chat_id
        except ChannelInvalidError as e:
            logger.error("频道 ID：{} 无效，无法获取主题：{}", self.chat_id, e)
            return self.chat_id
        except ChannelPublicGroupNaError as e:
            logger.error("频道 ID：{} 不可用，无法获取主题：{}", self.chat_id, e)
            return self.chat_id
        except TimeoutError as e:
            logger.error("请求频道 ID：{} 时超时，无法获取主题：{}", self.chat_id, e)
            return self.chat_id

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
            logger.error("Failed to send message: {}", e)
            raise

    async def delete_message(self, chat_id: str | int, message_id: int) -> None:
        """删除指定聊天中的消息
        Args:
            chat_id (str | int): 目标聊天ID
            message_id (int): 要删除的消息ID
        """
        if not self.client.is_connected():
            await self.connect()
        try:
            await self.client.delete_messages(chat_id, message_id)
        except errors.MessageDeleteForbiddenError as e:
            logger.error("无法删除消息 {}：{}", message_id, e)
            raise

    async def edit_message(self, chat_id: str | int, message_id: int, new_content: str) -> Message:
        """编辑指定聊天中的消息内容
        Args:
            chat_id (str | int): 目标聊天ID
            message_id (int): 要编辑的消息ID
            new_content (str): 新的消息内容
        """
        if not self.client.is_connected():
            await self.connect()
        try:
            msg = await self.client.edit_message(chat_id, message_id, new_content)
            return msg
        except errors.MessageEditTimeExpiredError as e:
            logger.error("无法编辑消息 {}：{}", message_id, e)
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
        except (ChannelInvalidError, UserIdInvalidError) as e:
            logger.error("未能禁止用户 {}：{}", user_id, e)
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
        except (ChannelInvalidError, UserIdInvalidError) as e:
            logger.error("无法取消禁止用户 {}：{}", user_id, e)
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
        except (PeerIdInvalidError, UserNotParticipantError) as e:
            logger.exception("未能踢出用户 {}：{}", user_id, e)
            raise

    async def kick_and_ban_participant(self, user_id: int) -> None:
        """将用户移出频道/群组并封禁
        Args:
            user_id (int): 要移出的用户ID
            until_date (int): 封禁截止时间的Unix时间戳
        """
        if not self.client.is_connected():
            await self.connect()

        await self.kick_participant(user_id)
        await self.ban_user(user_id, None)
