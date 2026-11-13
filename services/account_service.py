import base64
import ipaddress
import json
import re
import socket
import textwrap
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI
from httpx2 import AsyncClient, HTTPError, HTTPStatusError, RequestError
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
        self.app = app
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

        if parsed.scheme not in ("http", "https"):
            return False

        try:
            addr_infos = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror:
            return False

        if not addr_infos:
            return False

        for addr_info in addr_infos:
            ip = ipaddress.ip_address(addr_info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_reserved
                or ip.is_link_local
                or ip.is_multicast
            ):
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
        if (parsed_url.hostname or "").lower() != (
            parsed_prefix.hostname or ""
        ).lower():
            return False

        if parsed_url.port != parsed_prefix.port:
            return False

        prefix_path = parsed_prefix.path or "/"
        url_path = parsed_url.path or "/"
        if not url_path.startswith(prefix_path):
            return False

        return True

    async def verify_external_user(self, server_id: int, user_input: str) -> Result:
        """执行外部验证"""
        server = await self.server_repo.get_by_id(server_id)
        if not server or not server.registration_external_url:
            return Result(False, "服务器配置错误：缺少外部验证链接。")

        prefixes = [
            url.strip()
            for url in server.registration_external_url.split("|")
            if url.strip()
        ]
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
            if any(c in user_input for c in ("@", "\\", "\n", "\r")):
                return Result(False, "输入包含非法字符。")
            if not prefixes:
                return Result(False, "服务器未配置有效的验证前缀。")
            target_url = f"{prefixes[0]}{user_input}"

        if not self._is_safe_url(target_url):
            logger.warning("外部验证被阻止，目标地址不安全: {}", target_url)
            return Result(False, "验证请求被拒绝：目标地址不允许。")

        try:
            headers = {"User-Agent": "TellyMeta/1.0"}
            async with AsyncClient(
                timeout=10.0, follow_redirects=False, headers=headers
            ) as client:
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
                        expr = env.compile_expression(
                            server.registration_external_parser
                        )
                        is_valid = expr(**context)
                        if is_valid:
                            return Result(True, "验证通过")
                        else:
                            return Result(False, "验证失败 (解析未通过)。")
                    except (
                        NameError,
                        TypeError,
                        ValueError,
                        SyntaxError,
                        AttributeError,
                    ) as e:
                        logger.error(f"外部验证解析代码执行错误: {e}")
                        return Result(False, f"验证解析出错: {e}")
                else:
                    if response.is_success:
                        return Result(True, "验证通过")
                    else:
                        return Result(
                            False, f"验证失败 (Status: {response.status_code})。"
                        )
        except RequestError as e:
            logger.error("外部验证错误：{}", e)
            return Result(False, f"验证请求发生网络错误: {str(e)}")

    async def register(
        self,
        user_id: int,
        username: str | None | Literal[False],
        server_id: int,
        skip_checks: bool = False,
    ) -> Result:
        """注册新用户
        Args:
            user_id (int): 用户的 Telegram ID
            username (str): 用户的 Telegram 用户名
            server_id (int): 注册服务器 id
            skip_checks (bool): 跳过注册模式检测
        """
        if not username:
            return Result(False, "请先设置 Telegram 用户名，然后再尝试注册。")

        server = await self.server_repo.get_by_id(server_id)
        if not server or not server.is_enabled:
            return Result(False, "该服务器不存在或已停用。")

        if await self.media_repo.get_by_id(user_id, server_id):
            return Result(False, f"您已经在 **{server.name}** 注册过了，无需重复注册。")

        media_service: MediaService | None = self.media_clients.get(server_id)
        if not media_service:
            return Result(False, "服务器连接实例未找到，请联系管理员。")

        can_register = False
        mode = server.registration_mode
        reserved_score = 0
        reserved_count = False

        if skip_checks:
            can_register = True
        elif mode == RegistrationMode.COUNT:
            if server.registration_count_limit > 0:
                can_register = True
                await self.server_repo.update_policy_config(
                    server.id, count=server.registration_count_limit - 1
                )
                reserved_count = True
            else:
                await self.server_repo.update_policy_config(
                    server.id, mode=RegistrationMode.DEFAULT
                )
                return Result(False, "该服务器注册名额已满。")
        elif mode == RegistrationMode.TIME:
            limit_time = float(server.registration_time_limit)
            if limit_time > datetime.now().timestamp():
                can_register = True
            else:
                await self.server_repo.update_policy_config(
                    server.id, mode=RegistrationMode.DEFAULT
                )
                return Result(False, "该服务器开放注册时间已截止。")
        elif mode == RegistrationMode.OPEN:
            can_register = True
        elif mode == RegistrationMode.DEFAULT:
            user = await self.telegram_repo.get_or_create(user_id)
            register_score = int(await self.telegram_repo.get_renew_score())
            if user.score >= register_score:
                can_register = True
                await self.telegram_repo.update_score(user_id, -register_score)
                reserved_score = register_score
            else:
                return Result(
                    False, f"您的积分不足，注册该服务器需要 **{register_score}** 积分。"
                )
        else:
            return Result(False, "该服务器当前未开放注册。")

        if not can_register:
            return Result(False, "注册失败，当前未开放注册或您不满足注册条件。")

        created_media_id: str | None = None
        try:
            media_user_dto, pw = await media_service.create(username)
            if not media_user_dto:
                await self._rollback_registration_cost(
                    user_id, server_id, reserved_score, reserved_count
                )
                return Result(False, "注册失败，无法创建账户，请联系管理员。")

            media_user_id: str = media_user_dto.Id
            created_media_id = media_user_id

            # 先应用初始权限/NSFW策略，若失败则尚未写入本地记录，可直接清理外部账户
            if not server.nsfw_enabled:
                await self._apply_nsfw_policy(
                    media_service, media_user_id, server, enable_nsfw=False
                )
            else:
                await media_service.update_policy(
                    media_user_id, {"EnableAllFolders": True}, is_none=True
                )

            expires_at = server.registration_expiry_days
            media_user = await self.media_repo.create(
                user_id=user_id,
                server_id=server_id,
                media_id=media_user_id,
                media_name=username,
                expires_at=expires_at,
            )

            return Result(
                True,
                textwrap.dedent(f"""\
                🎉 **注册成功！**
                
                服务器: `{server.name}`
                地址: `{server.url}`
                用户名: `{username}`
                密码: `{pw}`
                
                有效期至: {media_user.expires_at.strftime('%Y-%m-%d')}
                请尽快登录并修改密码，祝您观影愉快！
            """),
            )
        except Exception as e:
            logger.error("{}: {} 注册失败: {}", username, user_id, e)
            await self.media_repo.session.rollback()

            cleanup_ok = True
            if created_media_id is not None:
                cleanup_ok = await self._cleanup_orphan_media_user(
                    media_service, server_id, user_id, username, created_media_id
                )

            await self._rollback_registration_cost(
                user_id, server_id, reserved_score, reserved_count
            )

            if not cleanup_ok:
                return Result(
                    False,
                    "注册失败，本地记录保存或初始化异常且外部账户清理未完成，请联系管理员核对处理。",
                )
            return Result(False, "注册失败，请联系管理员")

    async def _rollback_registration_cost(
        self,
        user_id: int,
        server_id: int,
        reserved_score: int,
        reserved_count: bool,
    ) -> None:
        """全额恢复注册失败时本次占用的积分或名额。

        Args:
            user_id (int): 用户的 Telegram ID
            server_id (int): 服务器 ID
            reserved_score (int): 本次扣减的积分数
            reserved_count (bool): 本次是否扣减了名额
        """
        if reserved_score > 0:
            await self.telegram_repo.update_score(user_id, reserved_score)
        if reserved_count:
            latest_server = await self.server_repo.get_by_id(server_id)
            if latest_server:
                await self.server_repo.update_policy_config(
                    server_id, count=latest_server.registration_count_limit + 1
                )

    async def _cleanup_orphan_media_user(
        self,
        media_service: MediaService,
        server_id: int,
        user_id: int,
        username: str,
        media_id: str,
    ) -> bool:
        """清理或协调外部建号成功但本地保存/初始化失败遗留的外部账户。

        若外部删除成功或返回 404（已不存在），返回 True；
        若外部删除失败，尝试禁用该账户并记录核对日志后返回 False。
        """
        try:
            await media_service.delete_user(media_id)
            logger.info(
                "注册失败已清理外部媒体账户: server_id={}, user_id={}, media_id={}",
                server_id,
                user_id,
                media_id,
            )
            return True
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info(
                    "注册失败清理时外部账户已不存在(404): server_id={}, user_id={}, media_id={}",
                    server_id,
                    user_id,
                    media_id,
                )
                return True
            logger.error(
                "清理外部孤儿账户失败: server_id={}, user_id={}, username={}, media_id={}, status={}",
                server_id,
                user_id,
                username,
                media_id,
                e.response.status_code,
            )
        except Exception as e:
            logger.error(
                "清理外部孤儿账户失败: server_id={}, user_id={}, username={}, media_id={}, err={}",
                server_id,
                user_id,
                username,
                media_id,
                e,
            )

        # 外部删除失败时，先禁用该账户，避免留下可正常使用的未纳管账户
        try:
            await media_service.ban_or_unban(media_id, is_ban=True)
            logger.warning(
                "外部孤儿账户删除失败，已先行禁用等待核对: server_id={}, user_id={}, media_id={}",
                server_id,
                user_id,
                media_id,
            )
        except Exception as ban_err:
            logger.critical(
                "外部孤儿账户删除与禁用均失败，请管理员立即核对: server_id={}, user_id={}, username={}, media_id={}, err={}",
                server_id,
                user_id,
                username,
                media_id,
                ban_err,
            )
        return False

    async def _apply_nsfw_policy(
        self,
        client: MediaService,
        media_user_id: str,
        server: ServerInstance,
        enable_nsfw: bool,
        current_policy: Any | None = None,
        all_libs: Sequence[Any] | None = None,
    ) -> None:
        """辅助方法：应用 NSFW 策略到媒体服务器
        Args:
            enable_nsfw: True 表示允许观看 NSFW (解锁)；False 表示禁止观看 (锁定)
            current_policy: 可选，当前的用户策略对象。
                            - 如果传入 (Toggle场景)，基于它修改，保留其他权限。
                            - 如果不传 (Register场景)，使用空字典，底层模型会自动用默认值填充其他字段。
            all_libs: 可选，预先获取的媒体库列表，避免批量撤权时重复请求。
        """
        # 1. 准备基础策略字典
        if current_policy:
            policy_dict = current_policy.model_dump()
        else:
            policy_dict = {}

        # 2. 修改 NSFW 相关字段
        if enable_nsfw:
            # 允许看 NSFW -> 开启 "EnableAllFolders"，清空排除列表
            policy_dict["EnableAllFolders"] = True
            policy_dict["EnabledFolders"] = []
            if server.server_type == ServerType.EMBY:
                policy_dict["ExcludedSubFolders"] = []

            await client.update_policy(media_user_id, policy_dict, is_none=True)
            return

        # 禁止看 NSFW -> 计算允许的库列表 (白名单模式)
        nsfw_ids = (
            set(server.nsfw_library_ids.split("|"))
            if server.nsfw_library_ids
            else set()
        )

        if all_libs is None:
            all_libs = await client.get_libraries() or []

        safe_lib_ids = []
        if server.server_type == ServerType.JELLYFIN:
            safe_lib_ids = [
                lib.ItemId
                for lib in all_libs
                if lib.ItemId and lib.ItemId not in nsfw_ids
            ]
        else:
            safe_lib_ids = [
                lib.Guid for lib in all_libs if lib.Guid and lib.Guid not in nsfw_ids
            ]

        policy_dict["EnableAllFolders"] = False
        policy_dict["EnabledFolders"] = safe_lib_ids

        if server.server_type == ServerType.EMBY:
            # Emby 还需要处理子文件夹排除
            nsfw_sub_ids = (
                server.nsfw_sub_library_ids.split("|")
                if server.nsfw_sub_library_ids
                else []
            )
            policy_dict["ExcludedSubFolders"] = nsfw_sub_ids

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

        try:
            media_info = await client.get_user_info(media_user.media_id)
        except HTTPError:
            return Result(False, "续期失败，无法获取您的账户信息，请联系管理员。")
        if not isinstance(media_info, User):
            return Result(False, "续期失败，无法获取您的账户信息，请联系管理员。")

        if media_user.expires_at > datetime.now() + timedelta(days=7):
            return Result(
                False,
                f"续期失败，您的账户有效期还有 {(media_user.expires_at - datetime.now()).days} 天，无需续期。",
            )

        renew_score = 0
        if use_score:
            user = await self.telegram_repo.get_or_create(user_id)
            renew_score = int(await self.telegram_repo.get_renew_score())
            if user.score < renew_score:
                return Result(
                    False, f"续期失败，您的积分不足，续期需要 {renew_score} 积分。"
                )

        # 宽限期内续期时，先在媒体服务器上恢复账户，再清除待删除状态与扣减积分
        if media_info.Policy.IsDisabled or media_user.is_banned:
            try:
                await client.ban_or_unban(media_user.media_id, is_ban=False)
            except (HTTPError, ValueError) as e:
                logger.error(
                    "续期恢复媒体账户失败: user_id={}, server_id={}, err={}",
                    user_id,
                    server_id,
                    e,
                )
                return Result(False, "续期失败，恢复媒体账户状态时出错，请联系管理员。")

        if use_score:
            await self.telegram_repo.update_score(user_id, -renew_score)

        media_user = await self.media_repo.extend_expiry(
            media_user, server.registration_expiry_days
        )

        return Result(
            True,
            f"续期成功，您的 {server.name} 账户已延长至 {media_user.expires_at.strftime('%Y-%m-{} %H:%M:{}')}。",
        )

    async def redeem_code(
        self, user_id: int, username: str | None | Literal[False], code_str: str
    ) -> Result:
        """使用注册码或续期码注册或续期
        Args:
            user_id (int): 用户的 Telegram ID
            code_str (str): 注册码或续期码
        """
        claimed = await self.code_repo.try_claim(code_str)
        if not claimed or claimed.used_at is None:
            return Result(False, "无效的注册码或续期码，请检查后重试。")

        # 提取纯值，避免后续 session.commit()/rollback() 导致 ORM 对象过期
        claimed_id = claimed.id
        claimed_used_at = claimed.used_at
        code_type = claimed.type
        code_server_id = claimed.server_id

        try:
            server = await self.server_repo.get_by_id(code_server_id)
            if not server:
                await self.code_repo.release_claim(claimed_id, claimed_used_at)
                return Result(False, "该激活码对应的服务器已失效或被删除，无法使用。")

            if code_type == "signup":
                result = await self.register(user_id, username, code_server_id, True)
            elif code_type == "renew":
                result = await self.renew(user_id, code_server_id, False)
            else:
                await self.code_repo.release_claim(claimed_id, claimed_used_at)
                return Result(False, "无效的码类型，请联系管理员。")
        except Exception as e:
            logger.error(
                "激活码兑换异常，释放占用: code_id={}, code={}, user_id={}, err={}",
                claimed_id,
                code_str,
                user_id,
                e,
            )
            await self.code_repo.session.rollback()
            await self.code_repo.release_claim(claimed_id, claimed_used_at)
            return Result(False, "兑换过程中发生错误，请稍后重试。")

        if not result.success:
            await self.code_repo.release_claim(claimed_id, claimed_used_at)
            return result

        logger.info(
            "激活码兑换成功: code_id={}, code={}, type={}, server_id={}, user_id={}, used_at={}",
            claimed_id,
            code_str,
            code_type,
            code_server_id,
            user_id,
            claimed_used_at.isoformat(),
        )
        return result

    async def generate_code(
        self, user_id: int, code_type: str, server_id: int
    ) -> Result:
        """生成注册码或续期码
        Args:
            user_id (int): 用户的 Telegram ID
            code_type (str): 码类型，'signup' 或 'renew'
        """
        if code_type not in ("signup", "renew"):
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
            return Result(
                False, f"生成失败，您的积分不足，生成码需要 **{score}** 积分。"
            )

        code = await self.code_repo.create(code_type, expires, server_id)
        await self.telegram_repo.update_score(user_id, -score)

        type_cn = "注册码" if code_type == "signup" else "续期码"
        return Result(
            True,
            textwrap.dedent(f"""\
            ✅ **{type_cn}生成成功**
            
            服务器: `{server.name}`
            代码: `{code.code}`
            过期时间: {code.expires_at.strftime('%Y-%m-%d')}
            
            请妥善保管此码，祝您观影愉快！
        """),
        )

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

        nsfw_ids = (
            set(server.nsfw_library_ids.split("|"))
            if server.nsfw_library_ids
            else set()
        )

        is_unlocked = policy.EnableAllFolders or any(
            lid in policy.EnabledFolders for lid in nsfw_ids
        )

        target_enable_nsfw = not is_unlocked

        # 管理员关闭服务器 NSFW 开关后，成员自助接口不得再授予 NSFW 权限
        if not server.nsfw_enabled and target_enable_nsfw:
            return Result(False, "该服务器当前未开放 NSFW 权限，无法开启。")

        try:
            await self._apply_nsfw_policy(
                client=client,
                media_user_id=media_user.media_id,
                server=server,
                enable_nsfw=target_enable_nsfw,
                current_policy=policy,
            )
        except HTTPError as e:
            logger.error(
                "切换 NSFW 策略失败: user_id={}, server_id={}, err={}",
                user_id,
                server_id,
                e,
            )
            return Result(False, "操作失败，无法更新媒体服务器权限，请稍后重试。")

        action_text = "开启" if target_enable_nsfw else "关闭"
        return Result(
            True, f"已 {action_text} 您的 NSFW 权限 (服务器: {server.name})。"
        )

    def _get_pending_nsfw_revokes(self) -> dict[int, set[str]]:
        """获取挂载在 app.state 上的待重试 NSFW 撤权映射 (server_id -> set[media_id])。"""
        pending = getattr(self.app.state, "_pending_nsfw_revokes", None)
        if pending is None:
            pending = {}
            self.app.state._pending_nsfw_revokes = pending
        return pending

    async def revoke_server_nsfw(
        self, server_id: int, target_media_ids: Sequence[str] | None = None
    ) -> tuple[int, int]:
        """撤销指定服务器上已有媒体账户的 NSFW 授权。

        外部服务器暂不可用或单账户撤权失败时，将失败的 media_id 记录到待重试队列，
        供定时任务后续重试，不将未撤权账户误报为已撤权。

        Args:
            server_id (int): 服务器 ID
            target_media_ids (Sequence[str] | None): 指定需要重试撤权的 media_id 列表；为 None 时扫描全服账户

        Returns:
            tuple[int, int]: (本次确认已撤权数量, 本次撤权失败/待重试数量)
        """
        pending = self._get_pending_nsfw_revokes()
        server = await self.server_repo.get_by_id(server_id)
        if not server or server.nsfw_enabled:
            pending.pop(server_id, None)
            return 0, 0

        if target_media_ids is not None:
            media_ids = list(dict.fromkeys(target_media_ids))
        else:
            media_users = await self.media_repo.get_all_by_server_id(server_id)
            media_ids = [mu.media_id for mu in media_users]

        if not media_ids:
            pending.pop(server_id, None)
            return 0, 0

        client = self.media_clients.get(server_id)
        if not client:
            pending.setdefault(server_id, set()).update(media_ids)
            logger.warning(
                "服务器 {} 客户端未连接，{} 个账户的 NSFW 撤权已加入待重试队列",
                server_id,
                len(media_ids),
            )
            return 0, len(media_ids)

        try:
            all_libs = await client.get_libraries() or []
        except Exception as e:
            pending.setdefault(server_id, set()).update(media_ids)
            logger.warning(
                "获取服务器 {} 媒体库失败，{} 个账户的 NSFW 撤权已加入待重试队列: {}",
                server_id,
                len(media_ids),
                e,
            )
            return 0, len(media_ids)

        revoked_count = 0
        failed_count = 0
        server_pending = pending.setdefault(server_id, set())

        for media_id in media_ids:
            try:
                media_info = await client.get_user_info(media_id)
            except HTTPStatusError as e:
                if e.response.status_code == 404:
                    server_pending.discard(media_id)
                    revoked_count += 1
                    continue
                server_pending.add(media_id)
                failed_count += 1
                logger.error(
                    "撤权获取用户信息失败: server_id={}, media_id={}, status={}",
                    server_id,
                    media_id,
                    e.response.status_code,
                )
                continue
            except Exception as e:
                server_pending.add(media_id)
                failed_count += 1
                logger.error(
                    "撤权获取用户信息失败: server_id={}, media_id={}, err={}",
                    server_id,
                    media_id,
                    e,
                )
                continue

            if not isinstance(media_info, User):
                server_pending.add(media_id)
                failed_count += 1
                continue

            try:
                await self._apply_nsfw_policy(
                    client=client,
                    media_user_id=media_id,
                    server=server,
                    enable_nsfw=False,
                    current_policy=media_info.Policy,
                    all_libs=all_libs,
                )
                server_pending.discard(media_id)
                revoked_count += 1
            except Exception as e:
                server_pending.add(media_id)
                failed_count += 1
                logger.error(
                    "撤销账户 NSFW 权限失败: server_id={}, media_id={}, err={}",
                    server_id,
                    media_id,
                    e,
                )

        if not server_pending:
            pending.pop(server_id, None)

        return revoked_count, failed_count

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
            return Result(
                True,
                textwrap.dedent(f"""\
                密码重置成功！您的新密码是: `{passwd}`
                请尽快登录并修改密码，祝您观影愉快！
            """),
            )
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
        except HTTPStatusError as e:
            if e.response.status_code != 404:
                logger.error(
                    "删除账户失败: user_id={}, server_id={}, status={}",
                    user_id,
                    server_id,
                    e.response.status_code,
                )
                return Result(False, "账户删除失败，请联系管理员。")
            logger.info(
                "媒体服务器账户已不存在(404)，清理本地记录: user_id={}, server_id={}",
                user_id,
                server_id,
            )
        except HTTPError:
            logger.error("删除账户失败: user_id={}, server_id={}", user_id, server_id)
            return Result(False, "账户删除失败，请联系管理员。")

        await self.media_repo.delete(media_user)
        return Result(True, "账户已成功删除。")
