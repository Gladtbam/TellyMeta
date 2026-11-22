import inspect
import ipaddress
import math
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI
from httpx2 import AsyncClient, HTTPError, HTTPStatusError, RequestError
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio.session import AsyncSession

from core.billing_anchor import backfill_billing_cycle, resolve_anchored_cycle_window
from core.config import get_settings
from core.economy_policy import (
    BYTES_PER_GIB,
    TrafficBalance,
    calculate_traffic_balances,
    calculate_traffic_pack_points_cost,
    calculate_traffic_pack_unit_bytes,
    evaluate_traffic_pack_access,
    is_auto_renew_open,
    sync_traffic_alert_flags,
)
from core.media_utils import get_account_lock
from core.safe_expr import evaluate_safe_expression
from core.scheduler_jobs import (
    clear_traffic_unsuspend_job,
    parse_lifecycle_job_account,
    remove_account_lifecycle_jobs,
    schedule_account_lifecycle_jobs,
    schedule_traffic_unsuspend_retry,
)
from core.telegram_message import TelegramMessage
from core.user_policy import (
    build_default_user_policy_patch,
    merge_user_policy,
    parse_server_default_user_policy,
)
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
from services.notification_renderer import (
    AccountDeletedPayload,
    AdminUserActionPayload,
    AutoRenewFailedPayload,
    AutoRenewSuccessPayload,
    BatchTrafficReleaseSummaryPayload,
    BillingCycleResetPayload,
    EffectiveEconomyContext,
    ExpiryBanPayload,
    GraceDeleteReminderPayload,
    NotificationPayload,
    PreExpiryReminderPayload,
    SelfActionReceiptPayload,
    TrafficUnsuspendedPayload,
    UserPolicySyncSummaryPayload,
)
from services.notification_service import NotificationService
from services.traffic_service import flush_app_traffic
from services.user_service import Result
from services.watch_time_service import evaluate_watch_time_renewal

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
    """自动续期单账号执行结果，显式区分成功、续期通道、扣分、可重试故障与失败预警详情"""

    renewed: bool
    message: str
    retryable_error: bool = False
    expires_at: datetime | None = None
    channel: _RenewChannel | None = None
    cost_score: int = 0
    failure_notifiable: bool = False
    required_score: int | None = None
    user_score: int | None = None
    watch_status_text: str = ""


