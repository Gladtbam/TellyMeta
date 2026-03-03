import textwrap
from dataclasses import dataclass
from datetime import date, timedelta
from random import choice, choices, randint
from typing import Any, Literal

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from models.orm import RegistrationMode, ServerType, TelegramUser
from repositories.code_repo import CodeRepository
from repositories.media_repo import MediaRepository
from repositories.server_repo import ServerRepository
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
    extra_data: Any | None = None

class UserService:
    def __init__(self, app: FastAPI, session: AsyncSession) -> None:
        self.session = session
        self.telegram_repo = TelegramRepository(session)
        self.media_repo = MediaRepository(session)
        self.code_repo = CodeRepository(session)
        self.server_repo = ServerRepository(session)
        self.media_clients: dict[int, MediaService] = app.state.media_clients or {}

    async def perform_checkin(self, user_id: int) -> Result:
        """用户签到"""
        user = await self.telegram_repo.get_or_create(user_id)

        today = date.today()
        last_date = user.last_checkin.date()

        if last_date == today:
            return Result(success=False, message=f"[您](tg://user?id={user_id})今天已经签到过了，请明天再来！")

        is_consecutive = last_date == (today - timedelta(days=1))
        bonus = 2 if is_consecutive else 0

        if (user.checkin_count + 1) % 7 != 0:
            base_score = randint(1, 3)
            final_score = base_score + bonus

            update_user = await self.telegram_repo.update_checkin(user_id, final_score)
            if update_user:
                msg = f"✅ 签到成功！[您](tg://user?id={user_id})获得了 **{final_score}** 积分。"
                if is_consecutive:
                    msg += f"\n(基础 {base_score} + 连签 {bonus})"
                return Result(success=True, message=msg)
            else:
                return Result(success=False, message="签到失败，请稍后再试。")
        else:
            return await self._perform_lucky_checkin(user)

    async def _perform_lucky_checkin(self, user: TelegramUser) -> Result:
        """幸运签到逻辑"""
        options = ['fullcode', 'halfcode', 'weekcode', 'daycode', 'double']
        probability = [0.01, 0.02, 0.12, 0.15, 0.7]
        result = choices(options, probability)[0]

        current_renew_score = await self.telegram_repo.get_renew_score()

        if result == 'fullcode':
            # 获取所有已启用的服务器
            enabled_servers = await self.server_repo.get_all_enabled()

            # 筛选符合条件的服务器：
            candidate_servers = [
                s for s in enabled_servers
                if s.id in self.media_clients
                and s.server_type in (ServerType.EMBY, ServerType.JELLYFIN)
                and s.registration_mode == RegistrationMode.DEFAULT
            ]

            if not candidate_servers:
                return Result(success=True, message="签到成功！获得 **5** 积分 (暂无符合发放奖励条件的服务器)。")

            target_server = choice(candidate_servers)
            if not target_server.id:
                return Result(success=True, message="签到成功！获得 **5** 积分 (暂无符合发放奖励条件的服务器)。")

            code_type = choice(['renew', 'signup'])
            code_name = '续期码' if code_type == 'renew' else '注册码'

            code = await self.code_repo.create(code_type, target_server.code_expiry_days, target_server.id)

            return Result(
                success=True,
                message=f"🎉 **恭喜抽中大奖！** {target_server.name} 的 {code_name}已通过私信发送给[您](tg://user?id={user.id})。",
                private_message=f"🎁 签到大奖！\n服务器: {target_server.name}\n类型: {code_name}\n代码: `{code.code}`\n请妥善保管！"
            )

        if result in ['halfcode', 'weekcode', 'daycode']:
            days = {'halfcode': 15, 'weekcode': 7, 'daycode': 1}[result]

            if user.media_users:
                extended_servers = []
                for mu in user.media_users:
                    await self.media_repo.extend_expiry(mu, days)

                    client = self.media_clients.get(mu.server_id)
                    if client:
                        await client.ban_or_unban(mu.media_id, is_ban=False)

                    server = await self.server_repo.get_by_id(mu.server_id)
                    extended_servers.append(server.name if server else str(mu.server_id))

                await self.telegram_repo.update_checkin(user.id, 0)
                server_str = ", ".join(extended_servers)
                return Result(
                    success=True,
                    message=f"🎉 **恭喜中奖！** [您](tg://user?id={user.id})的媒体账户 ({server_str}) 已自动延长 **{days}** 天有效期！"
                )

            score = int(current_renew_score / 30 * days)
            await self.telegram_repo.update_checkin(user.id, score)
            return Result(
                success=True,
                message=f"🎉 **恭喜中奖！** 获得 {days} 天时长奖励，因未绑定账户自动折算为 **{score}** 积分！"
            )

        # 检查连签 (lucky 同样享受连签加成，但如果是 flip/code 类奖励则不加积分)
        today = date.today()
        last_date = user.last_checkin.date()
        is_consecutive = last_date == (today - timedelta(days=1))
        bonus = 2 if is_consecutive else 0

        if result == 'double':
            base = abs(randint(2, 4)) * 2
            total = base + bonus
            await self.telegram_repo.update_checkin(user.id, total)
            msg = f"🎉 **恭喜！** 签到积分翻倍，[您](tg://user?id={user.id})获得了 **{total}** 积分。"
            if is_consecutive:
                msg += f"\n(基础 {base} + 连签 {bonus})"
            return Result(success=True, message=msg)

        # 保底逻辑
        total = 1 + bonus
        await self.telegram_repo.update_checkin(user.id, total)
        msg = f"签到成功！[您](tg://user?id={user.id})获得了保底 **{total}** 积分。"
        if is_consecutive:
            msg += f"\n(基础 1 + 连签 {bonus})"
        return Result(success=True, message=msg)

    async def get_user_info_data(self, user_id: int) -> dict:
        """获取结构化用户信息"""
        user = await self.telegram_repo.get_or_create(user_id)

        media_accounts = []
        if user.media_users:
            for mu in user.media_users:
                server = await self.server_repo.get_by_id(mu.server_id)
                media_accounts.append({
                    "media_user": mu,
                    "server": server,
                    "server_name": server.name if server else f"Server {mu.server_id}",
                    "server_url": server.url if server and server.url else "Unknown",
                    "server_type": server.server_type.capitalize() if server else "Undefind",
                    "status_text": "🚫 封禁" if mu.is_banned else "✅ 正常",
                    "is_banned": mu.is_banned,
                    "media_name": mu.media_name,
                    "expires_at": mu.expires_at,
                    "allow_subtitle_upload": server.allow_subtitle_upload if server else False,
                    "allow_request": server.allow_request if server else False,
                    "tos": server.tos if server else None
                })

        return {
            "user": user,
            "media_accounts": media_accounts
        }

    async def get_user_info(self, user_id: int) -> Result:
        """获取用户信息"""
        data = await self.get_user_info_data(user_id)
        user: TelegramUser = data['user']
        media_accounts = data['media_accounts']

        message = textwrap.dedent(f"""\
            👤 **个人信息**

            **Telegram ID**: `{user.id}`
            **积分**: `{user.score}`
            **签到**: `{user.checkin_count}` 天
            **警告**: `{user.warning_count}` 次
        """)

        if media_accounts:
            message += "\n\n**媒体账户**\n"
            for mu in media_accounts:
                message += f"- {mu['server_name']}: {mu['media_name']} ({mu['status_text']})\n"

        return Result(success=True, message=message)

    async def get_rank_list(self) -> Result:
        """获取排行榜"""
        users = await self.telegram_repo.get_top_users(10)
        if not users:
            return Result(True, "暂无排名数据。")

        return Result(True, "排行榜获取成功", extra_data=users)

    async def delete_account(self, user_id: int, account_type: Literal['media', 'tg', 'both']) -> Result:
        """删除账户"""
        user = await self.telegram_repo.get_by_id(user_id)
        if not user:
            return Result(True, "未找到该 Telegram 账户，无需删除。")

        logs = []
        try:
            if account_type in ['media', 'both'] and user.media_users:
                for mu in user.media_users:
                    client = self.media_clients.get(mu.server_id)
                    server_name = f"Server {mu.server_id}"

                    server = await self.server_repo.get_by_id(mu.server_id)
                    if server:
                        server_name = server.name

                    if client:
                        try:
                            await client.delete_user(mu.media_id)
                            logs.append(f"已删除 {server_name} 上的账户。")
                        except Exception as e:
                            logger.error(f"Failed to delete user on {server_name}: {e}")
                            logs.append(f"删除 {server_name} 账户失败 (API错误)。")
                    else:
                        logs.append(f"{server_name} 实例未连接，仅删除数据库记录。")

                    await self.media_repo.delete(mu)

            if account_type in ['tg', 'both']:
                await self.telegram_repo.delete_by_id(user_id)
                logs.append("Telegram 绑定记录已清除。")

            msg = "\n".join(logs) if logs else "无关联账户需要删除。"
            return Result(True, msg)

        except Exception as e:
            logger.exception(f"Delete account error for {user_id}: {e}")
            return Result(False, f"删除过程中发生错误: {str(e)}")
