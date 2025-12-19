import textwrap
from dataclasses import dataclass
from datetime import date
from random import choice, choices, randint
from typing import Any, Literal

from httpx import HTTPError
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button

from core.config import get_settings
from models.orm import TelegramUser
from repositories.code_repo import CodeRepository
from repositories.media_repo import MediaRepository
from repositories.telegram_repo import TelegramRepository
from services.media_service import MediaService

settings = get_settings()

@dataclass
class Result:
    """封装服务方法的结果，用于返回给 Handler 处理器。"""
    success: bool
    message: str = ""
    private_message: str | int | None = None
    keyboard: Any | None = None

class UserService:
    def __init__(self, session: AsyncSession, media_service: MediaService) -> None:
        self.session = session
        self.telegram_repo = TelegramRepository(session)
        self.media_repo = MediaRepository(session)
        self.code_repo = CodeRepository(session)
        self.media_service = media_service

    async def perform_checkin(self, user_id: int) -> Result:
        """用户签到，更新签到时间和签到次数。
        
        Args:
            user_id (int): 用户的 Telegram ID。
        
        Returns:
            Result: 包含签到结果的对象。
        """
        user = await self.telegram_repo.get_or_create(user_id)

        if user.last_checkin.date() == date.today():
            return Result(success=False, message="您今天已经签到过了，请明天再来！")

        if (user.checkin_count + 1) % 7 != 0:
            score = randint(-2, 5)
            update_user = await self.telegram_repo.update_checkin(user_id, score)
            if update_user:
                return Result(success=True, message=f"签到成功！您获得了 **{score}** 积分。")
            else:
                return Result(success=False, message="签到失败，请稍后再试。")
        else:
            return await self._perform_lucky_checkin(user)

    async def _perform_lucky_checkin(self, user: TelegramUser) -> Result:
        """每7天签到有概率获得额外奖励。
        
        Args:
            user (TelegramUser): 用户对象。
        
        Returns:
            Result: 包含签到结果的对象。
        """
        options = ['fullcode', 'halfcode', 'weekcode', 'daycode', 'double']
        probability = [0.01, 0.02, 0.12, 0.15, 0.7]
        result = choices(options, probability)[0]
        current_renew_score = await self.telegram_repo.get_renew_score()

        if result == 'fullcode':
            code_type = choice(['renew', 'signup'])
            code_name = '续期码' if code_type == 'renew' else '注册码'
            code = await self.code_repo.create(code_type, None)
            return Result(
                success=True,
                message=f"🎉 **恭喜抽中大奖！** {code_name}已通过私信发送给您。",
                private_message=f"签到成功！您获得了 **{code_name}**: `{code.code}`，请妥善保管！"
            )

        if result in ['halfcode', 'weekcode', 'daycode']:
            days = {'halfcode': 15, 'weekcode': 7, 'daycode': 1}[result]
            if user.media_user:
                await self.media_repo.extend_expiry(user.media_user, days)
                await self.telegram_repo.update_checkin(user.id, 0)
                await self.media_service.ban_or_unban(user.media_user.media_id, is_ban=False)
                return Result(
                    success=True,
                    message=f"🎉 **恭喜中奖！** 您的 {settings.media_server.capitalize()} 账户已延长 **{days}** 天有效期！"
                )

            score = int(current_renew_score / 30 * days)
            await self.telegram_repo.update_checkin(user.id, score)
            return Result(
                success=True,
                message=f"🎉 **恭喜中奖！** 由于您尚未绑定 {settings.media_server.capitalize()} 账户，已已自动折算为 **{score}** 积分！"
            )

        if result == 'double':
            score = abs(randint(-2, 5)) * 2
            await self.telegram_repo.update_checkin(user.id, score)
            return Result(success=True, message=f"🎉 **恭喜！** 签到积分翻倍，您获得了 **{score}** 积分。")

        await self.telegram_repo.update_checkin(user.id, 1)
        return Result(success=True, message="签到成功！您获得了保底 **1** 积分。")

    async def get_user_info(self, user_id: int) -> Result:
        """获取用户信息，包括 Media 账户信息。
        
        Args:
            user_id (int): 用户的 Telegram ID。
        
        Returns:
            Result: 包含用户信息的对象。
        """
        user = await self.telegram_repo.get_or_create(user_id)

        message = textwrap.dedent(f"""\
            **Telegram ID**: `{user.id}`
            **积分**: `{user.score}`
            **签到天数**: `{user.checkin_count}`
            **警告次数**: `{user.warning_count}`
        """)

        if user.media_user:
            message += textwrap.dedent(f"""\
                **{settings.media_server.capitalize()} 用户名**: `{user.media_user.media_name}`
                **{settings.media_server.capitalize()} 用户 ID**: `{user.media_user.media_id}`
                **{settings.media_server.capitalize()} 过期时间**: `{user.media_user.expires_at}`
                **{settings.media_server.capitalize()} 删除时间**: `{user.media_user.delete_at if user.media_user.delete_at else '未设置'}`
                **{settings.media_server.capitalize()} 状态**: `{'封禁' if user.media_user.is_banned else '正常'}`
            """)

        button_layout = [
        [('生成 “码”', 'me_create_code'), ('NSFW开关', 'me_nfsw'), ('忘记密码', 'me_forget_password')],
        [('续期', 'me_renew'), ('线路查询', 'me_line'), ('查询续期积分', 'me_query_renew')],
        [('求片', f'me_request_{user.id}'), ('上传字幕', f'me_subtitle_{user.id}')]
        ]
        keyboard = [
            [Button.inline(text, data=data.encode('utf-8')) for text, data in row]
            for row in button_layout
        ]

        return Result(success=True, message=message, keyboard=keyboard if user.media_user else None)

    async def delete_account(self, user_id: int, account_type: Literal['media', 'tg', 'both']) -> Result:
        """删除用户账户，包括 Media 账户和 Telegram 账户（可选）。
        
        Args:
            user_id (int): 用户的 Telegram ID。
            account_type (Literal['media', 'tg', 'both']): 要删除的账户类型。
        """
        user = await self.telegram_repo.get_by_id(user_id)
        if not user:
            return Result(True, "未找到该 Telegram 账户，无需删除。")

        message = []
        try:
            if account_type in ['media', 'both'] and user.media_user:
                await self.media_service.delete_user(user.media_user.media_id)
                await self.media_repo.delete(user.media_user)
                message.append(f"{settings.media_server.capitalize()} 账户已删除。")

            if account_type in ['tg', 'both']:
                await self.telegram_repo.delete_by_id(user_id)
                message.append("Telegram 账户已删除。")

            return Result(True, " ".join(message))
        except HTTPError:
            logger.error("删除账户失败{}：{}", user_id, user.media_user)
            return Result(False, " ".join(message))
