import base64
import ipaddress
import json
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI
from httpx2 import AsyncClient, HTTPError, HTTPStatusError, RequestError
from jinja2.sandbox import SandboxedEnvironment
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio.session import AsyncSession

from core.config import get_settings
from core.telegram_message import TelegramMessage
from models.nginx import format_traffic_bytes
from models.orm import MediaUser, RegistrationMode, ServerInstance, ServerType
from models.protocols import User
from repositories.code_repo import CodeRepository
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.media_service import (
    MediaService,
    compute_external_disabled_state,
    sync_external_disabled_state,
)
from services.notification_service import NotificationService
from services.user_service import Result
from services.watch_time_service import evaluate_watch_time_renewal, get_account_lock

settings = get_settings()


def format_traffic_release_summary(
    released_count: int, failed_count: int, *, as_suffix: bool = False
) -> str:
    """统一格式化关闭流量或超流自动停用开关后的账号释放结果说明

    Args:
        released_count: 本次成功清除或释放的超流停用账号数量。
        failed_count: 外部媒体服务器解封失败、待后续重试的账号数量。
        as_suffix: 为 ``True`` 时返回拼接在单服更新消息后的后缀；为 ``False`` 时返回完整全局提示。

    Returns:
        格式化后的提示文本；当没有任何超流停用账号受影响时返回空字符串。
    """
    if failed_count > 0:
        if as_suffix:
            return f"；已处理 {released_count} 个超流停用账号，另有 {failed_count} 个外部解封失败，请重试"
        return (
            f"已关闭全局流量开关并释放 {released_count} 个超流停用账号，"
            f"另有 {failed_count} 个账号外部解封失败，请检查媒体服务器后重试。"
        )
    if released_count > 0:
        if as_suffix:
            return f"；已自动释放 {released_count} 个超流停用账号（历史流量已保留）"
        return f"已关闭全局流量开关，并安全释放 {released_count} 个超流停用账号（历史流量已永久保留）。"
    return ""


class _RenewChannel(StrEnum):
    """续期通道枚举，区分激活码续期、时长保活免积分续期与积分扣费续期"""

    CODE = "code"
    WATCH_TIME = "watch_time"
    SCORE = "score"


@dataclass(slots=True, frozen=True)
class AutoRenewOutcome:
    """自动保活单账号执行结果，显式区分成功与外部临时可重试故障"""

    renewed: bool
    message: str
    retryable_error: bool = False
    expires_at: datetime | None = None


