import re
import textwrap
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio.session import AsyncSession

from core.config import get_settings
from models.emby import UserPolicy
from repositories.code_repo import CodeRepository
from repositories.config_repo import ConfigRepository
from repositories.emby_repo import EmbyRepository
from repositories.telegram_repo import TelegramRepository
from services.media_service import MediaService
from services.user_service import Result

settings = get_settings()

class AccountService:
    def __init__(self, session: AsyncSession, media_service: MediaService) -> None:
        self.session = session
        self.media_service = media_service
        self.config_repo = ConfigRepository(session)
        self.telegram_repo = TelegramRepository(session)
        self.code_repo = CodeRepository(session)
        self.emby_repo = EmbyRepository(session)

    async def set_registration_mode(self, mode: str)  -> Result:
        """设置注册模式
        Args:
            mode (str): 注册模式:
            - 如果是纯数字，则按名额限制。
            - 如果是时间格式 (e.g., "1h30m"), 则按时间限制。
            - 如果是 'close' 或 'stop', 则关闭开放注册，仅允许注册码和积分注册。
        """
        if re.fullmatch(r'\d+', mode):
            return await self._set_by_limit(int(mode))
        elif re.fullmatch(r'(\d+h)?(\d+m)?(\d+s)?', mode):
            return await self._set_by_time(mode)
        elif mode in ('close', 'stop'):
            return await self._set_closed()
        else:
            return Result(False, "无效的注册模式。请使用纯数字、时间格式 (e.g., '1h30m') 或 'close'/'stop'。")

    async def _set_by_limit(self, limit: int) -> Result:
        """按名额限制设置注册模式
        Args:
            limit (int): 名额限制
        """
        if limit <= 0:
            return Result(False, "名额限制必须是正整数。")

        await self.config_repo.set_settings('registration_mode', 'limit')
        await self.config_repo.set_settings('registration_count_limit', str(limit))
        await self.config_repo.set_settings('registration_time_limit', '0') # 清除时间限制

        return Result(True, f"注册已开启，当前剩余名额: **{limit}**。")

    async def _set_by_time(self, time_str: str) -> Result:
        """按时间限制设置注册模式
        Args:
            time_str (str): 时间限制字符串 (e.g., "1h30m")
        """
        hours = int((re.search(r'(\d+)h', time_str) or [0,0])[1])
        minutes = int((re.search(r'(\d+)m', time_str) or [0,0])[1])
        seconds = int((re.search(r'(\d+)s', time_str) or [0,0])[1])
        time = datetime.now() + timedelta(hours=hours, minutes=minutes, seconds=seconds)

        await self.config_repo.set_settings('registration_mode', 'time')
        await self.config_repo.set_settings('registration_time_limit', str(time.timestamp()))
        await self.config_repo.set_settings('registration_count_limit', '0') # 清除名额限制

        return Result(True, f"注册已开启，截止时间: **{time.strftime('%Y-%m-%d %H:%M:{}')}**。")

    async def _set_closed(self) -> Result:
        """关闭开放注册，仅允许注册码和积分注册"""
        await self.config_repo.set_settings('registration_mode', 'default')
        await self.config_repo.set_settings('registration_count_limit', '0') # 清除名额限制
        await self.config_repo.set_settings('registration_time_limit', '0') # 清除时间限制

        return Result(False, "注册已关闭，仅允许使用注册码和积分注册。")

    async def register(self, user_id: int, username: str | None):
        """注册新用户
        Args:
            user_id (int): 用户的 Telegram ID
            username (str): 用户的 Telegram 用户名
        """
        if not username:
            return Result(False, "请先设置 Telegram 用户名，然后再尝试注册。")

        if await self.emby_repo.get_by_id(user_id):
            return Result(False, "您已经注册过了，无需重复注册。")
        
        mode = await self.config_repo.get_settings('registration_mode', 'default')
        can_register = False

        if mode == 'limit':
            count = int(await self.config_repo.get_settings('registration_count_limit', '0') or '0')
            if count > 0:
                can_register = True
                await self.config_repo.set_settings('registration_count_limit', str(count - 1))
        elif mode == 'time':
            timestamp = float(await self.config_repo.get_settings('registration_time_limit', '0') or '0')
            if timestamp > datetime.now().timestamp():
                can_register = True
        else:
            user = await self.telegram_repo.get_or_create(user_id)
            register_score = int(await self.telegram_repo.get_renew_score())
            if user.score >= register_score:
                can_register = True
                await self.telegram_repo.update_score(user_id, -register_score)

        if not can_register:
            return Result(False, "注册失败，当前未开放注册或您不满足注册条件。")

        emby = await self.media_service.create(username)
        if not emby:
            return Result(False, "注册失败，无法创建账户，请联系管理员。")

        blocked_tags = (await self.config_repo.get_settings('nsfw_library', ''))
        if blocked_tags:
            blocked_tags = blocked_tags.split('|')
        else:
            blocked_tags = []

        if settings.media_server == 'emby':
            policy = UserPolicy(BlockedTags=blocked_tags)
        else:
            policy = UserPolicy(BlockedTags=blocked_tags) # 预留给 Jellyfin 未来使用
        await self.media_service.update_policy(emby.Id, policy, is_none=True)
        passwd = await self.media_service.post_password(emby.Id)
        await self.emby_repo.create(user_id, emby.Id, username)

        return Result(True, textwrap.dedent(f"""\
            注册成功！您的账户信息如下：
            - 服务器地址: `{settings.media_server_url}`
            - 用户名: `{username}`
            - 密码: `{passwd}`

            请尽快登录并修改密码，祝您观影愉快！
        """))

    async def renew(self, user_id: int, use_score: bool) -> Result:
        """续期
        Args:
            user_id (int): 用户的 Telegram ID
        """
        emby_user = await self.emby_repo.get_by_id(user_id)
        if not emby_user:
            return Result(False, "您尚未注册，请先注册后再续期。")

        emby_info = await self.media_service.get_user_info(emby_user.emby_id)
        if not emby_info:
            return Result(False, "续期失败，无法获取您的账户信息，请联系管理员。")

        if emby_user.expires_at > datetime.now() + timedelta(days=7):
            return Result(False, f"续期失败，您的账户有效期还有 **{(emby_user.expires_at - datetime.now()).days}** 天，无需续期。")

        if use_score:
            user = await self.telegram_repo.get_or_create(user_id)
            renew_score = int(await self.telegram_repo.get_renew_score())
            if user.score < renew_score:
                return Result(False, f"续期失败，您的积分不足，续期需要 **{renew_score}** 积分。")
            await self.telegram_repo.update_score(user_id, -renew_score)

        emby_user = await self.emby_repo.extend_expiry(emby_user, 30)
        if emby_info.Policy.IsDisabled:
            await self.media_service.ban_or_unban(emby_user.emby_id, is_ban=False)

        return Result(True, f"续期成功，您的账户已延长至 **{emby_user.expires_at.strftime('%Y-%m-%d %H:%M:{}')}**。")

    async def redeem_code(self, user_id: int, username: str, code_str: str) -> Result:
        """使用注册码或续期码注册或续期
        Args:
            user_id (int): 用户的 Telegram ID
            code_str (str): 注册码或续期码
        """
        code = await self.code_repo.get_by_code(code_str)
        if not code or code.used_at or code.expires_at < datetime.now():
            return Result(False, "无效的注册码或续期码，请检查后重试。")

        if code.type == 'signup':
            result = await self.register(user_id, username)
        elif code.type == 'renew':
            result = await self.renew(user_id, use_score=False)
        else:
            return Result(False, "无效的码类型，请联系管理员。")

        if result.success:
            await self.code_repo.mark_used(code)
        return result

    async def generate_code(self, user_id: int, code_type: str) -> Result:
        """生成注册码或续期码
        Args:
            user_id (int): 用户的 Telegram ID
            code_type (str): 码类型，'signup' 或 'renew'
        """
        if code_type not in ('signup', 'renew'):
            return Result(False, "无效的码类型，请使用 'signup' 或 'renew'。")

        user = await self.telegram_repo.get_or_create(user_id)
        if user.is_admin:
            expires = None
            score = 0
        else:
            expires = int(await self.config_repo.get_settings('code_expiry_days', '30') or '30')
            score = int(await self.telegram_repo.get_renew_score())

        if user.score < score:
            return Result(False, f"生成失败，您的积分不足，生成码需要 **{score}** 积分。")

        code = await self.code_repo.create(code_type, expires)
        await self.telegram_repo.update_score(user_id, -score)

        return Result(True, textwrap.dedent(f"""\
            码生成成功！
            - 类型: **{code.type}**
            - 码: `{code.code}`
            - 过期时间: **{code.expires_at}**

            请妥善保管此码，祝您观影愉快！
        """))

    async def toggle_nsfw_policy(self, user_id: int) -> Result:
        """切换用户的 NSFW 策略
        Args:
            user_id (int): 用户的 Telegram ID
        """
        emby_user = await self.emby_repo.get_by_id(user_id)
        if not emby_user:
            return Result(False, "您尚未注册，请先注册后再设置。")

        emby_info = await self.media_service.get_user_info(emby_user.emby_id)
        if not emby_info:
            return Result(False, "操作失败，无法获取您的账户信息，请联系管理员。")

        nsfw = await self.config_repo.get_settings('nsfw_library', '')
        if not nsfw:
            return Result(False, "管理员尚未设置 NSFW 过滤标签，无法切换 NSFW 策略。")
        nsfw = nsfw.split('|')

        blocked_tags = emby_info.Policy.BlockedTags
        if nsfw in blocked_tags:
            blocked_tags = []
            action = '解除'
        else:
            blocked_tags = nsfw
            action = '设置'

        if settings.media_server == 'emby':
            policy = emby_info.Policy.model_copy(update={'BlockedTags': blocked_tags})
        else:
            policy = emby_info.Policy.model_copy(update={'BlockedTags': blocked_tags}) # 预留给 Jellyfin 未来使用
        await self.media_service.update_policy(emby_user.emby_id, policy)

        return Result(True, textwrap.dedent(f"""\
            操作成功，已为您**{action}** NSFW 策略。
            - 当前 NSFW 策略: {'开启' if 'Japan' not in blocked_tags else '关闭'}
            - 说明: 开启后，含有 'Japan' 标签的内容将被屏蔽。
        """))

    async def forget_password(self, user_id: int):
        """重置密码
        Args:
            user_id (int): 用户的 Telegram ID
        """
        emby_user = await self.emby_repo.get_by_id(user_id)
        if not emby_user:
            return Result(False, "您尚未注册，请先注册后再重置密码。")

        emby_info = await self.media_service.get_user_info(emby_user.emby_id)
        if not emby_info:
            return Result(False, "操作失败，无法获取您的账户信息，请联系管理员。")

        passwd = await self.media_service.post_password(emby_user.emby_id)
        return Result(True, textwrap.dedent(f"""\
            密码重置成功！您的新密码是: `{passwd}`
            请尽快登录并修改密码，祝您观影愉快！
        """))
