import contextlib
import inspect
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any, ClassVar, Literal, cast

import python_socks
from fastapi import FastAPI
from loguru import logger
from telethon import TelegramClient, errors
from telethon.errors.rpcerrorlist import (
    ChannelInvalidError,
    ChannelPublicGroupNaError,
    PeerIdInvalidError,
    UserIdInvalidError,
    UserNotParticipantError,
)
from telethon.events.common import EventBuilder
from telethon.hints import MarkupLike
from telethon.tl import functions
from telethon.tl.types import (
    Channel,
    ChannelParticipantCreator,
    ChannelParticipantsAdmins,
    Chat,
    ChatBannedRights,
    KeyboardInlineButton,
    Message,
    ReplyInlineMarkup,
    User,
)
from telethon.tl.types.messages import ChatFull

from core.config import get_data_dir, get_settings

settings = get_settings()

DATA_DIR = get_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_edit_buttons(buttons: MarkupLike | None) -> MarkupLike | None:
    """规范化编辑消息时的按钮参数，避免向 EditMessageRequest 传入空按钮结构。

    Telegram MTProto 的 ``messages.editMessage`` 在携带空 ``ReplyInlineMarkup(rows=[])``
    或由空列表生成的空 ``ReplyKeyboardMarkup(rows=[])`` 时会返回 ``REPLY_MARKUP_INVALID``；
    显式传入 ``None``（不设置 ``reply_markup`` 字段）才会安全清除原消息的内联按钮。
    """
    if buttons is None:
        return None
    if isinstance(buttons, ReplyInlineMarkup):
        has_buttons = any(getattr(row, "buttons", None) for row in (buttons.rows or ()))
        return buttons if has_buttons else None
    if isinstance(buttons, (list, tuple)):
        non_empty_rows = [
            row
            for row in buttons
            if (isinstance(row, (list, tuple)) and len(row) > 0)
            or (not isinstance(row, (list, tuple)) and row is not None)
        ]
        return cast(MarkupLike, non_empty_rows) if non_empty_rows else None
    return buttons


