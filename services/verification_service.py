import asyncio
import contextlib
import contextvars
from datetime import datetime, timedelta
from random import randint, sample
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import Button, errors

from bot.utils import generate_captcha
from core.config import get_settings
from core.database import async_session
from core.telegram_manager import TelethonClientWarper
from core.telegram_message import TelegramMessage
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
from repositories.server_repo import ServerRepository
from repositories.verification_repo import VerificationRepository
from services.notification_renderer import VerificationRejectAuditPayload
from services.notification_service import NotificationService
from services.score_service import resolve_telegram_display_name
from services.user_service import Result, UserService

settings = get_settings()
KICK_TASK_SEMAPHORE = asyncio.Semaphore(3)  # 限制同时踢人任务的数量
# 通过上下文变量透传验证踢出原因，避免改动既有定时调用或单测对 kick_unverified_user(user_id) 的参数契约
_verification_reject_reason_var: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("verification_reject_reason", default=None)
)


async def cleanup_user_with_server_audit(
    session: AsyncSession,
    user_id: int,
    user_service: Any,
) -> tuple[tuple[str, ...], bool, str]:
    """执行成员全量删号，并返回已清理服务器名称列表、清理成功标记及详情文本。

    Args:
        session: 当前异步数据库会话。
        user_id: 待清理的 Telegram 用户 ID。
        user_service: 由调用方构造的 ``UserService`` 实例（兼容模块级单测打桩）。

    Returns:
        ``(cleaned_servers, success, detail)`` 三元组。
    """
    media_repo = MediaRepository(session)
    server_repo = ServerRepository(session)
    before_accounts = list(await media_repo.get_all_by_id(user_id))
    before_servers: list[tuple[int, str]] = []
    seen_sids: set[int] = set()
    for mu in before_accounts:
        sid = int(mu.server_id)
        if sid in seen_sids:
            continue
        seen_sids.add(sid)
        srv = await server_repo.get_by_id(sid)
        before_servers.append((sid, srv.name if srv is not None else f"Server {sid}"))

    delete_res = await user_service.delete_account(user_id, "both")
    success = bool(getattr(delete_res, "success", True))
    detail = str(getattr(delete_res, "message", "") or "").strip()

    after_accounts = list(await media_repo.get_all_by_id(user_id))
    remaining_sids = {int(mu.server_id) for mu in after_accounts}
    cleaned_servers = tuple(
        name for sid, name in before_servers if sid not in remaining_sids
    )
    return cleaned_servers, success, detail


def _format_cleaned_accounts_summary(
    cleaned_servers: tuple[str, ...],
    success: bool,
    detail: str,
) -> str:
    """将清理结果格式化为 ``VerificationRejectAuditPayload`` 使用的单行账号清理摘要。"""
    if cleaned_servers and not success:
        base = f"已清理 {', '.join(cleaned_servers)}（部分失败）"
        return f"{base}: {detail}" if detail else base
    if cleaned_servers:
        return ", ".join(cleaned_servers)
    if not success and detail:
        return detail
    return ""


