import base64
import ipaddress
import json
import re
import socket
import textwrap
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI
from httpx import AsyncClient, HTTPError, RequestError
from jinja2.sandbox import SandboxedEnvironment
from loguru import logger
from sqlalchemy.ext.asyncio.session import AsyncSession

from core.config import get_settings
from models.orm import RegistrationMode, ServerInstance, ServerType
from models.protocols import User
from repositories.code_repo import CodeRepository
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.media_service import MediaService
from services.user_service import Result

settings = get_settings()

class AccountService:
    def __init__(self, app: FastAPI, session: AsyncSession) -> None:
        self.config_repo = ConfigRepository(session)
        self.telegram_repo = TelegramRepository(session)
        self.code_repo = CodeRepository(session)
        self.media_repo = MediaRepository(session)
        self.server_repo = ServerRepository(session)
        self.media_clients: dict[int, MediaService] = app.state.media_clients

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        """检查 URL 的目标地址是否安全（非内网/回环/保留地址）"""
        parsed = urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            return False

        if parsed.username is not None or parsed.password is not None:
            return False

        if parsed.scheme not in ('http', 'https'):
            return False

        try:
            addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return False

        if not addr_infos:
            return False

        for addr_info in addr_infos:
            ip = ipaddress.ip_address(addr_info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local or ip.is_multicast:
                return False

        return True

    @staticmethod
    def _matches_prefix(url: str, prefix: str) -> bool:
        """安全地验证 URL 是否匹配指定前缀。
        使用 URL 解析而非简单字符串匹配，以防止 host 欺骗。

        匹配规则：
        1. scheme 和 hostname 必须完全匹配
        2. 端口必须匹配
        3. URL 的 path 必须以 prefix 的 path 开头
        """
        parsed_url = urlparse(url)
        parsed_prefix = urlparse(prefix)

        if parsed_url.scheme != parsed_prefix.scheme:
            return False
        if (parsed_url.hostname or '').lower() != (parsed_prefix.hostname or '').lower():
            return False

        if parsed_url.port != parsed_prefix.port:
            return False

        prefix_path = parsed_prefix.path or '/'
        url_path = parsed_url.path or '/'
        if not url_path.startswith(prefix_path):
            return False

        return True

    async def verify_external_user(self, server_id: int, user_input: str) -> Result:
        """执行外部验证"""
        server = await self.server_repo.get_by_id(server_id)
        if not server or not server.registration_external_url:
            return Result(False, "服务器配置错误：缺少外部验证链接。")

        prefixes = [url.strip() for url in server.registration_external_url.split('|') if url.strip()]
        target_url = None

        user_input = user_input.strip()

        if user_input.startswith("http://") or user_input.startswith("https://"):
            for prefix in prefixes:
                if self._matches_prefix(user_input, prefix):
                    target_url = user_input
                    break
            if not target_url:
                return Result(False, "提供的链接不在允许的验证范围内。")
        else:
            # 非 URL 输入不允许包含可能导致 URL 解析异常的特殊字符
            if any(c in user_input for c in ('@', '\\', '\n', '\r')):
                return Result(False, "输入包含非法字符。")
            if not prefixes:
                return Result(False, "服务器未配置有效的验证前缀。")
            target_url = f"{prefixes[0]}{user_input}"

        if not self._is_safe_url(target_url):
            logger.warning("外部验证被阻止，目标地址不安全: {}", target_url)
            return Result(False, "验证请求被拒绝：目标地址不允许。")

        try:
            async with AsyncClient(timeout=10.0, follow_redirects=False) as client:
                response = await client.get(target_url)

                if server.registration_external_parser:
                    env = SandboxedEnvironment()
                    context = {
                        "response": response, 
                        "r": response,

                        "json": json,
                        "base64": base64,
                        "re": re,

                        "len": len,
                        "int": int,
                        "str": str,
                        "bool": bool,
                        "list": list,
                        "dict": dict,
                    }
                    try:
                        # 执行自定义解析代码
                        expr = env.compile_expression(server.registration_external_parser)
                        is_valid = expr(**context)
                        if is_valid:
                            return Result(True, "验证通过")
                        else:
                            return Result(False, "验证失败 (解析未通过)。")
                    except (NameError, TypeError, ValueError, SyntaxError, AttributeError) as e:
                        logger.error(f"外部验证解析代码执行错误: {e}")
                        return Result(False, f"验证解析出错: {e}")
                else:
                    if response.is_success:
                        return Result(True, "验证通过")
                    else:
                        return Result(False, f"验证失败 (Status: {response.status_code})。")
        except RequestError as e:
            logger.error("外部验证错误：{}", e)
            return Result(False, f"验证请求发生网络错误: {str(e)}")

    async def register(
        self,
        user_id: int,
        username: str | None | Literal[False],
        server_id: int,
        skip_checks: bool = False
    ) -> Result:
        """注册新用户
        Args:
            user_id (int): 用户的 Telegram ID
            username (str): 用户的 Telegram 用户名
        """
        if not username:
            return Result(False, "请先设置 Telegram 用户名，然后再尝试注册。")

        server = await self.server_repo.get_by_id(server_id)
        if not server or not server.is_enabled:
            return Result(False, "该服务器不存在或已停用。")

        if await self.media_repo.get_by_id(user_id, server_id):
            return Result(False, f"您已经在 **{server.name}** 注册过了，无需重复注册。")

        can_register = False
        mode = server.registration_mode

        if skip_checks:
            can_register = True
        elif mode == RegistrationMode.COUNT:
            if server.registration_count_limit > 0:
                can_register = True
                await self.server_repo.update_policy_config(server.id, count=server.registration_count_limit - 1)
            else:
                await self.server_repo.update_policy_config(server.id, mode=RegistrationMode.DEFAULT)
                return Result(False, "该服务器注册名额已满。")
        elif mode == RegistrationMode.TIME:
            limit_time = float(server.registration_time_limit)
            if limit_time > datetime.now().timestamp():
                can_register = True
            else:
                await self.server_repo.update_policy_config(server.id, mode=RegistrationMode.DEFAULT)
                return Result(False, "该服务器开放注册时间已截止。")
        elif mode == RegistrationMode.OPEN:
            can_register = True
        elif mode == RegistrationMode.DEFAULT:
            user = await self.telegram_repo.get_or_create(user_id)
            register_score = int(await self.telegram_repo.get_renew_score())
            if user.score >= register_score:
                can_register = True
                await self.telegram_repo.update_score(user_id, -register_score)
            else:
                return Result(False, f"您的积分不足，注册该服务器需要 **{register_score}** 积分。")
        else:
            return Result(False, "该服务器当前未开放注册。")

        if not can_register:
            return Result(False, "注册失败，当前未开放注册或您不满足注册条件。")

        media_service: MediaService | None = self.media_clients.get(server_id)
        if not media_service:
            # 回滚积分扣除（如果是积分注册）
            if mode == RegistrationMode.DEFAULT:
                await self.telegram_repo.update_score(user_id, int(await self.telegram_repo.get_renew_score()))
            return Result(False, "服务器连接实例未找到，请联系管理员。")
        try:
            media_user_dto, pw = await media_service.create(username)
            if not media_user_dto:
                return Result(False, "注册失败，无法创建账户，请联系管理员。")

            expires_at = server.registration_expiry_days
            media_user = await self.media_repo.create(
                user_id=user_id,
                server_id=server.id,
                media_id=media_user_dto.Id,
                media_name=username,
                expires_at=expires_at
            )

            if not server.nsfw_enabled:
                await self._apply_nsfw_policy(media_service, media_user_dto.Id, server, enable_nsfw=False)
            else:
                await media_service.update_policy(media_user_dto.Id, {'EnableAllFolders': True}, is_none=True)

            return Result(True, textwrap.dedent(f"""\
                🎉 **注册成功！**
                
                服务器: `{server.name}`
                地址: `{server.url}`
                用户名: `{username}`
                密码: `{pw}`
                
                有效期至: {media_user.expires_at.strftime('%Y-%m-%d')}
                请尽快登录并修改密码，祝您观影愉快！
            """))
        except HTTPError:
            logger.error("{}: {} 注册失败", username, user_id)
            return Result(False, "注册失败，请联系管理员")

    async def _apply_nsfw_policy(
        self,
        client: MediaService,
        media_user_id: str,
        server: ServerInstance,
        enable_nsfw: bool,
        current_policy: Any | None = None) -> None:
        """辅助方法：应用 NSFW 策略到媒体服务器
        Args:
            enable_nsfw: True 表示允许观看 NSFW (解锁)；False 表示禁止观看 (锁定)
            current_policy: 可选，当前的用户策略对象。
                            - 如果传入 (Toggle场景)，基于它修改，保留其他权限。
                            - 如果不传 (Register场景)，使用空字典，底层模型会自动用默认值填充其他字段。
        """
        # 1. 准备基础策略字典
        if current_policy:
            policy_dict = current_policy.model_dump()
        else:
            policy_dict = {}

        # 2. 修改 NSFW 相关字段
        if enable_nsfw:
            # 允许看 NSFW -> 开启 "EnableAllFolders"，清空排除列表
            policy_dict['EnableAllFolders'] = True
            policy_dict['EnabledFolders'] = []
            if server.server_type == ServerType.EMBY:
                policy_dict['ExcludedSubFolders'] = []

            await client.update_policy(media_user_id, policy_dict, is_none=True)
            return

        # 禁止看 NSFW -> 计算允许的库列表 (白名单模式)
        nsfw_ids = set(server.nsfw_library_ids.split('|')) if server.nsfw_library_ids else set()

        try:
            all_libs = await client.get_libraries() or []
        except HTTPError as e:
            logger.error("在策略应用期间无法获取服务器 {} 的库：{}", server.id, e)
            return

        safe_lib_ids = []
        if server.server_type == ServerType.JELLYFIN:
            safe_lib_ids = [
                lib.ItemId for lib in all_libs
                if lib.ItemId and lib.ItemId not in nsfw_ids
            ]
        else:
            safe_lib_ids = [
                lib.Guid for lib in all_libs
                if lib.Guid and lib.Guid not in nsfw_ids
            ]

        policy_dict['EnableAllFolders'] = False
        policy_dict['EnabledFolders'] = safe_lib_ids

        if server.server_type == ServerType.EMBY:
            # Emby 还需要处理子文件夹排除
            nsfw_sub_ids = server.nsfw_sub_library_ids.split('|') if server.nsfw_sub_library_ids else []
            policy_dict['ExcludedSubFolders'] = nsfw_sub_ids

        await client.update_policy(media_user_id, policy_dict, is_none=True)

    async def renew(self, user_id: int, server_id: int, use_score: bool) -> Result:
        """续期
        Args:
            user_id (int): 用户的 Telegram ID
        """
        media_user = await self.media_repo.get_by_id(user_id, server_id)
        if media_user is None:
            return Result(False, "您尚未注册，请先注册后再续期。")

        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器配置已失效。")

        client = self.media_clients.get(server_id)
        if not client:
            return Result(False, f"客户端未运行: {server.name}")

        media_info = await client.get_user_info(media_user.media_id)
        if not isinstance(media_info, User):
            return Result(False, "续期失败，无法获取您的账户信息，请联系管理员。")

        if media_user.expires_at > datetime.now() + timedelta(days=7):
            return Result(False, f"续期失败，您的账户有效期还有 {(media_user.expires_at - datetime.now()).days} 天，无需续期。")

        if use_score:
            user = await self.telegram_repo.get_or_create(user_id)
            renew_score = int(await self.telegram_repo.get_renew_score())
            if user.score < renew_score:
                return Result(False, f"续期失败，您的积分不足，续期需要 {renew_score} 积分。")
            await self.telegram_repo.update_score(user_id, -renew_score)

        media_user = await self.media_repo.extend_expiry(media_user, server.registration_expiry_days)
        if media_info.Policy.IsDisabled:
            await client.ban_or_unban(media_user.media_id, is_ban=False)

        return Result(
            True,
            f"续期成功，您的 {server.name} 账户已延长至 {media_user.expires_at.strftime('%Y-%m-{} %H:%M:{}')}。"
        )

    async def redeem_code(self, user_id: int, username: str | None | Literal[False], code_str: str) -> Result:
        """使用注册码或续期码注册或续期
        Args:
            user_id (int): 用户的 Telegram ID
            code_str (str): 注册码或续期码
        """
        code = await self.code_repo.get_by_code(code_str)
        if not code or code.used_at or code.expires_at < datetime.now():
            return Result(False, "无效的注册码或续期码，请检查后重试。")

        server = await self.server_repo.get_by_id(code.server_id)
        if not server:
            return Result(False, "该激活码对应的服务器已失效或被删除，无法使用。")

        if code.type == 'signup':
            result = await self.register(user_id, username, code.server_id)
        elif code.type == 'renew':
            result = await self.renew(user_id, code.server_id, False)
        else:
            return Result(False, "无效的码类型，请联系管理员。")

        if result.success:
            await self.code_repo.mark_used(code)
        return result

    async def generate_code(self, user_id: int, code_type: str, server_id: int) -> Result:
        """生成注册码或续期码
        Args:
            user_id (int): 用户的 Telegram ID
            code_type (str): 码类型，'signup' 或 'renew'
        """
        if code_type not in ('signup', 'renew'):
            return Result(False, "无效的码类型")

        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在。")

        user = await self.telegram_repo.get_or_create(user_id)
        if user.is_admin:
            expires = None
            score = 0
        else:
            expires = server.code_expiry_days
            score = int(await self.telegram_repo.get_renew_score())

        if user.score < score:
            return Result(False, f"生成失败，您的积分不足，生成码需要 **{score}** 积分。")

        code = await self.code_repo.create(code_type, expires, server_id)
        await self.telegram_repo.update_score(user_id, -score)

        type_cn = "注册码" if code_type == 'signup' else "续期码"
        return Result(True, textwrap.dedent(f"""\
            ✅ **{type_cn}生成成功**
            
            服务器: `{server.name}`
            代码: `{code.code}`
            过期时间: {code.expires_at.strftime('%Y-%m-%d')}
            
            请妥善保管此码，祝您观影愉快！
        """))

    async def toggle_nsfw_policy(self, user_id: int, server_id: int) -> Result:
        """切换用户的 NSFW 策略
        Args:
            user_id (int): 用户的 Telegram ID
        """
        media_user = await self.media_repo.get_by_id(user_id, server_id)
        if media_user is None:
            return Result(False, "您尚未注册，请先注册后再设置。")

        server = await self.server_repo.get_by_id(server_id)
        if not server or not server.is_enabled:
            return Result(False, "该服务器不存在或已停用。")

        client = self.media_clients.get(server_id)
        if not client:
            return Result(False, "服务器连接失败。")

        media_info = await client.get_user_info(media_user.media_id)
        if not isinstance(media_info, User):
            return Result(False, "操作失败，无法获取您的账户信息，请联系管理员。")

        policy = media_info.Policy

        nsfw_ids = set(server.nsfw_library_ids.split('|')) if server.nsfw_library_ids else set()

        is_unlocked = policy.EnableAllFolders or any(lid in policy.EnabledFolders for lid in nsfw_ids)

        target_enable_nsfw = not is_unlocked

        await self._apply_nsfw_policy(
            client=client,
            media_user_id=media_user.media_id,
            server=server,
            enable_nsfw=target_enable_nsfw,
            current_policy=policy
        )

        action_text = "开启" if target_enable_nsfw else "关闭"
        return Result(True, f"已 {action_text} 您的 NSFW 权限 (服务器: {server.name})。")

    async def forget_password(self, user_id: int, server_id: int):
        """重置密码
        Args:
            user_id (int): 用户的 Telegram ID
        """
        media_user = await self.media_repo.get_by_id(user_id, server_id)
        if media_user is None:
            return Result(False, "您尚未注册，请先注册后再重置密码。")

        client = self.media_clients.get(server_id)
        if not client:
            return Result(False, "服务器连接失败。")

        media_info = await client.get_user_info(media_user.media_id)
        if not isinstance(media_info, User):
            return Result(False, "操作失败，无法获取您的账户信息，请联系管理员。")

        try:
            passwd = await client.post_password(media_user.media_id)
            return Result(True, textwrap.dedent(f"""\
                密码重置成功！您的新密码是: `{passwd}`
                请尽快登录并修改密码，祝您观影愉快！
            """))
        except HTTPError:
            return Result(False, "请稍后重试或寻求管理员帮助")

    async def delete_account(self, user_id: int, server_id: int) -> Result:
        """删除账户
        Args:
            user_id (int): 用户的 Telegram ID
        """
        media_user = await self.media_repo.get_by_id(user_id, server_id)
        if media_user is None:
            return Result(False, "未找到该账户")

        client = self.media_clients.get(server_id)
        if not client:
            return Result(False, "服务器连接失败。")

        try:
            await client.delete_user(media_user.media_id)
        except HTTPError:
            logger.error("删除账户失败: user_id={}, server_id={}", user_id, server_id)
            return Result(False, "账户删除失败，请联系管理员。")

        await self.media_repo.delete(media_user)
        return Result(True, "账户已成功删除。")