class TelethonClientWarper:
    _handlers: ClassVar[list[tuple[Callable, EventBuilder]]] = []

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        proxy_dict = None
        if settings.proxy:
            try:
                ptype, host, port, username, password = python_socks.parse_proxy_url(
                    settings.proxy
                )
                proxy_dict = {
                    "proxy_type": ptype.value,
                    "addr": host,
                    "port": port,
                    "username": username,
                    "password": password,
                    "rdns": True,
                }
            except (ValueError, KeyError, TypeError) as e:
                logger.error("无法解析代理URL {}: {}", settings.proxy, e)

        kwargs = {}
        if proxy_dict:
            kwargs["proxy"] = proxy_dict

        self.client = TelegramClient(
            str(DATA_DIR / "telegram"),
            settings.telegram_api_id,
            settings.telegram_api_hash,
            **kwargs,
        )
        self.chat_id = settings.telegram_chat_id
        # 缓存当前 Bot 自身的用户 ID，避免频繁调用 get_me()
        self._cached_me_id: int | None = None
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
            logger.info(
                "Registered handler: {} for event: {}", func.__name__, event_builder
            )

    async def connect(self) -> None:
        """连接到Telegram"""
        if not self.client.is_connected():
            try:
                if settings.telegram_bot_token:
                    await self.client.start(bot_token=settings.telegram_bot_token)  # type: ignore
                else:
                    await self.client.start()  # type: ignore
                # await self.client(functions.updates.GetStateRequest())
            except errors.SessionPasswordNeededError:
                logger.error(
                    "Two-step verification is enabled. Please provide the password."
                )
                raise

    async def disconnect(self) -> None:
        """断开与Telegram的连接"""
        if self.client.is_connected():
            await self.client.disconnect()  # type: ignore

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
            await self.client.run_until_disconnected()  # type: ignore
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("运行 Telethon 客户端时出错：{}", e)
            raise

    async def get_user_name(
        self, user_id: int | str, need_username: bool = False
    ) -> str | Literal[False] | None:
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
        except errors.PeerIdInvalidError as e:
            logger.error("无法获取 {} 的创建者：{}", self.chat_id, e)
            return None

    async def get_chat_admin_ids(self) -> list[User]:
        """获取频道/群组的管理员列表"""
        admin_ids = []
        if not self.client.is_connected():
            await self.connect()
        try:
            async for participant in self.client.iter_participants(
                self.chat_id, filter=ChannelParticipantsAdmins()
            ):
                if isinstance(participant.participant, ChannelParticipantCreator):
                    continue
                if isinstance(participant, User) and participant.bot:
                    continue
                admin_ids.append(participant)
            return admin_ids
        except errors.PeerIdInvalidError as e:
            logger.error("无法获取 {} 的管理员：{}", self.chat_id, e)
            return admin_ids

    async def get_group_topics(self) -> dict[str, Any]:
        """获取群组的所有话题"""
        mapping = {}
        is_forum = False

        if not self.client.is_connected():
            await self.connect()
        try:
            channel = await self.client.get_entity(self.chat_id)
            if isinstance(channel, Channel) and channel.forum:
                is_forum = True
                mapping[self.chat_id] = "💬 主群组 (使用指令绑定话题)"

            if isinstance(channel, Channel) and channel.megagroup:
                full_channel: ChatFull = await self.client(
                    functions.channels.GetFullChannelRequest(channel=channel)  # type: ignore
                )
                linked_chat_id = getattr(full_channel.full_chat, "linked_chat_id", None)
                if linked_chat_id:
                    mapping[int(f"-100{linked_chat_id}")] = "📢 关联频道"
                mapping[self.chat_id] = "💬 当前群组"

            else:
                mapping[self.chat_id] = "💬 当前群组"
        except (ChannelInvalidError, ChannelPublicGroupNaError, TimeoutError) as e:
            logger.error("频道 ID：{} 无效，无法获取群组信息：{}", self.chat_id, e)
            mapping[self.chat_id] = "💬 当前群组"
        return {"is_forum": is_forum, "topics": mapping}

    async def get_participant(self, user_id: int) -> User | None:
        """获取指定用户在频道/群组中的参与者信息
        Args:
            user_id (int): 用户ID
        Returns:
            User | None: 返回参与者信息，如果不存在则返回None
        """
        if not self.client.is_connected():
            await self.connect()
        try:
            return await self.client(
                functions.channels.GetParticipantRequest(
                    channel=self.chat_id,  # type: ignore
                    participant=await self.client.get_input_entity(user_id),
                )
            )
        except UserNotParticipantError:
            logger.warning("用户 {} 不是频道/群组 {} 的成员。", user_id, self.chat_id)
            return None

    async def send_message(
        self,
        chat_id: str | int,
        message: str,
        file: Any = None,
        buttons: MarkupLike | list[list[KeyboardInlineButton]] | None = None,
        reply_to: int | Message | None = None,
        **kwargs,
    ) -> Message:
        """发送消息到指定的聊天ID
        Args:
            message (str): 要发送的消息内容
            chat_id (str): 目标聊天ID
        """
        if not self.client.is_connected():
            await self.connect()
        try:
            return await self.client.send_message(
                chat_id,
                message,
                file=file,
                buttons=cast(MarkupLike | None, buttons),
                reply_to=reply_to,  # type: ignore
                **kwargs,
            )
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

    async def _resolve_existing_inline_markup(
        self, chat_id: str | int, message_id: int
    ) -> ReplyInlineMarkup | None:
        """读取目标消息当前附带的内联按钮，供未显式改写或清空按钮时保留原布局。"""
        get_messages = getattr(self.client, "get_messages", None)
        if not inspect.iscoroutinefunction(get_messages):
            return None
        with contextlib.suppress(errors.RPCError, ValueError, TypeError):
            existing_msg = await get_messages(chat_id, ids=message_id)
            existing_markup = getattr(existing_msg, "reply_markup", None)
            if isinstance(existing_markup, ReplyInlineMarkup):
                normalized = _normalize_edit_buttons(existing_markup)
                if isinstance(normalized, ReplyInlineMarkup):
                    return normalized
        return None

    async def edit_message(
        self,
        chat_id: str | int,
        message_id: int,
        new_content: str,
        *,
        buttons: MarkupLike | None = None,
        clear_buttons: bool = False,
        parse_mode: str | None = "markdown",
    ) -> Message:
        """编辑指定聊天中的消息内容。

        Args:
            chat_id: 目标聊天 ID。
            message_id: 要编辑的消息 ID。
            new_content: 新的消息文本内容。
            buttons: 可选的新内联按钮布局；未传入且 ``clear_buttons=False`` 时保留原按钮。
            clear_buttons: 为 ``True`` 时不携带 ``reply_markup``（传 ``None``），清空原消息附带的内联按钮。
            parse_mode: 文本解析模式，默认启用 ``"markdown"`` 以支持用户提及链接。
        """
        if not self.client.is_connected():
            await self.connect()
        if clear_buttons:
            # MTProto EditMessageRequest 传 ReplyInlineMarkup(rows=[]) 会触发 REPLY_MARKUP_INVALID；
            # 传 None 使底层不携带 reply_markup 字段，从而移除原有内联键盘。
            effective_buttons: MarkupLike | None = None
        elif buttons is not None:
            effective_buttons = _normalize_edit_buttons(buttons)
        else:
            effective_buttons = await self._resolve_existing_inline_markup(
                chat_id, message_id
            )
        try:
            return await self.client.edit_message(
                chat_id,
                message_id,
                new_content,
                buttons=effective_buttons,
                parse_mode=parse_mode,
            )
        except errors.MessageEditTimeExpiredError as e:
            logger.error("无法编辑消息 {}：{}", message_id, e)
            raise

    async def ban_user(self, user_id: int, until_date: datetime | None = None) -> None:
        """封禁用户
        Args:
            user_id (int): 要封禁的用户ID
            until_date (int): 封禁截止时间的Unix时间戳
        """
        rights = ChatBannedRights(
            until_date=until_date,
            send_messages=True,
            send_media=True,
            send_stickers=True,
            send_gifs=True,
            send_games=True,
            send_inline=True,
            embed_links=True,
            send_polls=True,
            change_info=True,
            invite_users=True,
            pin_messages=True,
            manage_topics=True,
            send_photos=True,
            send_videos=True,
            send_roundvideos=True,
            send_audios=True,
            send_voices=True,
            send_docs=True,
            send_plain=True,
        )
        if not self.client.is_connected():
            await self.connect()
        try:
            channel = await self.client.get_input_entity(self.chat_id)
            participant = await self.client.get_input_entity(user_id)

            await self.client(
                functions.channels.EditBannedRequest(
                    channel=channel,  # type: ignore
                    participant=participant,
                    banned_rights=rights,
                )
            )
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

            await self.client(
                functions.channels.EditBannedRequest(
                    channel=channel,  # type: ignore
                    participant=participant,
                    banned_rights=rights,
                )
            )
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