# 到期准点复核允许的调度器短时唤醒漂移容差（防止毫秒级唤醒晚于 expires_at 误触发重锚与清空当期流量）
EXPIRY_RESCUE_JITTER_GRACE = timedelta(minutes=5)


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

    def _get_scheduler(self) -> Any:
        """安全获取挂载在 ``app.state`` 上的 APScheduler 调度器实例（未配置时返回 ``None``）"""
        return getattr(getattr(self.app, "state", None), "scheduler", None)

    async def _emit_notification(
        self,
        payload: NotificationPayload,
        *,
        user_id: int | None = None,
        server_id: int | None = None,
        source_key: str | None = None,
        skip_inbox_fallback: bool = False,
    ) -> None:
        """通过统一通知管线异步入队发射事件（绝不在账号锁内执行）。"""
        await NotificationService(self.app).emit(
            payload,
            user_id=user_id,
            server_id=server_id,
            source_key=source_key,
            skip_inbox_fallback=skip_inbox_fallback,
            session=self.media_repo.session,
        )

    async def _safe_resolve_user_name(self, user_id: int) -> str:
        """安全查询 Telegram 用户展示名，测试替身未配置或异常时返回空串。"""
        tg_client = getattr(getattr(self.app, "state", None), "telethon_client", None)
        getter = getattr(tg_client, "get_user_name", None)
        if not callable(getter):
            return ""
        try:
            raw = getter(user_id)
            if inspect.isawaitable(raw):
                raw = await raw
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        except Exception:  # pylint: disable=broad-exception-caught
            return ""
        return ""

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
                    context = {
                        "response": response,
                        "r": response,
                    }
                    try:
                        # 使用 AST 白名单求值器安全解析外部响应表达式
                        is_valid = evaluate_safe_expression(
                            server.registration_external_parser, context
                        )
                        if is_valid:
                            return Result(True, "验证通过")
                        return Result(False, "验证失败 (解析未通过)。")
                    except (
                        NameError,
                        TypeError,
                        ValueError,
                        SyntaxError,
                        AttributeError,
                        LookupError,
                        ZeroDivisionError,
                        re.error,
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
        *,
        emit_receipt: bool = True,
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
            allowed, reason = await self.config_repo.check_points_feature(
                ConfigRepository.KEY_ENABLE_POINTS_REGISTER
            )
            if not allowed:
                return Result(
                    False,
                    f"{reason} 当前仅支持通过激活码注册该服务器。",
                )
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
            if not media_user_dto or not media_user_dto.Id:
                await self._rollback_registration_cost(
                    user_id, server_id, reserved_score, reserved_count
                )
                return Result(False, "注册失败，无法创建账户，请联系管理员。")

            # Jellyfin UserDto.Id 为 UUID 对象，落库与路径拼接前统一归一化为字符串
            media_user_id: str = str(media_user_dto.Id)
            created_media_id = media_user_id

            # 先应用默认 UserPolicy 模板及初始权限/NSFW策略，若失败则尚未写入本地记录，可直接清理外部账户
            policy_spec = parse_server_default_user_policy(server.default_user_policy)
            policy_patch = build_default_user_policy_patch(
                policy_spec, server.server_type
            )
            if policy_patch:
                base_policy = getattr(media_user_dto, "Policy", None)
                if base_policy is None:
                    fetched_info, _ = await self._fetch_external_user(
                        media_service, media_user_id
                    )
                    base_policy = fetched_info.Policy if fetched_info else None
                merged_policy = merge_user_policy(base_policy, policy_patch)
                await self._apply_nsfw_policy(
                    media_service,
                    media_user_id,
                    server,
                    enable_nsfw=bool(server.nsfw_enabled),
                    current_policy=merged_policy,
                )
            elif not server.nsfw_enabled:
                await self._apply_nsfw_policy(
                    media_service, media_user_id, server, enable_nsfw=False
                )
            else:
                await media_service.update_policy(
                    media_user_id, {"EnableAllFolders": True}, is_none=True
                )

            reg_now = self._now()
            media_user = await self.media_repo.create(
                user_id=user_id,
                server_id=server_id,
                media_id=media_user_id,
                media_name=username,
                expiry_months=self._get_server_expiry_months(server),
                now=reg_now,
            )
            schedule_account_lifecycle_jobs(
                self._get_scheduler(),
                media_user,
                now=reg_now,
            )

            if emit_receipt:
                await self._emit_notification(
                    SelfActionReceiptPayload(
                        action_type="register",
                        server_name=server.name,
                        media_name=str(username),
                        expires_at=media_user.expires_at,
                        cost_score=reserved_score,
                        economy=EffectiveEconomyContext.from_cache(server),
                    ),
                    user_id=user_id,
                    server_id=server_id,
                    source_key=f"self_register:{user_id}:{server_id}",
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
        # 1. 准备基础策略字典（支持传入已合并的字典或外部 UserPolicy 模型）
        if isinstance(current_policy, dict):
            policy_dict = dict(current_policy)
        elif current_policy is not None:
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

    async def renew(
        self,
        user_id: int,
        server_id: int,
        use_score: bool,
        *,
        emit_receipt: bool = True,
    ) -> Result:
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
            emit_receipt: 是否通过统一通知管线发射脱敏自助回执（由同步敏感弹窗通道调用时可置 ``False``）。

        Returns:
            续期操作结果 ``Result``，携带成功状态与通知文案
        """
        await self.flush_pending_traffic()
        pending_notifications: list[tuple[NotificationPayload, int, int, str]] = []
        result_obj: Result
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            media_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if media_user is None:
                return Result(False, "您尚未注册，请先注册后再续期。")

            server = await self.server_repo.get_by_id(server_id, fresh=True)
            if not server:
                return Result(False, "服务器配置已失效。")

            wt_status = evaluate_watch_time_renewal(
                server,
                media_user,
                global_watch_time_enabled=await self.config_repo.is_watch_time_enabled(),
            )
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

            # 若续期时当前周期重置节点已到点或已逾期，先结清上期流量账期与超流解封，避免新周期吞掉重置
            was_traffic_suspended = bool(media_user.is_traffic_suspended)
            was_banned = bool(media_user.is_banned)
            (
                media_user,
                retry_unsuspend,
            ) = await self._settle_due_traffic_reset_before_renewal_locked(
                media_user,
                server,
                now=now,
                effective_cycle_now=now,
                pending_notifications=pending_notifications,
            )
            just_unsuspended_externally = (
                was_traffic_suspended
                and not was_banned
                and not media_user.is_traffic_suspended
            )

            # 宽限期内续期时，先在媒体服务器上恢复账户，再扣减积分并更新本地周期与清零本期保活时长
            if not just_unsuspended_externally:
                restore_err = await self._restore_external_if_disabled(
                    client=client,
                    media_info=media_info,
                    media_user=media_user,
                    action_label="续期",
                )
                if restore_err is not None:
                    return Result(
                        False, "续期失败，恢复媒体账号状态时出错，请联系管理员。"
                    )

            if renew_channel == _RenewChannel.SCORE and renew_score > 0:
                await self.telegram_repo.update_score(user_id, -renew_score)

            media_user = await self.media_repo.extend_anchored_cycle(
                media_user,
                self._get_server_expiry_months(server),
                now=now,
            )
            scheduler = self._get_scheduler()
            schedule_account_lifecycle_jobs(
                scheduler,
                media_user,
                now=now,
            )
            if retry_unsuspend and server.is_enabled:
                schedule_traffic_unsuspend_retry(
                    scheduler,
                    user_id,
                    server_id,
                    run_date=now + timedelta(minutes=15),
                )

            if emit_receipt:
                pending_notifications.append(
                    (
                        SelfActionReceiptPayload(
                            action_type="renew",
                            server_name=server.name,
                            media_name=media_user.media_name,
                            expires_at=media_user.expires_at,
                            cost_score=(
                                renew_score
                                if renew_channel == _RenewChannel.SCORE
                                else 0
                            ),
                            economy=EffectiveEconomyContext.from_cache(server),
                        ),
                        user_id,
                        server_id,
                        f"self_renew:{user_id}:{server_id}",
                    )
                )

            expires_str = media_user.expires_at.strftime("%Y-%m-%d %H:%M:%S")
            suspension_note = ""
            if media_user.is_traffic_suspended:
                points_pack_ok = await self.config_repo.is_points_feature_enabled(
                    ConfigRepository.KEY_ENABLE_POINTS_TRAFFIC_PACK
                )
                pack_open, _ = evaluate_traffic_pack_access(
                    server,
                    points_pack_allowed=points_pack_ok,
                    points_pack_reason="",
                    global_traffic_enabled=await self.config_repo.is_traffic_enabled(),
                )
                suspension_note = (
                    "（提示：账号当前仍处于超流停用状态，兑换流量包或等待账期重置后可恢复播放）"
                    if pack_open
                    else "（提示：账号当前仍处于超流停用状态，请等待账期重置或联系管理员调整配额后恢复播放）"
                )
            if renew_channel == _RenewChannel.WATCH_TIME:
                points_enabled = ConfigRepository.is_points_enabled_from_cache()
                wt_tag = "时长保活免积分" if points_enabled else "时长保活自动续期"
                result_obj = Result(
                    True,
                    TelegramMessage.render_markdown(
                        t"续期成功（{wt_tag}），您的 {server.name} 媒体账号已延长至 {expires_str}。{suspension_note}"
                    ),
                )
            else:
                result_obj = Result(
                    True,
                    TelegramMessage.render_markdown(
                        t"续期成功，您的 {server.name} 媒体账号已延长至 {expires_str}。{suspension_note}"
                    ),
                )

        for payload, uid, sid, src_key in pending_notifications:
            await self._emit_notification(
                payload, user_id=uid, server_id=sid, source_key=src_key
            )
        return result_obj

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

    async def _settle_due_traffic_reset_before_renewal_locked(
        self,
        media_user: MediaUser,
        server: ServerInstance,
        *,
        now: datetime,
        effective_cycle_now: datetime,
        pending_notifications: list[tuple[NotificationPayload, int, int, str]]
        | None = None,
    ) -> tuple[MediaUser, bool]:
        """在续期或自动续期推进周期前，先结清已到点的月度流量重置

        当 ``expires_at == next_reset_at`` 同刻触发续期，或过期补续时原账期重置点已过，
        先执行该期流量重置与安全解封，避免 ``extend_anchored_cycle`` 将重置时间推进到下月
        而吞掉当期清零与超流解封。

        Args:
            media_user: 账号互斥锁内最新媒体账号 ORM 对象。
            server: 所属服务器实例配置。
            now: 当前实际时间。
            effective_cycle_now: 续期周期基准时间（抖动抢救时可能回齐至 ``expires_at``）。
            pending_notifications: 可选收集锁外发射通知载荷的列表。

        Returns:
            ``(媒体账号 ORM 对象, 是否需要调度超流解封延迟重试)`` 二元组。
        """
        if media_user.next_reset_at is None or media_user.next_reset_at > now:
            return media_user, False
        reset_now = (
            effective_cycle_now
            if media_user.next_reset_at <= effective_cycle_now
            else now
        )
        reset_res = await self._reset_single_account_traffic_locked(
            media_user,
            server,
            reset_now,
            pending_notifications=pending_notifications,
        )
        return media_user, reset_res == "reset_retry_unsuspend"

    async def try_auto_renew_account(
        self,
        user_id: int,
        server_id: int,
        *,
        anchor_rescue_grace: timedelta = EXPIRY_RESCUE_JITTER_GRACE,
    ) -> AutoRenewOutcome:
        """尝试在媒体账号到达有效期时，按服务器当前准许的条件执行自动续期

        仅当用户已开启 ``auto_renew``、未处于过期封禁、且已到达有效期（``expires_at <= now``）
        时执行：优先判定服务器时长保活资格（达标则 0 积分免费续期）；若未开放或未达标且全局积分续期
        生效，则原子校验并扣减 ``renew_score`` 后自动续期。外部核验成功前绝不扣分。

        Args:
            user_id: Telegram 用户 ID
            server_id: 目标媒体服务器 ID
            anchor_rescue_grace: 到期边界自动续期允许的调度唤醒容差；在
                ``expires_at <= now <= expires_at + anchor_rescue_grace`` 范围内仍按
                ``expires_at`` 顺推自然月，避免极短唤醒延迟误重锚 ``anchor_day``。

        Returns:
            结构化 ``AutoRenewOutcome``，包含续期成功标志、通道、消耗积分及临时通信异常 ``retryable_error`` 标志
        """
        await self.flush_pending_traffic()
        pending_notifications: list[tuple[NotificationPayload, int, int, str]] = []
        auto_success_payload: AutoRenewSuccessPayload | None = None
        outcome: AutoRenewOutcome
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            media_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if media_user is None:
                return AutoRenewOutcome(False, "媒体账号不存在")
            if media_user.is_banned:
                return AutoRenewOutcome(False, "媒体账号已处于过期封禁状态")
            if not media_user.auto_renew:
                return AutoRenewOutcome(False, "用户未开启自动续期")

            now = self._now()
            if media_user.expires_at > now:
                return AutoRenewOutcome(False, "账号尚未到达有效期")

            server = await self.server_repo.get_by_id(server_id, fresh=True)
            if not server or not server.is_enabled:
                return AutoRenewOutcome(False, "服务器不存在或已停用")

            wt_status = evaluate_watch_time_renewal(
                server,
                media_user,
                global_watch_time_enabled=await self.config_repo.is_watch_time_enabled(),
            )
            (
                points_renew_open,
                points_renew_reason,
            ) = await self.config_repo.check_points_feature(
                ConfigRepository.KEY_ENABLE_POINTS_RENEW
            )

            renew_score = 0
            if wt_status.is_eligible:
                renew_channel = _RenewChannel.WATCH_TIME
            elif points_renew_open:
                renew_channel = _RenewChannel.SCORE
                tg_user = await self.telegram_repo.get_or_create(user_id)
                renew_score = max(0, int(await self.telegram_repo.get_renew_score()))
                if tg_user.score < renew_score:
                    return AutoRenewOutcome(
                        False,
                        f"自动续期积分不足，需要 {renew_score} 积分（当前 {tg_user.score}）",
                        failure_notifiable=True,
                        required_score=renew_score,
                        user_score=int(tg_user.score),
                        watch_status_text=wt_status.status_text
                        if wt_status.is_open
                        else "",
                    )
            else:
                reason = (
                    wt_status.status_text if wt_status.is_open else points_renew_reason
                )
                return AutoRenewOutcome(
                    False,
                    f"未满足自动续期条件：{reason}",
                    failure_notifiable=True,
                    watch_status_text=wt_status.status_text
                    if wt_status.is_open
                    else "",
                )

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
                    "自动续期获取外部媒体账号失败",
                    retryable_error=is_retryable,
                )

            effective_cycle_now = now
            if (
                anchor_rescue_grace > timedelta(0)
                and media_user.expires_at
                <= now
                <= media_user.expires_at + anchor_rescue_grace
            ):
                # 调度器准点触发可能晚于 expires_at 数毫秒至数秒；在容差窗内仍视同到期边界未越界，
                # 保留原 anchor_day（例如 31 日）并避免误清空尚在进行中的流量周期
                effective_cycle_now = media_user.expires_at

            # 若 expires_at == next_reset_at 同刻到期自动续期，先结清当期流量重置并按需解除超流停用
            was_traffic_suspended = bool(media_user.is_traffic_suspended)
            was_banned = bool(media_user.is_banned)
            (
                media_user,
                retry_unsuspend,
            ) = await self._settle_due_traffic_reset_before_renewal_locked(
                media_user,
                server,
                now=now,
                effective_cycle_now=effective_cycle_now,
                pending_notifications=pending_notifications,
            )
            just_unsuspended_externally = (
                was_traffic_suspended
                and not was_banned
                and not media_user.is_traffic_suspended
            )

            if not just_unsuspended_externally:
                restore_err = await self._restore_external_if_disabled(
                    client=client,
                    media_info=media_info,
                    media_user=media_user,
                    action_label="自动续期",
                )
                if restore_err is not None:
                    return AutoRenewOutcome(
                        False, "自动续期恢复外部媒体账号状态失败", retryable_error=True
                    )

            # 确认外部账号正常后再原子扣除积分，防止外部 500 故障或跨服并发时出现误扣/负分
            remaining_score: int | None = None
            if renew_channel == _RenewChannel.SCORE and renew_score > 0:
                deducted = await self.telegram_repo.deduct_score_if_sufficient(
                    user_id, renew_score
                )
                if not deducted:
                    tg_after_fail = await self.telegram_repo.get_by_id(user_id)
                    return AutoRenewOutcome(
                        False,
                        f"自动续期失败，积分余额不足 {renew_score}",
                        failure_notifiable=True,
                        required_score=renew_score,
                        user_score=int(tg_after_fail.score)
                        if tg_after_fail is not None
                        else 0,
                        watch_status_text=wt_status.status_text
                        if wt_status.is_open
                        else "",
                    )
                tg_after = await self.telegram_repo.get_by_id(user_id)
                remaining_score = int(tg_after.score) if tg_after is not None else None
                reloaded = await self.media_repo.get_by_id(
                    user_id, server_id, fresh=True
                )
                if reloaded is None:
                    return AutoRenewOutcome(False, "媒体账号不存在")
                media_user = reloaded

            media_user = await self.media_repo.extend_anchored_cycle(
                media_user,
                self._get_server_expiry_months(server),
                now=effective_cycle_now,
            )
            scheduler = self._get_scheduler()
            schedule_account_lifecycle_jobs(
                scheduler,
                media_user,
                now=now,
            )
            if retry_unsuspend and server.is_enabled:
                schedule_traffic_unsuspend_retry(
                    scheduler,
                    user_id,
                    server_id,
                    run_date=now + timedelta(minutes=15),
                )

            server_name = str(server.name)
            media_name = str(media_user.media_name)
            new_expires_at = media_user.expires_at
            economy = EffectiveEconomyContext.from_cache(server)
            auto_success_payload = AutoRenewSuccessPayload(
                server_name=server_name,
                media_name=media_name,
                expires_at=new_expires_at,
                renew_channel=(
                    "watch_time"
                    if renew_channel == _RenewChannel.WATCH_TIME
                    else "points"
                ),
                cost_score=renew_score,
                remaining_score=remaining_score,
                is_traffic_suspended=bool(media_user.is_traffic_suspended),
                economy=economy,
            )
            exp_str = new_expires_at.strftime("%Y-%m-%d %H:%M:%S")
            channel_desc = (
                "有效播放时长"
                if renew_channel == _RenewChannel.WATCH_TIME
                else f"消耗 {renew_score} 积分"
            )
            logger.info(
                "自动续期成功: user_id={}, server_id={}, channel={}, cost={}, expires_at={}",
                user_id,
                server_id,
                renew_channel.value,
                renew_score,
                exp_str,
            )
            outcome = AutoRenewOutcome(
                True,
                f"已通过{channel_desc}自动续期至 {exp_str}",
                expires_at=new_expires_at,
                channel=renew_channel,
                cost_score=renew_score,
            )

        for payload, uid, sid, src_key in pending_notifications:
            await self._emit_notification(
                payload, user_id=uid, server_id=sid, source_key=src_key
            )
        if auto_success_payload is not None:
            await self._emit_notification(
                auto_success_payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"auto_renew_success:{user_id}:{server_id}",
            )
        return outcome

    async def process_auto_renewals(self) -> int:
        """批量扫描已到达有效期且开启自动续期的媒体账号并按服务器准许条件续期

        Returns:
            本次成功完成自动续期的账号数量
        """
        deadline = self._now()
        candidates = await self.media_repo.find_auto_renew_candidates(deadline)
        # 仅提取主键标量，使逐账号进入锁内执行 end_clean_read_tx() + fresh=True 重查时不受此前 ORM 实例或读快照牵连
        target_pairs = [(mu.id, mu.server_id) for mu in candidates]
        renewed_count = 0
        for uid, sid in target_pairs:
            outcome = await self.try_auto_renew_account(
                uid, sid, anchor_rescue_grace=EXPIRY_RESCUE_JITTER_GRACE
            )
            if outcome.renewed:
                renewed_count += 1
        return renewed_count

    async def toggle_auto_renew(
        self, user_id: int, server_id: int, enabled: bool | None = None
    ) -> Result:
        """切换或设置指定媒体账号的用户可控自动续期开关

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
                return Result(False, "您尚未注册，请先注册后再设置自动续期。")

            server = await self.server_repo.get_by_id(server_id, fresh=True)
            if not server:
                return Result(False, "服务器配置已失效。")

            target_state = (not media_user.auto_renew) if enabled is None else enabled
            wt_status = evaluate_watch_time_renewal(
                server,
                media_user,
                global_watch_time_enabled=await self.config_repo.is_watch_time_enabled(),
            )
            points_renew_open = await self.config_repo.is_points_feature_enabled(
                ConfigRepository.KEY_ENABLE_POINTS_RENEW
            )
            auto_open = is_auto_renew_open(
                server,
                watch_time_renewal_open=wt_status.is_open,
                points_renew_open=points_renew_open,
            )

            # 仅开启时要求服务器至少开放一种自动续期条件；关闭开关始终放行，以便历史用户可随时退订
            if target_state and not auto_open:
                if not server.is_enabled:
                    return Result(False, "当前服务器已停用，暂不支持开启自动续期。")
                if ConfigRepository.is_points_enabled_from_cache():
                    return Result(
                        False,
                        "当前服务器未开放可自动触发的续期条件（时长保活与积分续期均未生效）。",
                    )
                return Result(
                    False,
                    "当前服务器未开放可自动触发的续期条件（请先开启有效播放时长保活续期）。",
                )

            media_user = await self.media_repo.set_auto_renew(media_user, target_state)

            if not target_state:
                msg = "已关闭自动续期。"
            elif wt_status.is_open and points_renew_open:
                msg = "已开启自动续期：到达有效期时将优先尝试时长免分续期，未达标时自动消耗积分续期。"
            elif wt_status.is_open:
                msg = (
                    "已开启自动续期：到达有效期且本周期观看度达 100% 时将自动免积分续期。"
                    if ConfigRepository.is_points_enabled_from_cache()
                    else "已开启自动续期：到达有效期且本周期观看度达 100% 时将自动免费续期。"
                )
            else:
                renew_score = max(0, int(await self.telegram_repo.get_renew_score()))
                msg = f"已开启自动续期：到达有效期时将自动消耗 {renew_score} 积分续期。"

            return Result(True, msg, extra_data={"auto_renew": target_state})

    @staticmethod
    def _sync_traffic_alert_flags_locked(
        media_user: MediaUser, bal: TrafficBalance
    ) -> None:
        """依据最新有效配额与剩余余额刷新 80%/100% 配额预警去重标记。"""
        sync_traffic_alert_flags(media_user, bal)

    @staticmethod
    def _should_release_suspension_locked(
        global_traffic_on: bool,
        srv: ServerInstance | None,
        mu: MediaUser,
    ) -> tuple[bool, TrafficBalance]:
        """判断处于超流停用状态的账号在最新配置与额度下是否满足释放条件。

        Args:
            global_traffic_on: 全局流量总开关（``traffic:enable``）是否开启。
            srv: 所属媒体服务器实例（可为 ``None``）。
            mu: 目标媒体账号 ORM 对象。

        Returns:
            ``(should_release, balance)`` 二元组；当任一流量开关/自动停用门控关闭、
            所属服不限流或当期双池总剩余大于 0 时返回 ``True``。
        """
        bal = calculate_traffic_balances(
            srv.traffic_quota_bytes if srv else 0,
            mu.cycle_traffic_adjustment_bytes,
            mu.cycle_traffic_bytes,
            mu.traffic_pack_bytes,
        )
        srv_auto_suspend = bool(
            srv is not None
            and srv.is_enabled
            and srv.enable_traffic
            and srv.enable_traffic_auto_suspend
        )
        should_release = (
            (not global_traffic_on)
            or (not srv_auto_suspend)
            or (not bal.is_limited)
            or ((bal.total_remaining_bytes or 0) > 0)
        )
        return should_release, bal

    async def redeem_traffic_pack(
        self, user_id: int, server_id: int, quantity: int = 1
    ) -> Result:
        """消耗 Telegram 积分兑换指定媒体账号的单期流量包，并在安全时自动解除超流停用

        严格校验 ``积分启用 ∩ 流量包积分开关 ∩ 全局与单服有限配额限流 ∩ 单服 allow_traffic_pack``；
        单价支持单服 ``fixed`` / ``ratio``（最低 1 积分），购买余额仅限本周期有效，随 ``next_reset_at`` 清零。

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
        global_traffic_on = await self.config_repo.is_traffic_enabled()

        should_unban_external = False
        server_name = ""
        media_name = ""
        new_pack_bytes = 0
        remaining_score = 0
        still_banned = False
        next_reset_at: datetime | None = None
        total_cost = 0
        added_bytes = 0
        unsuspended_payload: TrafficUnsuspendedPayload | None = None

        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            media_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            server = await self.server_repo.get_by_id(server_id, fresh=True)
            gate_ok, gate_reason = evaluate_traffic_pack_access(
                server,
                points_pack_allowed=allowed,
                points_pack_reason=reason,
                global_traffic_enabled=global_traffic_on,
            )
            if not gate_ok:
                return Result(False, gate_reason)
            if media_user is None:
                return Result(False, "您尚未注册该服务器，请先注册后再兑换流量包。")

            assert server is not None
            renew_score = (
                await self.telegram_repo.get_renew_score()
                if server.traffic_pack_points_mode != "fixed"
                else 0
            )
            unit_cost = calculate_traffic_pack_points_cost(server, renew_score)
            unit_bytes = calculate_traffic_pack_unit_bytes(server)
            total_cost = unit_cost * quantity
            added_bytes = unit_bytes * quantity

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

                bal = calculate_traffic_balances(
                    server.traffic_quota_bytes,
                    fresh_mu.cycle_traffic_adjustment_bytes,
                    fresh_mu.cycle_traffic_bytes,
                    fresh_mu.traffic_pack_bytes,
                )
                self._sync_traffic_alert_flags_locked(fresh_mu, bal)

                await self.media_repo.session.commit()
                await self.media_repo.session.refresh(fresh_mu)
                new_pack_bytes = int(fresh_mu.traffic_pack_bytes)
                next_reset_at = fresh_mu.next_reset_at
                tg_user = await self.telegram_repo.get_or_create(user_id)
                remaining_score = int(tg_user.score)
                if should_unban_external:
                    unsuspended_payload = TrafficUnsuspendedPayload(
                        server_name=server_name,
                        media_name=media_name,
                        recovery_reason="traffic_pack",
                        added_bytes=added_bytes,
                        total_pack_bytes=new_pack_bytes,
                        next_reset_at=next_reset_at,
                        still_expiry_banned=False,
                        economy=EffectiveEconomyContext.from_cache(server),
                    )
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

        if unsuspended_payload is not None:
            await self._emit_notification(
                unsuspended_payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"traffic_unsuspended:{user_id}:{server_id}:traffic_pack",
            )

        added_str = format_traffic_bytes(added_bytes)
        total_pack_str = format_traffic_bytes(new_pack_bytes)
        reset_clause = (
            f"仅限当前账期有效，将于 {next_reset_at.strftime('%Y-%m-%d %H:%M')} 清零。"
            if next_reset_at is not None
            else "仅限当前账期有效，将于本周期重置时清零。"
        )
        if should_unban_external:
            msg = (
                f"流量包兑换成功（+{added_str}，消耗 {total_cost} 积分），"
                f"当前流量包余额 {total_pack_str}，已为您立即解除超流停用。{reset_clause}"
            )
        elif still_banned:
            msg = (
                f"流量包兑换成功（+{added_str}，消耗 {total_cost} 积分），"
                f"当前流量包余额 {total_pack_str}。{reset_clause}由于账号仍处于过期封禁状态，续期后将恢复播放。"
            )
        else:
            msg = (
                f"流量包兑换成功（+{added_str}，消耗 {total_cost} 积分），"
                f"当前流量包余额 {total_pack_str}。{reset_clause}"
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
                "next_reset_at": next_reset_at.isoformat() if next_reset_at else None,
            },
        )

    async def redeem_code(
        self,
        user_id: int,
        username: str | Literal[False] | None,
        code_str: str,
        *,
        emit_receipt: bool = True,
    ) -> Result:
        """使用注册码或续期码注册或续期
        Args:
            user_id (int): 用户的 Telegram ID
            code_str (str): 注册码或续期码
            emit_receipt: 是否通过统一通知管线发射脱敏核销回执。
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
                result = await self.register(
                    user_id, username, code_server_id, True, emit_receipt=False
                )
            elif code_type == "renew":
                result = await self.renew(
                    user_id, code_server_id, False, emit_receipt=False
                )
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

        if emit_receipt:
            fresh_mu = await self.media_repo.get_by_id(
                user_id, code_server_id, fresh=True
            )
            await self._emit_notification(
                SelfActionReceiptPayload(
                    action_type="redeem_code",
                    server_name=server.name,
                    media_name=(
                        fresh_mu.media_name
                        if fresh_mu is not None
                        else str(username or "")
                    ),
                    expires_at=fresh_mu.expires_at if fresh_mu is not None else None,
                    code_kind="signup" if code_type == "signup" else "renew",
                    economy=EffectiveEconomyContext.from_cache(server),
                ),
                user_id=user_id,
                server_id=code_server_id,
                source_key=f"self_redeem_code:{user_id}:{code_server_id}",
            )

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

    async def sync_server_default_user_policy(self, server_id: int) -> Result:
        """将指定服务器配置的默认 UserPolicy 同步应用至该服全部存量媒体账号

        逐账号拉取外部当前策略并仅覆盖默认模板声明的允许字段，严格保留每个账号既有的
        ``IsDisabled``（过期封禁/超流停用）、``IsAdministrator``、NSFW 媒体库授权及认证器字段。

        Args:
            server_id: 目标媒体服务器实例 ID。

        Returns:
            包含同步结果、汇总文案与 ``total_count`` / ``synced_count`` / ``skipped_count`` /
            ``failed_count`` 明细字典的 ``Result`` 对象。
        """
        server = await self.server_repo.get_by_id(server_id, fresh=True)
        if not server:
            return Result(False, "服务器不存在。", extra_data={"not_found": True})
        if server.server_type not in (ServerType.EMBY, ServerType.JELLYFIN):
            return Result(False, "仅 Emby/Jellyfin 媒体服务器支持同步 UserPolicy。")
        if not server.is_enabled:
            return Result(False, "该服务器已停用，请先启用后再同步存量用户。")

        # 提前提取只读标量属性，避免后续 end_clean_read_tx() 回滚过期 ORM 对象引发 MissingGreenlet
        server_name = str(server.name)
        server_type = server.server_type

        policy_spec = parse_server_default_user_policy(server.default_user_policy)
        if not policy_spec.enabled:
            return Result(False, "当前服务器未启用默认 UserPolicy 配置，无需同步。")

        try:
            policy_patch = build_default_user_policy_patch(policy_spec, server_type)
        except ValueError as exc:
            return Result(False, f"默认 UserPolicy 配置无效: {exc}")

        client = self.media_clients.get(server_id)
        if not client:
            return Result(False, f"客户端未运行: {server_name}")

        media_users = await self.media_repo.get_all_by_server_id(server_id)
        targets = [(int(mu.id), str(mu.media_id)) for mu in media_users]
        await self.media_repo.end_clean_read_tx()

        total_count = len(targets)
        if total_count == 0:
            return Result(
                True,
                f"{server_name} 当前暂无存量媒体账号，无需同步。",
                extra_data={
                    "total_count": 0,
                    "synced_count": 0,
                    "skipped_count": 0,
                    "failed_count": 0,
                },
            )

        synced_count = 0
        skipped_count = 0
        failed_count = 0
        error_items: list[str] = []

        for uid, _media_id in targets:
            async with get_account_lock(uid, server_id):
                await self.media_repo.end_clean_read_tx()
                fresh_mu = await self.media_repo.get_by_id(uid, server_id, fresh=True)
                if fresh_mu is None:
                    skipped_count += 1
                    continue

                try:
                    media_info = await client.get_user_info(fresh_mu.media_id)
                except HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        skipped_count += 1
                        logger.info(
                            "同步默认 UserPolicy 时外部账号已不存在(404)，已跳过: server_id={}, user_id={}, media_id={}",
                            server_id,
                            uid,
                            fresh_mu.media_id,
                        )
                    else:
                        failed_count += 1
                        error_items.append(
                            f"{fresh_mu.media_name}: HTTP {exc.response.status_code}"
                        )
                        logger.error(
                            "同步默认 UserPolicy 获取外部账户失败: server_id={}, user_id={}, media_id={}, status={}",
                            server_id,
                            uid,
                            fresh_mu.media_id,
                            exc.response.status_code,
                        )
                    continue
                except (HTTPError, ValueError, TypeError) as exc:
                    failed_count += 1
                    error_items.append(f"{fresh_mu.media_name}: {exc}")
                    logger.error(
                        "同步默认 UserPolicy 获取外部账户异常: server_id={}, user_id={}, media_id={}, err={}",
                        server_id,
                        uid,
                        fresh_mu.media_id,
                        exc,
                    )
                    continue

                if not isinstance(media_info, User) or media_info.Policy is None:
                    failed_count += 1
                    error_items.append(f"{fresh_mu.media_name}: 缺少 Policy")
                    logger.warning(
                        "同步默认 UserPolicy 失败，外部账户缺少 Policy: server_id={}, user_id={}, media_id={}",
                        server_id,
                        uid,
                        fresh_mu.media_id,
                    )
                    continue

                merged_policy = merge_user_policy(media_info.Policy, policy_patch)
                try:
                    await client.update_policy(
                        fresh_mu.media_id, merged_policy, is_none=True
                    )
                    synced_count += 1
                except (HTTPError, ValueError, TypeError) as exc:
                    failed_count += 1
                    error_items.append(f"{fresh_mu.media_name}: {exc}")
                    logger.error(
                        "同步默认 UserPolicy 更新外部策略失败: server_id={}, user_id={}, media_id={}, err={}",
                        server_id,
                        uid,
                        fresh_mu.media_id,
                        exc,
                    )

        parts = [f"成功 {synced_count} 个"]
        if skipped_count > 0:
            parts.append(f"跳过 {skipped_count} 个（外部不存在或已清理）")
        if failed_count > 0:
            parts.append(f"失败 {failed_count} 个")
        summary_msg = f"已同步 {server_name} 的默认 UserPolicy 到存量账号（总计 {total_count} 个：{'，'.join(parts)}）。"
        overall_ok = not (synced_count == 0 and failed_count > 0)

        if synced_count + failed_count > 0:
            try:
                # 先释放循环读快照再入队，并在只读查询后及时收口，避免内存 SQLite 连接池事务冲突
                await self.media_repo.end_clean_read_tx()
                await NotificationService(self.app).emit(
                    UserPolicySyncSummaryPayload(
                        server_name=server_name,
                        total_count=total_count,
                        updated_count=synced_count,
                        skipped_count=skipped_count,
                        failed_count=failed_count,
                        error_summary="; ".join(error_items[:5]),
                    ),
                    server_id=server_id,
                    source_key=f"policy_sync:{server_id}",
                    session=self.media_repo.session,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("发射默认 UserPolicy 同步摘要通知失败: {}", exc)
            finally:
                await self.media_repo.end_clean_read_tx()

        return Result(
            overall_ok,
            summary_msg,
            extra_data={
                "total_count": total_count,
                "synced_count": synced_count,
                "skipped_count": skipped_count,
                "failed_count": failed_count,
            },
        )

    async def forget_password(
        self, user_id: int, server_id: int, *, emit_receipt: bool = True
    ):
        """重置密码
        Args:
            user_id (int): 用户的 Telegram ID
            server_id (int): 目标媒体服务器 ID
            emit_receipt: 是否通过统一通知管线发射脱敏安全操作回执
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
            if emit_receipt:
                server = await self.server_repo.get_by_id(server_id, fresh=True)
                await self._emit_notification(
                    SelfActionReceiptPayload(
                        action_type="reset_password",
                        server_name=(
                            server.name if server is not None else f"Server {server_id}"
                        ),
                        media_name=media_user.media_name,
                        economy=EffectiveEconomyContext.from_cache(server),
                    ),
                    user_id=user_id,
                    server_id=server_id,
                    source_key=f"self_reset_password:{user_id}:{server_id}",
                )
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
        remove_account_lifecycle_jobs(self._get_scheduler(), user_id, server_id)
        return Result(True, "账户已成功删除。")

    async def release_traffic_suspensions(
        self,
        server_id: int | None = None,
        *,
        trigger_reason: str = "服务器限流策略或配额变更",
    ) -> tuple[int, int]:
        """关闭流量开关、关闭超流停用或调高/清零服务器配额时，安全批量释放符合条件的超流停用账号

        保留历史总流量，仅在满足最新释放判定且未处于过期封禁（``is_banned``）时才解除外部媒体服务器禁用。

        Args:
            server_id: 目标服务器 ID；为 ``None`` 时扫描全部服务器（用于全局开关关闭）。
            trigger_reason: 批量释放的触发原因描述（用于运维汇总摘要展示）。

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
        skipped_banned_count = 0
        released_accounts: list[str] = []
        pending_user_notices: list[tuple[TrafficUnsuspendedPayload, int, int]] = []
        global_traffic_on = ConfigRepository.is_traffic_enabled_from_cache()

        for uid, sid in target_pairs:
            async with get_account_lock(uid, sid):
                await self.media_repo.end_clean_read_tx()
                fresh_mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
                if fresh_mu is None or not fresh_mu.is_traffic_suspended:
                    continue

                fresh_srv = await self.server_repo.get_by_id(sid, fresh=True)
                should_release, bal = self._should_release_suspension_locked(
                    global_traffic_on, fresh_srv, fresh_mu
                )
                if not should_release:
                    continue

                self._sync_traffic_alert_flags_locked(fresh_mu, bal)
                was_banned = bool(fresh_mu.is_banned)

                status = await self._clear_or_sync_traffic_suspension_locked(
                    fresh_mu,
                    fresh_srv,
                    log_context="释放超流停用外部解封",
                )
                if status == "cleared":
                    await self.media_repo.session.commit()
                    released_count += 1
                    if was_banned:
                        skipped_banned_count += 1
                    else:
                        srv_name = (
                            fresh_srv.name if fresh_srv is not None else f"Server {sid}"
                        )
                        released_accounts.append(str(fresh_mu.media_name))
                        pending_user_notices.append(
                            (
                                TrafficUnsuspendedPayload(
                                    server_name=srv_name,
                                    media_name=fresh_mu.media_name,
                                    recovery_reason="server_policy_change",
                                    next_reset_at=fresh_mu.next_reset_at,
                                    still_expiry_banned=False,
                                    economy=EffectiveEconomyContext.from_cache(
                                        fresh_srv
                                    ),
                                ),
                                uid,
                                sid,
                            )
                        )
                elif status == "retryable_error":
                    failed_count += 1
                    schedule_traffic_unsuspend_retry(
                        self._get_scheduler(),
                        uid,
                        sid,
                        run_date=self._now() + timedelta(minutes=15),
                    )
                    await self.media_repo.session.commit()

        for payload, uid, sid in pending_user_notices:
            await self._emit_notification(
                payload,
                user_id=uid,
                server_id=sid,
                source_key=f"traffic_unsuspended:{uid}:{sid}:server_policy_change",
            )

        if released_count > 0 or failed_count > 0:
            if server_id is not None:
                srv_obj = await self.server_repo.get_by_id(server_id, fresh=True)
                summary_server_name = (
                    srv_obj.name if srv_obj is not None else f"Server {server_id}"
                )
            else:
                summary_server_name = "全局全部服务器"
            await self._emit_notification(
                BatchTrafficReleaseSummaryPayload(
                    server_name=summary_server_name,
                    trigger_reason=trigger_reason,
                    released_count=released_count,
                    skipped_banned_count=skipped_banned_count,
                    failed_count=failed_count,
                    released_accounts=released_accounts,
                ),
                server_id=server_id,
                source_key=f"batch_traffic_release:{server_id if server_id is not None else 'global'}",
            )

        return released_count, failed_count

    async def adjust_account_cycle_traffic(
        self,
        user_id: int,
        server_id: int,
        *,
        delta_gib: float = 0.0,
        reset_cycle_usage: bool = False,
        operator_name: str = "管理员",
    ) -> Result:
        """管理员调整指定媒体账号当前账期临时额度（±GiB）或清零当期已用流量，并按 ADR-0002 安全解封

        操作前强制调用 ``flush_pending_traffic()`` 刷盘内存缓冲；仅影响当前账期
        （``cycle_traffic_adjustment_bytes`` / ``cycle_traffic_bytes``），绝不清空历史总流量
        ``total_traffic_bytes``，且清零已用时不清除用户购买的当期流量包余额 ``traffic_pack_bytes``。

        Args:
            user_id: 目标 Telegram 用户 ID。
            server_id: 目标媒体服务器 ID。
            delta_gib: 当期临时配额增减量（GiB，可正可负）。
            reset_cycle_usage: 是否同步清零当期已用流量、既有调额与预警标记。
            operator_name: 发起调额的管理员显示名称。

        Returns:
            操作结果 ``Result``，``extra_data`` 附带调额后流量与解封状态明细。
        """
        if not math.isfinite(delta_gib):
            return Result(False, "调额 GiB 数值无效。")
        delta_bytes = round(delta_gib * BYTES_PER_GIB)
        if not reset_cycle_usage and delta_bytes == 0:
            return Result(False, "请指定非零的临时调额 GiB 或选择清零当期已用流量。")

        await self.flush_pending_traffic()
        now = self._now()
        scheduler = self._get_scheduler()

        unsuspended_payload: TrafficUnsuspendedPayload | None = None
        admin_payload: AdminUserActionPayload | None = None
        result_obj: Result

        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            fresh_mu = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if fresh_mu is None:
                return Result(False, "目标媒体账号不存在。")
            fresh_srv = await self.server_repo.get_by_id(server_id, fresh=True)
            if fresh_srv is None:
                return Result(False, "所属媒体服务器不存在。")
            global_traffic_on = ConfigRepository.is_traffic_enabled_from_cache()
            if not global_traffic_on:
                return Result(False, "全局流量采集总开关已关闭，不支持当期流量调额。")
            if not fresh_srv.is_enabled or not fresh_srv.enable_traffic:
                return Result(
                    False, "该服务器已停用或未开启流量采集，不支持当期流量调额。"
                )
            if max(0, int(fresh_srv.traffic_quota_bytes or 0)) <= 0:
                return Result(False, "该服务器为不限流模式，无需当期流量调额或清零。")

            if reset_cycle_usage:
                fresh_mu.cycle_traffic_bytes = 0
                fresh_mu.cycle_traffic_adjustment_bytes = 0
                fresh_mu.traffic_alert_80_sent = False
                fresh_mu.traffic_alert_100_sent = False

            if delta_bytes != 0:
                fresh_mu.cycle_traffic_adjustment_bytes = (
                    int(fresh_mu.cycle_traffic_adjustment_bytes or 0) + delta_bytes
                )

            should_release, bal = self._should_release_suspension_locked(
                global_traffic_on, fresh_srv, fresh_mu
            )
            self._sync_traffic_alert_flags_locked(fresh_mu, bal)

            was_suspended = bool(fresh_mu.is_traffic_suspended)
            unsuspended = False
            retry_scheduled = False
            cleared_while_banned = False
            if was_suspended and should_release:
                status = await self._clear_or_sync_traffic_suspension_locked(
                    fresh_mu,
                    fresh_srv,
                    log_context="管理员当期流量调额解封",
                )
                if status == "cleared":
                    if fresh_mu.is_banned:
                        cleared_while_banned = True
                    else:
                        unsuspended = True
                elif status == "retryable_error":
                    retry_scheduled = True
                    schedule_traffic_unsuspend_retry(
                        scheduler,
                        user_id,
                        server_id,
                        run_date=now + timedelta(minutes=15),
                    )

            await self.media_repo.session.commit()
            await self.media_repo.session.refresh(fresh_mu)

            economy = EffectiveEconomyContext.from_cache(fresh_srv)
            if unsuspended:
                unsuspended_payload = TrafficUnsuspendedPayload(
                    server_name=fresh_srv.name,
                    media_name=fresh_mu.media_name,
                    recovery_reason="admin_adjust",
                    adjustment_bytes=delta_bytes,
                    reset_cycle_usage=reset_cycle_usage,
                    next_reset_at=fresh_mu.next_reset_at,
                    still_expiry_banned=False,
                    economy=economy,
                )
            admin_payload = AdminUserActionPayload(
                action_type="traffic_adjust",
                target_user_id=user_id,
                target_user_name="",
                operator_name=operator_name,
                server_name=fresh_srv.name,
                media_name=fresh_mu.media_name,
                traffic_delta_bytes=delta_bytes if delta_bytes != 0 else None,
                reset_cycle_usage=reset_cycle_usage,
                unsuspended_traffic=unsuspended,
                economy=economy,
            )

            quota_text = (
                format_traffic_bytes(bal.effective_base_quota_bytes)
                if bal.is_limited
                else "不限流量"
            )
            remain_text = (
                format_traffic_bytes(bal.total_remaining_bytes or 0)
                if bal.total_remaining_bytes is not None
                else "不限"
            )
            actions: list[str] = []
            if reset_cycle_usage:
                actions.append("已清零当期已用流量")
            if delta_bytes != 0:
                sign = "+" if delta_bytes > 0 else "-"
                actions.append(
                    f"当期临时调额 {sign}{format_traffic_bytes(abs(delta_bytes))}"
                )
            suffix = ""
            if unsuspended:
                suffix = "，并已自动解除超流停用"
            elif cleared_while_banned:
                suffix = "，已清除超流停用标记（因账号仍处于过期封禁，外部保持禁用）"
            elif retry_scheduled:
                suffix = "，外部媒体服解封暂时失败，已登记自动重试任务"

            msg = (
                f"{'、'.join(actions)}{suffix}。"
                f"当前生效配额: {quota_text}，当期总剩余: {remain_text}。"
            )
            result_obj = Result(
                True,
                msg,
                extra_data={
                    "effective_quota_bytes": (
                        bal.effective_base_quota_bytes if bal.is_limited else 0
                    ),
                    "cycle_traffic_bytes": int(fresh_mu.cycle_traffic_bytes),
                    "cycle_traffic_adjustment_bytes": int(
                        fresh_mu.cycle_traffic_adjustment_bytes
                    ),
                    "traffic_remaining_bytes": bal.base_remaining_bytes,
                    "traffic_total_remaining_bytes": bal.total_remaining_bytes,
                    "is_traffic_suspended": bool(fresh_mu.is_traffic_suspended),
                    "unsuspended": unsuspended,
                },
            )

        if unsuspended_payload is not None:
            await self._emit_notification(
                unsuspended_payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"traffic_unsuspended:{user_id}:{server_id}:admin_adjust",
            )
        if admin_payload is not None:
            target_name = await self._safe_resolve_user_name(user_id)
            if target_name:
                admin_payload = dataclass_replace(
                    admin_payload, target_user_name=target_name
                )
            await self._emit_notification(
                admin_payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"admin_traffic_adjust:{user_id}:{server_id}",
            )
        return result_obj

    async def flush_pending_traffic(self) -> None:
        """在执行账期重置、切换流量开关或管理员调额前尽力将内存中的在途流量增量刷盘。"""
        await flush_app_traffic(self.app)

    async def _clear_or_sync_traffic_suspension_locked(
        self,
        fresh_mu: MediaUser,
        fresh_srv: ServerInstance | None,
        *,
        log_context: str,
    ) -> Literal["cleared", "not_suspended", "server_disabled", "retryable_error"]:
        """在单账号串行锁内统一判定并清除超流停用标记，遵循 ADR-0002 双状态隔离原则。

        若账号同时处于过期封禁（``is_banned=True``），仅清除 ``is_traffic_suspended`` 而不唤醒
        外部账户；若账号未封禁，则仅在所属服务器启用且外部媒体服务器成功恢复后才清除标记。

        Args:
            fresh_mu: 锁内强制刷新的媒体账号对象。
            fresh_srv: 锁内强制刷新的服务器实例对象。
            log_context: 同步外部状态时的中文日志上下文说明。

        Returns:
            ``"cleared"``、``"not_suspended"``、``"server_disabled"`` 或 ``"retryable_error"``。
        """
        uid = int(fresh_mu.id)
        sid = int(fresh_mu.server_id)
        scheduler = self._get_scheduler()

        if not fresh_mu.is_traffic_suspended:
            clear_traffic_unsuspend_job(scheduler, uid, sid)
            return "not_suspended"

        keep_disabled = compute_external_disabled_state(
            is_banned=bool(fresh_mu.is_banned),
            is_traffic_suspended=False,
        )
        if keep_disabled:
            # 仍处于过期封禁时仅安全清除超流标记，绝不在外部媒体服解封（遵循 ADR-0002）
            fresh_mu.is_traffic_suspended = False
            clear_traffic_unsuspend_job(scheduler, uid, sid)
            return "cleared"

        if fresh_srv is None or not fresh_srv.is_enabled:
            # 已停用或已下线服务器不再保留运行期客户端，跳过外部通信以避免产生无效告警
            return "server_disabled"

        outcome = await sync_external_disabled_state(
            self.media_clients.get(sid),
            media_id=fresh_mu.media_id,
            target_disabled=False,
            treat_404_as_synced=True,
            log_context=log_context,
            user_id=uid,
            server_id=sid,
        )
        if outcome.ok:
            fresh_mu.is_traffic_suspended = False
            clear_traffic_unsuspend_job(scheduler, uid, sid)
            return "cleared"
        return "retryable_error"

    async def _reset_single_account_traffic_locked(
        self,
        fresh_mu: MediaUser,
        fresh_srv: ServerInstance | None,
        now: datetime,
        *,
        pending_notifications: list[tuple[NotificationPayload, int, int, str]]
        | None = None,
    ) -> Literal["reset", "reset_retry_unsuspend", "skipped"]:
        """在已持有账号串行锁的前提下执行单账号锚定月度流量清零与安全解封

        Args:
            fresh_mu: 锁内强制刷新的媒体账号对象。
            fresh_srv: 锁内强制刷新的服务器实例对象。
            now: 当前基准时间。
            pending_notifications: 可选收集锁外发射通知载荷的列表。

        Returns:
            ``"reset"`` 表示完成重置并无需解封重试；``"reset_retry_unsuspend"`` 表示已推进账期但
            外部解除超流停用暂失败需短期重试；``"skipped"`` 表示未到点或仅回填锚定基准。
        """
        scheduler = self._get_scheduler()
        srv_enabled = fresh_srv is not None and fresh_srv.is_enabled

        # 缺失锚定或周期边界的老账号先平滑补全后再按新边界调度，避免误清零
        if (
            fresh_mu.anchor_day is None
            or fresh_mu.cycle_started_at is None
            or fresh_mu.next_reset_at is None
        ):
            anchor_day, cycle_started_at, next_reset_at = backfill_billing_cycle(
                current_expires_at=fresh_mu.expires_at,
                anchor_day=fresh_mu.anchor_day,
                current_cycle_started_at=fresh_mu.cycle_started_at,
                current_next_reset_at=fresh_mu.next_reset_at,
                now=now,
            )
            fresh_mu.anchor_day = anchor_day
            fresh_mu.cycle_started_at = cycle_started_at
            fresh_mu.next_reset_at = next_reset_at
            await self.media_repo.session.commit()
            await self.media_repo.session.refresh(fresh_mu)
            if srv_enabled:
                schedule_account_lifecycle_jobs(scheduler, fresh_mu, now=now)
            return "skipped"

        if fresh_mu.next_reset_at > now:
            if srv_enabled:
                schedule_account_lifecycle_jobs(scheduler, fresh_mu, now=now)
            return "skipped"

        was_suspended = bool(fresh_mu.is_traffic_suspended)
        was_banned = bool(fresh_mu.is_banned)
        cleared_pack_bytes = max(0, int(fresh_mu.traffic_pack_bytes or 0))
        cleared_adjustment_bytes = int(fresh_mu.cycle_traffic_adjustment_bytes or 0)

        new_started_at, new_next_reset_at = resolve_anchored_cycle_window(
            now, fresh_mu.anchor_day, fresh_mu.next_reset_at
        )
        # 重置当期基础消耗、当期临时调额、当期流量包余额及预警标记；永久保留历史总流量与观影时长
        fresh_mu.cycle_traffic_bytes = 0
        fresh_mu.cycle_traffic_adjustment_bytes = 0
        fresh_mu.traffic_pack_bytes = 0
        fresh_mu.traffic_alert_80_sent = False
        fresh_mu.traffic_alert_100_sent = False
        fresh_mu.cycle_started_at = new_started_at
        fresh_mu.next_reset_at = new_next_reset_at

        unsuspend_status = await self._clear_or_sync_traffic_suspension_locked(
            fresh_mu,
            fresh_srv,
            log_context="账期重置解除超流停用",
        )

        await self.media_repo.session.commit()
        await self.media_repo.session.refresh(fresh_mu)
        if srv_enabled:
            schedule_account_lifecycle_jobs(scheduler, fresh_mu, now=now)

        if pending_notifications is not None and fresh_srv is not None:
            uid = int(fresh_mu.id)
            sid = int(fresh_mu.server_id)
            economy = EffectiveEconomyContext.from_cache(fresh_srv)
            if was_suspended and unsuspend_status == "cleared" and not was_banned:
                pending_notifications.append(
                    (
                        TrafficUnsuspendedPayload(
                            server_name=fresh_srv.name,
                            media_name=fresh_mu.media_name,
                            recovery_reason="cycle_reset",
                            next_reset_at=fresh_mu.next_reset_at,
                            still_expiry_banned=False,
                            economy=economy,
                        ),
                        uid,
                        sid,
                        f"traffic_unsuspended:{uid}:{sid}:cycle_reset",
                    )
                )
            elif not was_suspended:
                pending_notifications.append(
                    (
                        BillingCycleResetPayload(
                            server_name=fresh_srv.name,
                            media_name=fresh_mu.media_name,
                            reset_at=now,
                            next_reset_at=fresh_mu.next_reset_at,
                            cleared_pack_bytes=cleared_pack_bytes,
                            cleared_adjustment_bytes=cleared_adjustment_bytes,
                            new_quota_bytes=max(
                                0, int(fresh_srv.traffic_quota_bytes or 0)
                            ),
                            unsuspended_traffic=False,
                            economy=economy,
                        ),
                        uid,
                        sid,
                        f"billing_cycle_reset:{uid}:{sid}",
                    )
                )

        if unsuspend_status == "retryable_error" and srv_enabled:
            return "reset_retry_unsuspend"
        return "reset"

    async def _release_orphan_traffic_suspension_locked(
        self,
        fresh_mu: MediaUser,
        fresh_srv: ServerInstance | None,
    ) -> bool:
        """在锁内检查仍标记为超流停用的账号是否已拥有可用流量或已关停限流开关并安全释放

        Returns:
            成功清除了超流停用标记返回 ``True``，否则返回 ``False``。
        """
        if not fresh_mu.is_traffic_suspended:
            clear_traffic_unsuspend_job(
                self._get_scheduler(), int(fresh_mu.id), int(fresh_mu.server_id)
            )
            return False

        global_traffic_on = ConfigRepository.is_traffic_enabled_from_cache()
        should_release, bal = self._should_release_suspension_locked(
            global_traffic_on, fresh_srv, fresh_mu
        )
        if not should_release:
            return False

        self._sync_traffic_alert_flags_locked(fresh_mu, bal)

        status = await self._clear_or_sync_traffic_suspension_locked(
            fresh_mu,
            fresh_srv,
            log_context="兜底巡检解除残余超流停用",
        )
        if status != "cleared":
            return False

        await self.media_repo.session.commit()
        return True

    async def _reconcile_account_traffic_step_locked(
        self,
        fresh_mu: MediaUser,
        fresh_srv: ServerInstance | None,
        now: datetime,
        scheduler: Any,
        *,
        pending_notifications: list[tuple[NotificationPayload, int, int, str]]
        | None = None,
    ) -> tuple[bool, bool]:
        """在已持有单账号锁时执行遗漏账期重置与残余超流释放对账，并在需要时登记解封重试。

        Returns:
            ``(reset_done, unsuspended_done)`` 二元组。
        """
        reset_done = False
        retry_unsuspend = False
        if fresh_mu.next_reset_at is None or fresh_mu.next_reset_at <= now:
            reset_status = await self._reset_single_account_traffic_locked(
                fresh_mu,
                fresh_srv,
                now,
                pending_notifications=pending_notifications,
            )
            reset_done = reset_status in ("reset", "reset_retry_unsuspend")
            retry_unsuspend = reset_status == "reset_retry_unsuspend"

        unsuspended = False
        if fresh_mu.is_traffic_suspended:
            was_banned = bool(fresh_mu.is_banned)
            unsuspended = await self._release_orphan_traffic_suspension_locked(
                fresh_mu, fresh_srv
            )
            if (
                unsuspended
                and pending_notifications is not None
                and fresh_srv is not None
                and not was_banned
            ):
                uid = int(fresh_mu.id)
                sid = int(fresh_mu.server_id)
                pending_notifications.append(
                    (
                        TrafficUnsuspendedPayload(
                            server_name=fresh_srv.name,
                            media_name=fresh_mu.media_name,
                            recovery_reason="server_policy_change",
                            next_reset_at=fresh_mu.next_reset_at,
                            still_expiry_banned=False,
                            economy=EffectiveEconomyContext.from_cache(fresh_srv),
                        ),
                        uid,
                        sid,
                        f"traffic_unsuspended:{uid}:{sid}:orphan_release",
                    )
                )

        if retry_unsuspend and fresh_srv is not None and fresh_srv.is_enabled:
            schedule_traffic_unsuspend_retry(
                scheduler,
                int(fresh_mu.id),
                int(fresh_mu.server_id),
                run_date=now + timedelta(minutes=15),
            )
        return reset_done, unsuspended

    async def reconcile_missed_traffic_resets(self) -> int:
        """兜底巡检遗漏的月度流量重置、残余超流停用释放及缺失生命周期任务补全

        Returns:
            本次成功重置账期或释放残余超流停用的账号数量。
        """
        await self.flush_pending_traffic()
        now = self._now()
        await self.media_repo.backfill_missing_billing_anchors(now=now)

        target_pairs = await self.media_repo.list_account_pairs()
        await self.media_repo.end_clean_read_tx()

        healed_count = 0
        scheduler = self._get_scheduler()
        pending_notifications: list[tuple[NotificationPayload, int, int, str]] = []
        for uid, sid in target_pairs:
            async with get_account_lock(uid, sid):
                await self.media_repo.end_clean_read_tx()
                fresh_mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
                if fresh_mu is None:
                    remove_account_lifecycle_jobs(scheduler, uid, sid)
                    continue
                fresh_srv = await self.server_repo.get_by_id(sid, fresh=True)
                (
                    reset_done,
                    unsuspended,
                ) = await self._reconcile_account_traffic_step_locked(
                    fresh_mu,
                    fresh_srv,
                    now,
                    scheduler,
                    pending_notifications=pending_notifications,
                )
                if reset_done or unsuspended:
                    healed_count += 1

                if fresh_srv is not None and fresh_srv.is_enabled:
                    schedule_account_lifecycle_jobs(scheduler, fresh_mu, now=now)
                else:
                    remove_account_lifecycle_jobs(scheduler, uid, sid)

        for payload, uid, sid, src_key in pending_notifications:
            await self._emit_notification(
                payload, user_id=uid, server_id=sid, source_key=src_key
            )
        return healed_count

    async def calibrate_account_lifecycles(
        self, *, server_id: int | None = None
    ) -> dict[str, int]:
        """校准存量与恢复期媒体账号的锚定周期、遗漏状态与精准调度节点

        副作用：
        - 为缺少锚定基准或 ``delete_at`` 的老账号平滑补全（支持按 ``server_id`` 过滤以适配单服启用）；
        - 清理调度器中已无对应数据库记录或已所属停用服务器的孤立单次任务；
        - 按单账号锁逐个补齐停机期间错过的账期重置、到期封禁、宽限删号及临近窗口自动保活，
          并为启用服务器下的存活账号重新注册/同步未来的单次调度节点；停用服务器跳过外部通信并清除调度节点。

        Args:
            server_id: 可选仅校准指定服务器 ID；为 ``None`` 时全平台全量校准。

        Returns:
            统计各项恢复与校准数量的字典。
        """
        await self.flush_pending_traffic()
        now = self._now()
        scheduler = self._get_scheduler()
        summary = {
            "backfilled_anchors": await self.media_repo.backfill_missing_billing_anchors(
                now=now, server_id=server_id
            ),
            "backfilled_delete_at": await self.media_repo.backfill_missing_delete_at(
                now=now, server_id=server_id
            ),
            "resets": 0,
            "unsuspended": 0,
            "auto_renewed": 0,
            "banned": 0,
            "deleted": 0,
            "calibrated": 0,
            "orphan_jobs_removed": 0,
        }

        target_pairs = await self.media_repo.list_account_pairs(server_id=server_id)
        await self.media_repo.end_clean_read_tx()

        if server_id is None and scheduler is not None:
            # 全局全量校准时同步剔除库中已不存在对应 MediaUser 记录的残留孤立任务
            valid_pairs = set(target_pairs)
            for job in list(scheduler.get_jobs()):
                job_id = getattr(job, "id", "")
                if not isinstance(job_id, str):
                    continue
                parsed = parse_lifecycle_job_account(job_id)
                if parsed is not None and parsed not in valid_pairs:
                    remove_account_lifecycle_jobs(scheduler, parsed[0], parsed[1])
                    summary["orphan_jobs_removed"] += 1

        pending_notifications: list[tuple[NotificationPayload, int, int, str]] = []
        for uid, sid in target_pairs:
            async with get_account_lock(uid, sid):
                await self.media_repo.end_clean_read_tx()
                fresh_mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
                if fresh_mu is None:
                    remove_account_lifecycle_jobs(scheduler, uid, sid)
                    continue
                fresh_srv = await self.server_repo.get_by_id(sid, fresh=True)
                (
                    reset_done,
                    unsuspended,
                ) = await self._reconcile_account_traffic_step_locked(
                    fresh_mu,
                    fresh_srv,
                    now,
                    scheduler,
                    pending_notifications=pending_notifications,
                )
                if reset_done:
                    summary["resets"] += 1
                if unsuspended:
                    summary["unsuspended"] += 1

                is_banned = bool(fresh_mu.is_banned)
                expires_at = fresh_mu.expires_at
                delete_at = fresh_mu.delete_at
                srv_enabled = bool(fresh_srv is not None and fresh_srv.is_enabled)

            # 停用服务器已断开外部客户端连接，禁止发外部删号/封禁/自动续期请求，仅保留数据并在末段移除其调度节点
            if not srv_enabled:
                remove_account_lifecycle_jobs(scheduler, uid, sid)
                continue

            # get_account_lock 为非重入 asyncio.Lock，释放锁后再调用同样内部加锁的生命周期入口以防自死锁
            if is_banned and delete_at is not None and delete_at <= now:
                if await self.delete_expired_banned_account(uid, sid) == "deleted":
                    summary["deleted"] += 1
                    continue
            elif not is_banned and expires_at <= now:
                # 已到期的账号由 ban_expired_account 先复核自动续期（达标时长/积分），不满足再封禁
                ban_res = await self.ban_expired_account(uid, sid)
                if ban_res == "renewed":
                    summary["auto_renewed"] += 1
                elif ban_res == "banned":
                    summary["banned"] += 1

            async with get_account_lock(uid, sid):
                await self.media_repo.end_clean_read_tx()
                final_mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
                if final_mu is None:
                    remove_account_lifecycle_jobs(scheduler, uid, sid)
                    continue
                final_srv = await self.server_repo.get_by_id(sid, fresh=True)
                if final_srv is not None and final_srv.is_enabled:
                    schedule_account_lifecycle_jobs(scheduler, final_mu, now=now)
                    summary["calibrated"] += 1
                else:
                    remove_account_lifecycle_jobs(scheduler, uid, sid)

        for payload, uid, sid, src_key in pending_notifications:
            await self._emit_notification(
                payload, user_id=uid, server_id=sid, source_key=src_key
            )

        return summary

    async def reset_account_traffic_cycle(
        self, user_id: int, server_id: int
    ) -> Literal["reset", "retry_unsuspend", "skipped"]:
        """执行单个媒体账号的准点月度账期重置并自注册下一次重置任务

        副作用：重置前先刷新 ``TrafficService`` 内存缓冲，随后按锚定自然月推进
        ``next_reset_at``、清零当期基础流量与预警标记（流量包余额不变），并在未处于
        过期封禁时恢复外部媒体服务器权限。若外部解除超流停用因临时故障失败，返回
        ``"retry_unsuspend"`` 以触发短期延迟解封重试。

        Args:
            user_id: Telegram 用户 ID。
            server_id: 目标媒体服务器 ID。

        Returns:
            ``"reset"``、``"retry_unsuspend"`` 或 ``"skipped"``。
        """
        await self.flush_pending_traffic()
        pending_notifications: list[tuple[NotificationPayload, int, int, str]] = []
        outcome: Literal["reset", "retry_unsuspend", "skipped"]
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            fresh_mu = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if fresh_mu is None:
                remove_account_lifecycle_jobs(self._get_scheduler(), user_id, server_id)
                return "skipped"
            fresh_srv = await self.server_repo.get_by_id(server_id, fresh=True)
            res = await self._reset_single_account_traffic_locked(
                fresh_mu,
                fresh_srv,
                self._now(),
                pending_notifications=pending_notifications,
            )
            outcome = "retry_unsuspend" if res == "reset_retry_unsuspend" else res

        for payload, uid, sid, src_key in pending_notifications:
            await self._emit_notification(
                payload, user_id=uid, server_id=sid, source_key=src_key
            )
        return outcome

    async def release_account_traffic_suspension(
        self, user_id: int, server_id: int
    ) -> Literal["released", "skipped", "retryable_error"]:
        """短期延迟重试解除账期重置时遗留的超流停用状态并同步恢复外部媒体权限

        Args:
            user_id: Telegram 用户 ID。
            server_id: 目标媒体服务器 ID。

        Returns:
            ``"released"``、``"skipped"`` 或 ``"retryable_error"``。
        """
        scheduler = self._get_scheduler()
        unsuspended_payload: TrafficUnsuspendedPayload | None = None
        result_status: Literal["released", "skipped", "retryable_error"]
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            fresh_mu = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if fresh_mu is None:
                remove_account_lifecycle_jobs(scheduler, user_id, server_id)
                return "skipped"
            fresh_srv = await self.server_repo.get_by_id(server_id, fresh=True)
            if fresh_srv is None or not fresh_srv.is_enabled:
                remove_account_lifecycle_jobs(scheduler, user_id, server_id)
                return "skipped"

            now = self._now()
            was_suspended = bool(fresh_mu.is_traffic_suspended)
            was_banned = bool(fresh_mu.is_banned)
            # 仅当当前周期尚未再度超流耗尽或账期已推入新周期后允许释放
            status = await self._clear_or_sync_traffic_suspension_locked(
                fresh_mu,
                fresh_srv,
                log_context="账期重置后延迟重试解除超流停用",
            )
            if status == "cleared":
                await self.media_repo.session.commit()
                await self.media_repo.session.refresh(fresh_mu)
                schedule_account_lifecycle_jobs(scheduler, fresh_mu, now=now)
                if was_suspended and not was_banned:
                    unsuspended_payload = TrafficUnsuspendedPayload(
                        server_name=fresh_srv.name,
                        media_name=fresh_mu.media_name,
                        recovery_reason="cycle_reset",
                        next_reset_at=fresh_mu.next_reset_at,
                        still_expiry_banned=False,
                        economy=EffectiveEconomyContext.from_cache(fresh_srv),
                    )
                result_status = "released"
            elif status == "retryable_error":
                return "retryable_error"
            else:
                return "skipped"

        if unsuspended_payload is not None:
            await self._emit_notification(
                unsuspended_payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"traffic_unsuspended:{user_id}:{server_id}:cycle_reset",
            )
        return result_status

    async def process_pre_expiry_check(
        self,
        user_id: int,
        server_id: int,
        *,
        pre_expiry_window: timedelta = timedelta(hours=24),
    ) -> Literal[
        "reminded",
        "not_due",
        "already_expired",
        "banned",
        "missing",
        "server_disabled",
    ]:
        """处理单个媒体账号临近到期节点检查：仅评估并推送当前续期条件与到期提醒

        到达有效期的实际自动续期统一推迟至 ``expires_at`` 到期节点（由 ``ban_expired_account``
        前置复核或过期扫描触发），确保用户能在到期前充分利用剩余时间达成免分观影条件。

        Args:
            user_id: Telegram 用户 ID。
            server_id: 目标媒体服务器 ID。
            pre_expiry_window: 临近到期判定窗口（默认到期前 24 小时）。

        Returns:
            执行结果状态字面量，便于单次任务调度器与测试观测结果分流。
        """
        reminder_payload: PreExpiryReminderPayload | None = None
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            fresh_mu = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            if fresh_mu is None:
                remove_account_lifecycle_jobs(self._get_scheduler(), user_id, server_id)
                return "missing"
            if fresh_mu.is_banned:
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), fresh_mu, now=self._now()
                )
                return "banned"

            now = self._now()
            if fresh_mu.expires_at > now + pre_expiry_window:
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), fresh_mu, now=now
                )
                return "not_due"
            if fresh_mu.expires_at <= now:
                return "already_expired"

            fresh_srv = await self.server_repo.get_by_id(server_id, fresh=True)
            if fresh_srv is None or not fresh_srv.is_enabled:
                return "server_disabled"

            wt_status = evaluate_watch_time_renewal(
                fresh_srv,
                fresh_mu,
                global_watch_time_enabled=await self.config_repo.is_watch_time_enabled(),
            )
            economy = EffectiveEconomyContext.from_cache(fresh_srv)
            auto_renew_flag = bool(fresh_mu.auto_renew)
            points_renew_score: int | None = None
            user_score: int | None = None
            if economy.effective_points_renew:
                points_renew_score = max(
                    0, int(await self.telegram_repo.get_renew_score())
                )
                if auto_renew_flag:
                    tg_user = await self.telegram_repo.get_by_id(user_id)
                    user_score = int(tg_user.score) if tg_user is not None else 0

            reminder_payload = PreExpiryReminderPayload(
                server_name=fresh_srv.name,
                media_name=fresh_mu.media_name,
                expires_at=fresh_mu.expires_at,
                auto_renew_enabled=auto_renew_flag,
                watch_eligible=wt_status.is_eligible,
                watch_status_text=wt_status.status_text,
                points_renew_score=points_renew_score,
                user_score=user_score,
                economy=economy,
            )

        # 释放非重入账号锁后再投递统一通知管线，防止阻塞同账号其他操作
        if reminder_payload is not None:
            await self._emit_notification(
                reminder_payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"pre_expiry:{user_id}:{server_id}",
            )
        return "reminded"

    async def ban_expired_account(
        self,
        user_id: int,
        server_id: int,
        *,
        check_auto_renew: bool = True,
    ) -> Literal["renewed", "banned", "skipped", "retryable_error"]:
        """对单个到期媒体账号执行到期自动续期复核与过期封禁落库及宽限删号调度

        Args:
            user_id: Telegram 用户 ID。
            server_id: 目标媒体服务器 ID。
            check_auto_renew: 是否在封禁前复核一次自动续期资格（默认 ``True``）。

        Returns:
            ``"renewed"``、``"banned"``、``"skipped"`` 或 ``"retryable_error"``。
        """
        # 先在单账号锁内校验是否已失效/并发续期；由于后续可能调用内部同样申请 get_account_lock 的
        # try_auto_renew_account，而 asyncio.Lock 非重入，这里校验后需立即释放锁以防自死锁
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            pre_mu = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            pre_now = self._now()
            if pre_mu is None:
                remove_account_lifecycle_jobs(self._get_scheduler(), user_id, server_id)
                return "skipped"
            if pre_mu.is_banned or pre_mu.expires_at > pre_now:
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), pre_mu, now=pre_now
                )
                return "skipped"

        failed_auto_outcome: AutoRenewOutcome | None = None
        if check_auto_renew:
            auto_outcome = await self.try_auto_renew_account(
                user_id,
                server_id,
                anchor_rescue_grace=EXPIRY_RESCUE_JITTER_GRACE,
            )
            if auto_outcome.renewed:
                logger.info(
                    "到期封禁前复核触发自动续期成功，跳过封禁媒体账号: {} Server: {}",
                    user_id,
                    server_id,
                )
                return "renewed"
            if auto_outcome.retryable_error:
                logger.warning(
                    "自动续期媒体账号外部核验临时失败，本次跳过封禁等待重试: {} Server: {}",
                    user_id,
                    server_id,
                )
                return "retryable_error"
            if auto_outcome.failure_notifiable:
                failed_auto_outcome = auto_outcome

        pending_payloads: list[NotificationPayload] = []
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            fresh_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            now = self._now()
            if fresh_user is None:
                remove_account_lifecycle_jobs(self._get_scheduler(), user_id, server_id)
                return "skipped"
            if fresh_user.is_banned or fresh_user.expires_at > now:
                logger.info(
                    "媒体账号状态已变更或并发完成续期，跳过本次过期封禁: {} (ID: {}) Server: {}",
                    user_id,
                    fresh_user.media_id,
                    server_id,
                )
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), fresh_user, now=now
                )
                return "skipped"

            client = self.media_clients.get(fresh_user.server_id)
            if not client:
                logger.warning(
                    "未找到服务器实例(ID: {})，跳过封禁过期媒体账号: {} (ID: {})",
                    fresh_user.server_id,
                    fresh_user.id,
                    fresh_user.media_id,
                )
                return "retryable_error"

            try:
                logger.info(
                    "封禁过期媒体账号: {} (ID: {}) Server: {}",
                    fresh_user.id,
                    fresh_user.media_id,
                    fresh_user.server_id,
                )
                await client.ban_or_unban(user_id=fresh_user.media_id, is_ban=True)
            except HTTPStatusError as e:
                if e.response.status_code == 404:
                    # 媒体服务器账户已不存在时，仍标记封禁并按到期时间+宽限天数进入待删除队列
                    logger.warning(
                        "封禁过期媒体账号时媒体服务器账户不存在(404)，标记待删除: {} (ID: {})",
                        fresh_user.id,
                        fresh_user.media_id,
                    )
                else:
                    logger.error(
                        "封禁过期媒体账号失败，保留未封禁状态以便重试: {} (ID: {})，状态码: {}",
                        fresh_user.id,
                        fresh_user.media_id,
                        e.response.status_code,
                    )
                    return "retryable_error"
            except (HTTPError, ValueError) as e:
                logger.error(
                    "封禁过期媒体账号失败，保留未封禁状态以便重试: {} (ID: {})，错误: {}",
                    fresh_user.id,
                    fresh_user.media_id,
                    e,
                )
                return "retryable_error"

            fresh_user = await self.media_repo.mark_banned(fresh_user)
            schedule_account_lifecycle_jobs(self._get_scheduler(), fresh_user, now=now)

            fresh_srv = await self.server_repo.get_by_id(server_id, fresh=True)
            server_name = (
                fresh_srv.name if fresh_srv is not None else f"Server {server_id}"
            )
            policy_cfg = await self.config_repo.get_policy_config()
            grace_days = max(0, int(policy_cfg["account_delete_grace_days"]))
            economy = EffectiveEconomyContext.from_cache(fresh_srv)
            points_renew_score = (
                max(0, int(await self.telegram_repo.get_renew_score()))
                if economy.effective_points_renew
                else None
            )
            if failed_auto_outcome is not None:
                pending_payloads.append(
                    AutoRenewFailedPayload(
                        server_name=server_name,
                        media_name=fresh_user.media_name,
                        expires_at=fresh_user.expires_at,
                        reason="",
                        grace_days=grace_days,
                        required_score=failed_auto_outcome.required_score,
                        user_score=failed_auto_outcome.user_score,
                        watch_status_text=failed_auto_outcome.watch_status_text,
                        will_ban=True,
                        economy=economy,
                    )
                )
            pending_payloads.append(
                ExpiryBanPayload(
                    server_name=server_name,
                    media_name=fresh_user.media_name,
                    expires_at=fresh_user.expires_at,
                    delete_at=fresh_user.delete_at,
                    grace_days=grace_days,
                    points_renew_score=points_renew_score,
                    economy=economy,
                )
            )

        for payload in pending_payloads:
            await self._emit_notification(
                payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"{payload.event.value}:{user_id}:{server_id}",
            )
        return "banned"

    async def delete_expired_banned_account(
        self, user_id: int, server_id: int
    ) -> Literal["deleted", "skipped", "retryable_error"]:
        """对单个宽限期届满的已过期封禁账号执行外部删除与本地清理

        严格检查 ``is_banned == True`` 且 ``delete_at <= now``，绝不误删仅处于
        超流停用（``is_traffic_suspended == True``）但未过期封禁的账号（遵循 ADR-0002）。

        Args:
            user_id: Telegram 用户 ID。
            server_id: 目标媒体服务器 ID。

        Returns:
            ``"deleted"``、``"skipped"`` 或 ``"retryable_error"``。
        """
        deleted_payload: AccountDeletedPayload | None = None
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            fresh_user = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            now = self._now()
            if fresh_user is None:
                remove_account_lifecycle_jobs(self._get_scheduler(), user_id, server_id)
                return "skipped"
            if not fresh_user.is_banned or fresh_user.delete_at is None:
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), fresh_user, now=now
                )
                return "skipped"
            if fresh_user.delete_at > now:
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), fresh_user, now=now
                )
                return "skipped"

            client = self.media_clients.get(fresh_user.server_id)
            if not client:
                logger.warning(
                    "未找到服务器实例(ID: {})，保留过期封禁媒体账号本地记录以便重试: {} (ID: {})",
                    fresh_user.server_id,
                    fresh_user.id,
                    fresh_user.media_id,
                )
                return "retryable_error"

            try:
                await client.delete_user(fresh_user.media_id)
            except HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.info(
                        "媒体服务器账户已不存在(404)，清理已过期封禁媒体账号本地记录: {} (ID: {})",
                        fresh_user.id,
                        fresh_user.media_id,
                    )
                else:
                    logger.error(
                        "删除已过期封禁媒体账号失败，保留本地记录以便重试: {} (ID: {})，状态码: {}",
                        fresh_user.id,
                        fresh_user.media_id,
                        e.response.status_code,
                    )
                    return "retryable_error"
            except (HTTPError, Exception) as e:
                logger.error(
                    "删除已过期封禁媒体账号失败，保留本地记录以便重试: {} (ID: {})，错误: {}",
                    fresh_user.id,
                    fresh_user.media_id,
                    e,
                )
                return "retryable_error"

            fresh_srv = await self.server_repo.get_by_id(server_id, fresh=True)
            server_name = (
                fresh_srv.name if fresh_srv is not None else f"Server {server_id}"
            )
            media_name = str(fresh_user.media_name)
            economy = EffectiveEconomyContext.from_cache(fresh_srv)
            await self.media_repo.delete(fresh_user)
            remove_account_lifecycle_jobs(self._get_scheduler(), user_id, server_id)
            deleted_payload = AccountDeletedPayload(
                server_name=server_name,
                media_name=media_name,
                reason="过期封禁宽限期届满未续期",
                deleted_at=now,
                economy=economy,
            )

        if deleted_payload is not None:
            await self._emit_notification(
                deleted_payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"account_deleted:{user_id}:{server_id}",
            )
        return "deleted"

    async def process_grace_delete_reminder(
        self,
        user_id: int,
        server_id: int,
        *,
        reminder_window: timedelta = timedelta(hours=24),
    ) -> Literal[
        "reminded",
        "not_due",
        "already_due",
        "already_sent",
        "not_banned",
        "missing",
        "server_disabled",
    ]:
        """在宽限期删号前 24 小时发送最终清理预警，并持久化去重标记防止重复触发。

        Args:
            user_id: Telegram 用户 ID。
            server_id: 目标媒体服务器 ID。
            reminder_window: 删号前预警提前窗口（默认 24 小时）。

        Returns:
            执行状态字面量，便于单次调度任务及测试观测结果分流。
        """
        payload: GraceDeleteReminderPayload | None = None
        async with get_account_lock(user_id, server_id):
            await self.media_repo.end_clean_read_tx()
            fresh_mu = await self.media_repo.get_by_id(user_id, server_id, fresh=True)
            now = self._now()
            if fresh_mu is None:
                remove_account_lifecycle_jobs(self._get_scheduler(), user_id, server_id)
                return "missing"
            if not fresh_mu.is_banned or fresh_mu.delete_at is None:
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), fresh_mu, now=now
                )
                return "not_banned"
            if fresh_mu.grace_delete_reminder_sent:
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), fresh_mu, now=now
                )
                return "already_sent"
            if fresh_mu.delete_at <= now:
                return "already_due"
            if fresh_mu.delete_at - reminder_window > now:
                schedule_account_lifecycle_jobs(
                    self._get_scheduler(), fresh_mu, now=now
                )
                return "not_due"

            fresh_srv = await self.server_repo.get_by_id(server_id, fresh=True)
            if fresh_srv is None or not fresh_srv.is_enabled:
                return "server_disabled"

            fresh_mu.grace_delete_reminder_sent = True
            await self.media_repo.session.commit()

            policy_cfg = await self.config_repo.get_policy_config()
            grace_days = max(0, int(policy_cfg["account_delete_grace_days"]))
            economy = EffectiveEconomyContext.from_cache(fresh_srv)
            points_renew_score = (
                max(0, int(await self.telegram_repo.get_renew_score()))
                if economy.effective_points_renew
                else None
            )
            remaining_hours = max(
                1, round((fresh_mu.delete_at - now).total_seconds() / 3600)
            )
            payload = GraceDeleteReminderPayload(
                server_name=fresh_srv.name,
                media_name=fresh_mu.media_name,
                delete_at=fresh_mu.delete_at,
                remaining_hours=remaining_hours,
                grace_days=grace_days,
                points_renew_score=points_renew_score,
                economy=economy,
            )

        if payload is not None:
            await NotificationService(self.app).emit(
                payload,
                user_id=user_id,
                server_id=server_id,
                source_key=f"grace_delete_reminder:{user_id}:{server_id}",
                session=self.media_repo.session,
            )
        return "reminded"