class VerificationService:
    def __init__(self, app: FastAPI, session: AsyncSession) -> None:
        self.client: TelethonClientWarper = app.state.telethon_client
        self.scheduler: AsyncIOScheduler = app.state.scheduler
        self.config_repo = ConfigRepository(session)
        self.verification_repo = VerificationRepository(session)

    async def start_verification(self, user_id: int) -> Result:
        """开始对新用户进行人机验证"""

        url = f"https://t.me/{settings.telegram_bot_name.lstrip('@')}"
        user_name = await self.client.get_user_name(user_id)
        if not user_name:
            logger.info("非法用户名用户 {} 试图加入群组，拒绝验证。", user_id)
            await self.client.kick_participant(user_id)
            return Result(success=False, message="")
        policy_cfg = await self.config_repo.get_policy_config()
        timeout_minutes = policy_cfg["verification_timeout_minutes"]
        welcome_message = TelegramMessage.render_markdown(t"""\
            欢迎新成员 [{user_name}](tg://user?id={user_id})！

            请在 **{timeout_minutes} 分钟内私聊我** 点击开始并完成人机验证，否则您将会被移出群组。
        """)
        # buttons = [Button.url("➡️ 前往验证", url)]
        keyboard = [
            [Button.url("➡️ 前往验证", url)],
            [
                Button.inline("⛔ 踢出", f"kick_{user_id}".encode()),
                Button.inline("🚫 封禁", f"ban_{user_id}".encode()),
            ],
        ]

        kick_time = datetime.now() + timedelta(minutes=timeout_minutes)
        job = self.scheduler.add_job(
            kick_unverified_user,
            "date",
            run_date=kick_time,
            args=[user_id],
            id=f"kick_{user_id}",
            replace_existing=True,
        )

        await self.verification_repo.create(user_id=user_id, job_id=job.id)

        await self.client.ban_user(user_id, None)  # 先禁言，防止其在验证前发送消息
        return Result(
            success=True,
            message=welcome_message,
            keyboard=keyboard,
            extra_data={"timeout_seconds": timeout_minutes * 60},
        )

    async def _build_group_welcome_message(self, user_id: int) -> str:
        """构造新成员通过入群验证后的群组欢迎文案。

        Args:
            user_id: 已通过人机验证的用户 Telegram ID。

        Returns:
            渲染后的 Markdown 欢迎文案（包含成员可点击提及链接）。
        """
        user_name: str | bool | None = None
        with contextlib.suppress(errors.RPCError, ValueError):
            user_name = await self.client.get_user_name(user_id)
        display_name = str(user_name) if user_name else str(user_id)
        return TelegramMessage.render_markdown(t"""\
            🎉 欢迎新成员 [{display_name}](tg://user?id={user_id}) 加入本群！

            已通过人机验证，可以在群组中发言了。
        """)

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

        buttons = [
            Button.inline(str(opt), b"verify_" + str(opt).encode())
            for opt in shuffled_options
        ]
        keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        return image_data, keyboard

    async def process_verifocation_attempt(self, user_id: int, answer: str) -> Result:
        """处理用户的验证码答案，验证通过后解除禁言并返回群组欢迎消息文案。"""
        challenge = await self.verification_repo.get(user_id)
        if not challenge:
            return Result(
                success=False,
                message="未找到您的验证记录，可能已过期。请重新加入群组以获取新的验证机会。",
            )

        if challenge.captcha_answer != answer:
            with contextlib.suppress(JobLookupError):
                self.scheduler.remove_job(challenge.scheduler_job_id)
            token = _verification_reject_reason_var.set("入群验证答案错误")
            try:
                await kick_unverified_user(user_id)
            finally:
                _verification_reject_reason_var.reset(token)
            return Result(success=False, message="验证码错误，请重新加入群组重试。")

        # 删除定时任务
        with contextlib.suppress(JobLookupError):
            self.scheduler.remove_job(challenge.scheduler_job_id)

        # 删除验证记录
        await self.verification_repo.delete(user_id)
        await self.client.unban_user(user_id)  # 解除用户禁言

        welcome_message = (
            await self._build_group_welcome_message(user_id)
            if challenge.message_id is not None
            else None
        )
        return Result(
            success=True,
            message="验证成功！您现在可以在群组中发言了。",
            private_message=challenge.message_id,
            extra_data=welcome_message,
        )  # type: ignore

    async def reject_verification(self, user_id: int, is_ban: bool = False) -> Result:
        """拒绝用户的验证请求并将其移出群组"""
        user_name = await self.client.get_user_name(user_id)
        challenge = await self.verification_repo.get(user_id)
        if not challenge:
            return Result(
                success=False,
                message=TelegramMessage.render_markdown(
                    t"未[{user_name}](tg://user?id={user_id})找到验证记录，可能已过期。"
                ),
            )
        reject_reason = (
            "管理员拒绝入群验证（封禁）" if is_ban else "管理员拒绝入群验证（踢出）"
        )
        token = _verification_reject_reason_var.set(reject_reason)
        try:
            await kick_unverified_user(user_id, is_ban=is_ban)
        finally:
            _verification_reject_reason_var.reset(token)
        with contextlib.suppress(JobLookupError):
            self.scheduler.remove_job(challenge.scheduler_job_id)

        return Result(
            success=True,
            message=TelegramMessage.render_markdown(
                t"[{user_name}](tg://user?id={user_id})已被移出群组。"
            ),
        )


async def kick_unverified_user(user_id: int, is_ban: bool = False) -> None:
    """将未通过验证的用户移出群组、清理关联账号并发射入群验证清理审计通知。"""
    from main import app

    session_factory: async_sessionmaker[AsyncSession] = async_session
    client: TelethonClientWarper = app.state.telethon_client
    reject_reason = _verification_reject_reason_var.get() or "入群验证超时"
    display_name = await resolve_telegram_display_name(client, user_id)

    async with KICK_TASK_SEMAPHORE:
        try:
            if is_ban:
                await client.ban_user(user_id)
            else:
                await client.kick_participant(user_id)
        except (errors.RPCError, ValueError) as e:
            logger.warning("移出未验证用户 {} 失败: {}", user_id, e)

        async with session_factory() as session:
            try:
                user_service = UserService(app, session)
                verification_repo = VerificationRepository(session)

                (
                    cleaned_servers,
                    del_success,
                    del_detail,
                ) = await cleanup_user_with_server_audit(session, user_id, user_service)
                logger.info("用户 {} 未通过验证，已被移出群组。", user_id)

                await verification_repo.delete(user_id)

                cleaned_summary = _format_cleaned_accounts_summary(
                    cleaned_servers, del_success, del_detail
                )
                try:
                    await NotificationService(app).emit(
                        VerificationRejectAuditPayload(
                            user_id=user_id,
                            user_name=display_name,
                            action="banned" if is_ban else "kicked",
                            reason=reject_reason,
                            cleaned_accounts_summary=cleaned_summary,
                        ),
                        source_key=f"verify_reject:{user_id}",
                        session=session,
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.warning("发射入群验证拒审/超时审计通知失败: {}", exc)
            except SQLAlchemyError as e:
                await session.rollback()
                logger.error("移出未验证用户 {} 失败: {}", user_id, e)
            finally:
                await session.close()
