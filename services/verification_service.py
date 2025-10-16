import textwrap
from datetime import datetime, timedelta
from random import randint, sample

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import Button

from bot.utils import generate_captcha
from core.config import get_settings
from core.database import async_session
from core.telegram_manager import TelethonClientWarper
from repositories.verification_repo import VerificationRepository
from services.user_service import Result

settings = get_settings()


class VerificationService:
    def __init__(self, app: FastAPI, session: AsyncSession) -> None:
        self.session = session
        self.client: TelethonClientWarper = app.state.telethon_client
        self.scheduler: AsyncIOScheduler = app.state.scheduler
        self.verification_repo = VerificationRepository(session)

    async def start_verification(self, user_id: int) -> Result:
        """开始对新用户进行人机验证"""

        url = f"https://t.me/{settings.telegram_bot_name.lstrip('@')}"
        welcome_message = textwrap.dedent(f"""\
            欢迎新成员 [{user_id}](tg://user?id={user_id})！

            请在 **5 分钟内私聊我**并完成人机验证，否则您将会被移出群组。
        """)
        buttons = [Button.url("➡️ 前往验证", url)]

        kick_time = datetime.now() + timedelta(minutes=5)
        job = self.scheduler.add_job(
            kick_unverified_user,
            'date',
            run_date=kick_time,
            args=[user_id],
            id=f"kick_{user_id}",
            replace_existing=True
        )

        await self.verification_repo.create(
            user_id=user_id,
            job_id=job.id
        )

        await self.client.ban_user(user_id, None)  # 先禁言，防止其在验证前发送消息
        return Result(
            success=True,
            message=welcome_message,
            keyboard=buttons)

    async def create_get_challenge_details(self, user_id: int):
        """生成验证码并返回图片和选项按钮"""
        challenge = await self.verification_repo.get(user_id)
        if not challenge:
            return None

        correct_answer, image_data = generate_captcha()
        await self.verification_repo.update_answer(user_id, correct_answer)

        options = {int(correct_answer)}
        while len(options) < 4:
            offset = randint(-10, 10)
            if offset == 0:
                continue
            options.add(int(correct_answer) + offset)

        shuffled_options = sample(list(options), len(options))

        buttons = [Button.inline(str(opt), b"verify_" + str(opt).encode()) for opt in shuffled_options]
        keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        return image_data, keyboard

    async def process_verifocation_attempt(self, user_id: int, answer: str) -> Result:
        """处理用户的验证码答案"""
        challenge = await self.verification_repo.get(user_id)
        if not challenge:
            return Result(success=False, message="未找到您的验证记录，可能已过期。请重新加入群组以获取新的验证机会。")

        if challenge.captcha_answer != answer:
            await kick_unverified_user(
                user_id
            )
            return Result(success=False, message="验证码错误，请重新加入群组重试。")

        # 删除定时任务
        try:
            self.scheduler.remove_job(challenge.scheduler_job_id)
        except Exception as e:
            logger.warning("移除定时任务失败: {}", e)

        # 删除验证记录
        await self.verification_repo.delete(user_id)
        await self.client.unban_user(user_id)  # 解除用户禁言

        return Result(success=True, message="验证成功！您现在可以在群组中发言了。")

async def kick_unverified_user(
    user_id: int,
) -> None:
    """将未通过验证的用户移出群组"""
    from main import app
    session_factory: async_sessionmaker[AsyncSession] = async_session
    client: TelethonClientWarper = app.state.telethon_client
    scheduler: AsyncIOScheduler = app.state.scheduler

    async with session_factory() as session:
        try:
            verification_repo = VerificationRepository(session)
            challenge = await verification_repo.get(user_id)
            if challenge:
                await verification_repo.delete(user_id)

            await client.kick_participant(user_id)
            logger.info("用户 {} 未通过验证，已被移出群组。", user_id)

            # 删除验证记录
            await verification_repo.delete(user_id)
        except Exception as e:
            await session.rollback()
            raise e
        finally:
            await session.close()   # 关闭会话