class AccountService:
    def __init__(
        self,
        app: FastAPI,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化媒体账号业务服务

        Args:
            app: FastAPI 应用实例，用于读取运行期媒体客户端映射
            session: 异步数据库会话
            clock: 可选的当前时间提供器，便于确定性测试自然月周期推算
        """
        self.app = app
        self.config_repo = ConfigRepository(session)
        self.telegram_repo = TelegramRepository(session)
        self.code_repo = CodeRepository(session)
        self.media_repo = MediaRepository(session)
        self.server_repo = ServerRepository(session)
        self.media_clients: dict[int, MediaService] = app.state.media_clients
        self._now: Callable[[], datetime] = clock or datetime.now

    @staticmethod
    def _get_server_expiry_months(server: ServerInstance) -> int:
        """获取服务器配置的有效自然月数，非正整数时回退至默认 1 个月"""
        return max(1, server.registration_expiry_months)

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
        return url_path.startswith(prefix_path)

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

        if user_input.startswith(("http://", "https://")):
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
                        return Result(False, "验证失败 (解析未通过)。")
                    except (
                        NameError,
                        TypeError,
                        ValueError,
                        SyntaxError,
                        AttributeError,
                    ) as e:
                        logger.error(t"外部验证解析代码执行错误: {e}")
                        return Result(False, f"验证解析出错: {e}")
                else:
                    if response.is_success:
                        return Result(True, "验证通过")
                    return Result(False, f"验证失败 (Status: {response.status_code})。")
        except RequestError as e:
            logger.error("外部验证错误：{}", e)
            return Result(False, f"验证请求发生网络错误: {e!s}")

    async def register(
        self,
        user_id: int,
        username: str | Literal[False] | None,
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
            return Result(
                False,
                TelegramMessage.render_markdown(
                    t"您已经在 **{server.name}** 注册过了，无需重复注册。"
                ),
            )

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

            media_user = await self.media_repo.create(
                user_id=user_id,
                server_id=server_id,
                media_id=media_user_id,
                media_name=username,
                expiry_months=self._get_server_expiry_months(server),
                now=self._now(),
            )

            return Result(
                True,
                TelegramMessage.render_markdown(t"""\
                🎉 **注册成功！**

                服务器: `{server.name}`
                地址: `{server.url}`
                用户名: `{username}`
                密码: `{pw}`

                有效期至: {media_user.expires_at.strftime("%Y-%m-%d")}
                请尽快登录并修改密码，祝您观影愉快！
            """),
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
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
        except (HTTPError, ValueError) as e:
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
        except (HTTPError, ValueError) as ban_err:
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
        policy_dict = current_policy.model_dump() if current_policy else {}

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
        """按服务器有效自然月数与锚定自然月周期续期指定媒体账号

        优先判定时长保活资格（启用时长保活且本周期有效播放时长达标时免积分续期，
        不受积分续期开关或余额限制）；未达标或未开启保活时，回退至积分续期
        （全额扣分、纯门槛不打折）或激活码续期通道。
        副作用：必要时恢复外部媒体服务器启用状态、按需扣减用户积分、延长锚定周期并清零本期保活时长。

        Args:
            user_id: Telegram 用户 ID
            server_id: 目标媒体服务器 ID
            use_score: 保留既有方法签名以兼容全部调用方；``True`` 表示自助续期
                （优先走时长保活，不满足时回退积分扣费），``False`` 表示激活码兑换续期

        Returns:
            续期操作结果 ``Result``，携带成功状态与通知文案
        """
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            media_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if media_user is None:
                return Result(False, "您尚未注册，请先注册后再续期。")

            server = await self.server_repo.get_by_id(server_id, fresh=True)
            if not server:
                return Result(False, "服务器配置已失效。")

            wt_status = evaluate_watch_time_renewal(server, media_user)
            if not use_score:
                renew_channel = _RenewChannel.CODE
            elif wt_status.is_eligible:
                # 达标保活续期完全独立于积分开关与余额，且遵循 ADR-0001 绝不向钱包发分
                renew_channel = _RenewChannel.WATCH_TIME
            else:
                renew_channel = _RenewChannel.SCORE
                allowed, reason = await self.config_repo.check_points_feature(
                    ConfigRepository.KEY_ENABLE_POINTS_RENEW
                )
                if not allowed:
                    return Result(False, reason)

            client = self.media_clients.get(server_id)
            if not client:
                return Result(
                    False,
                    TelegramMessage.render_markdown(t"客户端未运行: {server.name}"),
                )

            media_info, _ = await self._fetch_external_user(client, media_user.media_id)
            if media_info is None:
                return Result(False, "续期失败，无法获取您的账户信息，请联系管理员。")

            now = self._now()
            policy_cfg = await self.config_repo.get_policy_config()
            renew_advance_days = policy_cfg["account_renew_advance_days"]
            if media_user.expires_at > now + timedelta(days=renew_advance_days):
                return Result(
                    False,
                    f"续期失败，您的账户有效期还有 {(media_user.expires_at - now).days} 天，无需续期。",
                )

            renew_score = 0
            if renew_channel == _RenewChannel.SCORE:
                user = await self.telegram_repo.get_or_create(user_id)
                # 纯门槛不打折：未达时长门槛时按完整续期积分计费
                renew_score = int(await self.telegram_repo.get_renew_score())
                if user.score < renew_score:
                    return Result(
                        False, f"续期失败，您的积分不足，续期需要 {renew_score} 积分。"
                    )

            # 宽限期内续期时，先在媒体服务器上恢复账户，再扣减积分并更新本地周期与清零本期保活时长
            restore_err = await self._restore_external_if_disabled(
                client=client,
                media_info=media_info,
                media_user=media_user,
                action_label="续期",
            )
            if restore_err is not None:
                return Result(False, "续期失败，恢复媒体账号状态时出错，请联系管理员。")

            if renew_channel == _RenewChannel.SCORE and renew_score > 0:
                await self.telegram_repo.update_score(user_id, -renew_score)

            media_user = await self.media_repo.extend_anchored_cycle(
                media_user,
                self._get_server_expiry_months(server),
                now=now,
            )

            expires_str = media_user.expires_at.strftime("%Y-%m-%d %H:%M:%S")
            suspension_note = (
                "（提示：账号当前仍处于超流停用状态，兑换流量包或等待账期重置后可恢复播放）"
                if media_user.is_traffic_suspended
                else ""
            )
            if renew_channel == _RenewChannel.WATCH_TIME:
                return Result(
                    True,
                    TelegramMessage.render_markdown(
                        t"续期成功（时长保活免积分），您的 {server.name} 媒体账号已延长至 {expires_str}。{suspension_note}"
                    ),
                )

            return Result(
                True,
                TelegramMessage.render_markdown(
                    t"续期成功，您的 {server.name} 媒体账号已延长至 {expires_str}。{suspension_note}"
                ),
            )

    async def _fetch_external_user(
        self, client: MediaService, media_id: str
    ) -> tuple[User | None, bool]:
        """向媒体服务器查询外部用户信息并返回 ``(User | None, is_retryable)``

        Args:
            client: 媒体服务器客户端适配器
            media_id: 外部媒体账号 ID

        Returns:
            查询成功返回 ``(User, False)``；失败时返回 ``(None, is_retryable)``，
            其中 HTTP 404 表示外部账号已不存在（不可重试），其余通信异常为可重试故障
        """
        try:
            media_info = await client.get_user_info(media_id)
        except HTTPStatusError as e:
            # 外部返回 404 说明账号已不存在，无需阻止后续过期封禁的 404 本地清理流程
            return None, e.response.status_code != 404
        except HTTPError:
            return None, True
        if not isinstance(media_info, User):
            return None, True
        return media_info, False

    async def _restore_external_if_disabled(
        self,
        *,
        client: MediaService,
        media_info: User,
        media_user: MediaUser,
        action_label: str,
    ) -> Exception | None:
        """在续期解除过期封禁时，按组合状态（过期封禁 OR 超流停用）同步外部禁用态

        仅当解除过期封禁后账号不同时处于超流停用时才在媒体服真正解封；
        若仍处于超流停用，则绝不误放并在外部未禁用时补齐禁用状态。

        Args:
            client: 媒体服务器客户端适配器
            media_info: 已获取的外部媒体用户信息
            media_user: 本地媒体账号 ORM 对象
            action_label: 日志场景前缀（如 ``续期`` / ``自动保活``）

        Returns:
            恢复成功或无需恢复时返回 ``None``；外部调用失败时返回异常对象供调用方中止或重试
        """
        is_policy_disabled = bool(media_info.Policy and media_info.Policy.IsDisabled)
        target_disabled = compute_external_disabled_state(
            is_banned=False,
            is_traffic_suspended=bool(media_user.is_traffic_suspended),
        )
        if not target_disabled and not (is_policy_disabled or media_user.is_banned):
            return None
        if target_disabled and is_policy_disabled:
            return None
        outcome = await sync_external_disabled_state(
            client,
            media_id=media_user.media_id,
            target_disabled=target_disabled,
            log_context=(
                f"{action_label}维持超流停用外部禁用态"
                if target_disabled
                else f"{action_label}恢复媒体账号"
            ),
            user_id=media_user.id,
            server_id=media_user.server_id,
        )
        return outcome.error

    async def _send_auto_renew_dm(
        self,
        *,
        user_id: int,
        server_name: str,
        media_name: str,
        expires_at: datetime,
    ) -> bool:
        """向用户发送自动保活成功 Bot 私聊通知

        副作用：尝试通过 Telegram 发送私聊消息；发送失败仅记录告警日志，绝不回滚已完成续期。

        Args:
            user_id: Telegram 用户 ID
            server_name: 媒体服务器名称
            media_name: 媒体账号用户名
            expires_at: 续期后的新到期时间

        Returns:
            私聊通知送达成功返回 ``True``，未配置客户端或发送失败返回 ``False``
        """
        if getattr(self.app.state, "telethon_client", None) is None:
            return False
        exp_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
        msg = TelegramMessage.render_html(t"""\
            ✅ <b>自动保活续期成功</b>

            服务器: <code>{server_name}</code>
            媒体账号: <code>{media_name}</code>
            消耗积分: <code>0</code>（有效播放时长已达标）
            有效期延长至: <code>{exp_str}</code>
        """)
        try:
            ok = await NotificationService(self.app).send_text_to_user(user_id, msg)
            if not ok:
                logger.warning(
                    "自动保活私聊通知送达失败（不影响已完成续期）: user_id={}, server={}",
                    user_id,
                    server_name,
                )
            return ok
        except (HTTPError, ValueError, TypeError, OSError) as e:
            logger.warning(
                "自动保活私聊通知发送异常（不影响已完成续期）: user_id={}, server={}, err={}",
                user_id,
                server_name,
                e,
            )
            return False

    async def try_auto_renew_account(
        self,
        user_id: int,
        server_id: int,
        *,
        pre_expiry_window: timedelta = timedelta(hours=24),
    ) -> AutoRenewOutcome:
        """尝试对单个媒体账号执行临近到期窗口下的时长自动保活

        仅当用户已手动开启 ``auto_renew``、未封禁、对应服务器开启保活且门槛大于 0、
        当前周期有效播放时长达标且已进入临近到期窗口时免积分续期并推送私聊通知（绝不扣减或增发积分）。

        Args:
            user_id: Telegram 用户 ID
            server_id: 目标媒体服务器 ID
            pre_expiry_window: 临近到期判定窗口（默认到期前 24 小时）

        Returns:
            结构化 ``AutoRenewOutcome``，包含续期成功标志及临时通信异常 ``retryable_error`` 标志
        """
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            media_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if media_user is None:
                return AutoRenewOutcome(False, "媒体账号不存在")
            if media_user.is_banned:
                return AutoRenewOutcome(False, "媒体账号已处于过期封禁状态")
            if not media_user.auto_renew:
                return AutoRenewOutcome(False, "用户未开启自动保活")

            server = await self.server_repo.get_by_id(server_id, fresh=True)
            if not server or not server.is_enabled:
                return AutoRenewOutcome(False, "服务器不存在或已停用")

            wt_status = evaluate_watch_time_renewal(server, media_user)
            if not wt_status.is_eligible:
                return AutoRenewOutcome(False, wt_status.status_text)

            now = self._now()
            if media_user.expires_at > now + pre_expiry_window:
                return AutoRenewOutcome(False, "尚未进入临近到期自动保活窗口")

            client = self.media_clients.get(server_id)
            if not client:
                return AutoRenewOutcome(
                    False, f"客户端未运行: {server.name}", retryable_error=True
                )

            media_info, is_retryable = await self._fetch_external_user(
                client, media_user.media_id
            )
            if media_info is None:
                return AutoRenewOutcome(
                    False,
                    "自动保活获取外部媒体账号失败",
                    retryable_error=is_retryable,
                )

            restore_err = await self._restore_external_if_disabled(
                client=client,
                media_info=media_info,
                media_user=media_user,
                action_label="自动保活",
            )
            if restore_err is not None:
                return AutoRenewOutcome(
                    False, "自动保活恢复外部媒体账号状态失败", retryable_error=True
                )

            media_user = await self.media_repo.extend_anchored_cycle(
                media_user,
                self._get_server_expiry_months(server),
                now=now,
            )

            await self._send_auto_renew_dm(
                user_id=user_id,
                server_name=server.name,
                media_name=media_user.media_name,
                expires_at=media_user.expires_at,
            )

            exp_str = media_user.expires_at.strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                "自动保活续期成功: user_id={}, server_id={}, expires_at={}",
                user_id,
                server_id,
                exp_str,
            )
            return AutoRenewOutcome(
                True,
                f"已通过有效播放时长自动保活续期至 {exp_str}",
                expires_at=media_user.expires_at,
            )

    async def process_auto_renewals(
        self,
        *,
        pre_expiry_window: timedelta = timedelta(hours=24),
    ) -> int:
        """批量扫描临近到期窗口内符合自动保活条件的媒体账号并执行免积分续期

        Args:
            pre_expiry_window: 临近到期判定窗口（默认到期前 24 小时）

        Returns:
            本次成功完成自动保活续期的账号数量
        """
        deadline = self._now() + pre_expiry_window
        candidates = await self.media_repo.find_auto_renew_candidates(deadline)
        # 仅提取主键标量，使逐账号进入锁内执行 end_clean_read_tx() + fresh=True 重查时不受此前 ORM 实例或读快照牵连
        target_pairs = [(mu.id, mu.server_id) for mu in candidates]
        renewed_count = 0
        for uid, sid in target_pairs:
            outcome = await self.try_auto_renew_account(
                uid, sid, pre_expiry_window=pre_expiry_window
            )
            if outcome.renewed:
                renewed_count += 1
        return renewed_count

    async def toggle_auto_renew(
        self, user_id: int, server_id: int, enabled: bool | None = None
    ) -> Result:
        """切换或设置指定媒体账号的用户可控自动保活开关

        Args:
            user_id: Telegram 用户 ID
            server_id: 目标媒体服务器 ID
            enabled: 指定目标状态；传入 ``None`` 时在当前布尔值上取反

        Returns:
            包含最新 ``auto_renew`` 状态与提示文案的 ``Result`` 对象
        """
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            media_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if media_user is None:
                return Result(False, "您尚未注册，请先注册后再设置自动保活。")

            server = await self.server_repo.get_by_id(server_id, fresh=True)
            if not server:
                return Result(False, "服务器配置已失效。")

            target_state = (not media_user.auto_renew) if enabled is None else enabled
            media_user = await self.media_repo.set_auto_renew(media_user, target_state)

            wt_status = evaluate_watch_time_renewal(server, media_user)
            if target_state:
                if wt_status.is_open:
                    msg = "已开启自动保活：临近到期且本周期有效播放时长达标时将自动免积分续期。"
                else:
                    msg = (
                        "已开启自动保活（提示：当前服务器未开放保活续期，"
                        "仅在服务器开启保活并设定大于 0 的门槛后生效）。"
                    )
            else:
                msg = "已关闭自动保活。"

            return Result(True, msg, extra_data={"auto_renew": target_state})

    async def _send_traffic_unsuspended_dm(
        self,
        *,
        user_id: int,
        server_name: str,
        media_name: str,
        added_bytes: int,
        total_pack_bytes: int,
    ) -> bool:
        """向因超流停用且兑换流量包后立即恢复播放权限的用户发送 Bot 私聊通知

        副作用：尝试发送 Telegram 私聊提醒；发送失败仅记录日志，不影响已完成兑换。

        Args:
            user_id: Telegram 用户 ID。
            server_name: 媒体服务器名称。
            media_name: 媒体账号用户名。
            added_bytes: 本次兑换增加的流量包字节数。
            total_pack_bytes: 兑换后的流量包总余额字节数。

        Returns:
            私聊送达成功返回 ``True``，未配置客户端或送达失败返回 ``False``。
        """
        if getattr(self.app.state, "telethon_client", None) is None:
            return False
        added_str = format_traffic_bytes(added_bytes)
        total_pack_str = format_traffic_bytes(total_pack_bytes)
        msg = TelegramMessage.render_html(t"""\
            ✅ <b>流量包兑换成功，超流停用已解除</b>

            服务器: <code>{server_name}</code>
            媒体账号: <code>{media_name}</code>
            本次增加: <code>+{added_str}</code>
            流量包余额: <code>{total_pack_str}</code>
            您的播放权限已立即恢复，祝观影愉快！
        """)
        try:
            return await NotificationService(self.app).send_text_to_user(user_id, msg)
        except (HTTPError, ValueError, TypeError, OSError) as exc:
            logger.warning(
                "发送流量包解封私聊通知失败（不影响兑换结果）: user_id={}, server={}, err={}",
                user_id,
                server_name,
                exc,
            )
            return False

    async def redeem_traffic_pack(
        self, user_id: int, server_id: int, quantity: int = 1
    ) -> Result:
        """消耗 Telegram 积分兑换指定媒体账号的持久化流量包，并在安全时自动解除超流停用

        受全局积分总开关与“积分兑换流量包”子开关共同控制；仅当账号当时仅因超流停用
        且未处于过期封禁（``is_banned == False``）时才解除外部媒体服务器禁用并通知用户。

        Args:
            user_id: Telegram 用户 ID。
            server_id: 目标媒体服务器 ID。
            quantity: 兑换份数（必须 >= 1）。

        Returns:
            包含成功状态、提示文案及 ``extra_data`` 账务明细的 ``Result`` 对象。
        """
        if quantity < 1:
            return Result(False, "兑换数量至少为 1 份。")

        allowed, reason = await self.config_repo.check_points_feature(
            ConfigRepository.KEY_ENABLE_POINTS_TRAFFIC_PACK
        )
        if not allowed:
            return Result(False, reason)

        traffic_cfg = await self.config_repo.get_traffic_config()
        unit_cost = max(1, int(traffic_cfg["traffic_pack_cost_score"]))
        unit_bytes = max(1, int(traffic_cfg["traffic_pack_unit_bytes"]))
        total_cost = unit_cost * quantity
        added_bytes = unit_bytes * quantity

        should_unban_external = False
        server_name = ""
        media_name = ""
        new_pack_bytes = 0
        remaining_score = 0
        still_banned = False

        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            media_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if media_user is None:
                return Result(False, "您尚未注册该服务器，请先注册后再兑换流量包。")

            server = await self.server_repo.get_by_id(server_id, fresh=True)
            if not server or not server.is_enabled:
                return Result(False, "服务器不存在或已停用，暂不支持兑换流量包。")

            server_name = server.name
            media_name = media_user.media_name

            deducted = await self.telegram_repo.deduct_score_if_sufficient(
                user_id, total_cost
            )
            if not deducted:
                return Result(
                    False,
                    f"兑换失败，您的积分不足，兑换需要 {total_cost} 积分。",
                )

            try:
                fresh_mu = await self.media_repo.get_by_id(
                    user_id, server_id, fresh=True
                )
                if fresh_mu is None:
                    await self.telegram_repo.update_score(user_id, total_cost)
                    return Result(False, "媒体账号状态已变更，已退回积分。")

                was_suspended = bool(fresh_mu.is_traffic_suspended)
                still_banned = bool(fresh_mu.is_banned)
                target_disabled = compute_external_disabled_state(
                    is_banned=still_banned,
                    is_traffic_suspended=False,
                )
                should_unban_external = was_suspended and not target_disabled

                if should_unban_external:
                    outcome = await sync_external_disabled_state(
                        self.media_clients.get(server_id),
                        media_id=fresh_mu.media_id,
                        target_disabled=False,
                        log_context="兑换流量包解除超流停用",
                        user_id=user_id,
                        server_id=server_id,
                    )
                    if not outcome.ok:
                        await self.telegram_repo.update_score(user_id, total_cost)
                        if outcome.missing_client:
                            return Result(
                                False,
                                "兑换失败，服务器客户端未连接，积分已退回，请稍后重试。",
                            )
                        return Result(
                            False,
                            "兑换失败，恢复媒体账号状态时出错，积分已退回，请联系管理员。",
                        )

                fresh_mu.traffic_pack_bytes = (
                    max(0, int(fresh_mu.traffic_pack_bytes or 0)) + added_bytes
                )
                fresh_mu.is_traffic_suspended = False

                quota = max(0, int(server.traffic_quota_bytes or 0))
                if quota > 0:
                    base_remaining = max(
                        0, quota - int(fresh_mu.cycle_traffic_bytes or 0)
                    )
                    new_pack = int(fresh_mu.traffic_pack_bytes)
                    new_total_remaining = base_remaining + new_pack
                    if new_total_remaining > 0:
                        fresh_mu.traffic_alert_100_sent = False
                    if new_total_remaining * 5 > (quota + new_pack):
                        fresh_mu.traffic_alert_80_sent = False

                await self.media_repo.session.commit()
                await self.media_repo.session.refresh(fresh_mu)
                new_pack_bytes = int(fresh_mu.traffic_pack_bytes)
                tg_user = await self.telegram_repo.get_or_create(user_id)
                remaining_score = int(tg_user.score)
            except (SQLAlchemyError, ValueError, RuntimeError) as exc:
                logger.exception(
                    "兑换流量包提交数据库失败，退回已扣积分: user_id={}, server_id={}, err={}",
                    user_id,
                    server_id,
                    exc,
                )
                await self.media_repo.session.rollback()
                await self.telegram_repo.update_score(user_id, total_cost)
                return Result(
                    False, "兑换过程中发生数据库异常，积分已退回，请稍后重试。"
                )

        if should_unban_external:
            await self._send_traffic_unsuspended_dm(
                user_id=user_id,
                server_name=server_name,
                media_name=media_name,
                added_bytes=added_bytes,
                total_pack_bytes=new_pack_bytes,
            )

        added_str = format_traffic_bytes(added_bytes)
        total_pack_str = format_traffic_bytes(new_pack_bytes)
        if should_unban_external:
            msg = (
                f"流量包兑换成功（+{added_str}，消耗 {total_cost} 积分），"
                f"当前流量包余额 {total_pack_str}，已为您立即解除超流停用。"
            )
        elif still_banned:
            msg = (
                f"流量包兑换成功（+{added_str}，消耗 {total_cost} 积分），"
                f"当前流量包余额 {total_pack_str}。由于账号仍处于过期封禁状态，续期后将恢复播放。"
            )
        else:
            msg = (
                f"流量包兑换成功（+{added_str}，消耗 {total_cost} 积分），"
                f"当前流量包余额 {total_pack_str}。"
            )

        return Result(
            True,
            msg,
            extra_data={
                "added_bytes": added_bytes,
                "cost_score": total_cost,
                "remaining_score": remaining_score,
                "traffic_pack_bytes": new_pack_bytes,
                "is_traffic_suspended": False,
                "unsuspended": should_unban_external,
            },
        )

    async def redeem_code(
        self, user_id: int, username: str | Literal[False] | None, code_str: str
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
        except Exception as e:  # pylint: disable=broad-exception-caught
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
        """为用户或管理员生成注册码或续期码。

        Args:
            user_id: Telegram 用户 ID。
            code_type: 激活码类型，``signup`` 或 ``renew``。
            server_id: 目标媒体服务器 ID。

        Returns:
            包含生成结果与激活码详情文案的 ``Result`` 对象。

        副作用：非管理员用户需校验总开关与“积分生成激活码”子开关并扣减积分，管理员零成本制码。
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
            # 仅非管理员消耗积分生成激活码时校验总开关与“积分生成激活码”子开关；管理员零消费制码不受阻断
            allowed, reason = await self.config_repo.check_points_feature(
                ConfigRepository.KEY_ENABLE_POINTS_GENERATE_CODE
            )
            if not allowed:
                return Result(False, reason)
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
            TelegramMessage.render_markdown(t"""\
            ✅ **{type_cn}生成成功**

            服务器: `{server.name}`
            代码: `{code.code}`
            过期时间: {code.expires_at.strftime("%Y-%m-%d")}

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

        is_unlocked = bool(
            policy
            and (
                policy.EnableAllFolders
                or any(lid in (policy.EnabledFolders or []) for lid in nsfw_ids)
            )
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
            True,
            TelegramMessage.render_markdown(
                t"已 {action_text} 您的 NSFW 权限 (服务器: {server.name})。"
            ),
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
        except (HTTPError, ValueError) as e:
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
            except (HTTPError, ValueError, KeyError, TypeError) as e:
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
            except (HTTPError, ValueError, KeyError, TypeError) as e:
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
                TelegramMessage.render_markdown(t"""\
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

    async def release_traffic_suspensions(
        self, server_id: int | None = None
    ) -> tuple[int, int]:
        """关闭流量采集或超流自动停用开关时，安全批量释放超流停用账号

        永久保留历史流量与流量包余额；仅当账号不同时处于过期封禁（``is_banned``）时
        才在外部媒体服务器解除禁用，绝不误放过期封禁账号。

        Args:
            server_id: 目标服务器 ID；为 ``None`` 时扫描全部服务器（用于全局开关关闭）。

        Returns:
            ``(成功清除或释放账号数, 外部解封失败待重试账号数)`` 二元组。
        """
        suspended = await self.media_repo.find_traffic_suspended(server_id)
        if not suspended:
            return 0, 0

        target_pairs = [(int(mu.id), int(mu.server_id)) for mu in suspended]
        # 释放只读扫描快照，保证进入账号锁后能读取最新并发提交状态
        await self.media_repo.end_clean_read_tx()

        released_count = 0
        failed_count = 0
        global_traffic_on = ConfigRepository.is_traffic_enabled_from_cache()

        for uid, sid in target_pairs:
            async with get_account_lock(uid, sid):
                await self.media_repo.end_clean_read_tx()
                fresh_mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
                if fresh_mu is None or not fresh_mu.is_traffic_suspended:
                    continue

                fresh_srv = await self.server_repo.get_by_id(sid, fresh=True)
                # 防止锁等待期间管理员已重新同时开启了全局及单服自动停用开关
                if (
                    global_traffic_on
                    and fresh_srv is not None
                    and fresh_srv.is_enabled
                    and fresh_srv.enable_traffic
                    and fresh_srv.enable_traffic_auto_suspend
                ):
                    continue

                keep_disabled = compute_external_disabled_state(
                    is_banned=bool(fresh_mu.is_banned),
                    is_traffic_suspended=False,
                )
                if not keep_disabled:
                    outcome = await sync_external_disabled_state(
                        self.media_clients.get(sid),
                        media_id=fresh_mu.media_id,
                        target_disabled=False,
                        treat_404_as_synced=True,
                        log_context="释放超流停用外部解封",
                        user_id=uid,
                        server_id=sid,
                    )
                    if not outcome.ok:
                        failed_count += 1
                        continue

                fresh_mu.is_traffic_suspended = False
                await self.media_repo.session.commit()
                released_count += 1

        return released_count, failed_count
