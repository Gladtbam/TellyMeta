from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from httpx2 import HTTPError, HTTPStatusError
from loguru import logger
from pydantic import BaseModel, ConfigDict

from models.emby import MediaFolder
from models.protocols import BaseItem, DeviceInfo, Library, PublicSystemInfo, User


class ActiveSession(BaseModel):
    """统一的结构化活跃播放会话模型

    屏蔽 Emby 与 Jellyfin ``/Sessions`` 接口在用户标识、媒体项标识与播放状态上的
    数据结构差异，为 Emby 主动会话心跳与 Nginx 会话归因提供统一的上层契约。

    Attributes:
        user_id: 媒体平台用户 ID（Jellyfin UUID 统一归一化为标准字符串）。
        session_id: 会话 ID（对应 ``/Sessions`` 中的 ``Id``）。
        play_session_id: 播放会话 ID（来自 ``PlayState.PlaySessionId`` 或流地址参数，可用时提供）。
        device_id: 客户端设备 ID（``DeviceId``）。
        auth_token: 客户端认证令牌或 API Key（可用时提取，否则为 ``None``）。
        item_id: 当前正在播放的媒体项 ID（``NowPlayingItem.Id``）。
        is_paused: 当前是否处于暂停状态（缺失播放状态时默认为 ``False``）。
        position_ticks: 当前播放进度刻度（1 秒 = 10,000,000 ticks；不可用时为 ``None``）。
    """

    model_config = ConfigDict(frozen=True)

    user_id: str | None = None
    session_id: str | None = None
    play_session_id: str | None = None
    device_id: str | None = None
    auth_token: str | None = None
    item_id: str | None = None
    is_paused: bool = False
    position_ticks: int | None = None


class MediaService[
    UserT: User,
    BaseItemT_co: BaseItem,
    LibraryT: Library,
    DeviceInfoT: DeviceInfo,
    PublicSystemInfoT: PublicSystemInfo,
](ABC):
    """定义媒体服务的抽象基类"""

    server_name: str = "Unknown"
    notify_topic_id: int | None = None

    @abstractmethod
    async def create(self, name: str) -> tuple[UserT | None, str | None]:
        """创建用户"""
        raise NotImplementedError

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        """删除用户"""
        raise NotImplementedError

    @abstractmethod
    async def update_policy(
        self, user_id: str, policy: dict[str, Any], is_none: bool = False
    ) -> None:
        """更新用户策略"""
        raise NotImplementedError

    @abstractmethod
    async def get_item_info(self, item_id: str) -> BaseItemT_co | None:
        """获取媒体项信息"""
        raise NotImplementedError

    @abstractmethod
    async def post_item_info(self, item_id: str, item_info: BaseItem) -> None:
        """更新媒体项信息"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_info(self, user_id: str) -> UserT | None:
        """获取用户信息"""
        raise NotImplementedError

    @abstractmethod
    async def post_password(self, user_id: str, reset_password: bool = False) -> str:
        """更新用户密码"""
        raise NotImplementedError

    @abstractmethod
    async def ban_or_unban(self, user_id: str, is_ban: bool = True) -> None:
        """封禁或解封用户"""
        raise NotImplementedError

    @abstractmethod
    async def get_session_list(self) -> int:
        """获取用户在线数量"""
        raise NotImplementedError

    @abstractmethod
    async def get_active_sessions(self) -> list[ActiveSession]:
        """获取当前正在播放的结构化活跃会话列表

        仅返回包含 ``NowPlayingItem`` 的播放会话（含播放中与暂停中），过滤空闲会话。

        Returns:
            统一规范化后的 ``ActiveSession`` 列表。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_libraries(self) -> Sequence[LibraryT] | None:
        """获取媒体库列表"""
        raise NotImplementedError

    @abstractmethod
    async def get_selectable_media_folders(self) -> list[MediaFolder] | None:
        """获取 Emby 媒体文件夹
        仅 Emby
        """
        raise NotImplementedError

    @abstractmethod
    async def get_user_id_by_device_id(self, device_id: str) -> DeviceInfoT | None:
        """ "通过设备 ID 获取用户 ID"""
        raise NotImplementedError

    @abstractmethod
    async def get_system_info_public(self) -> PublicSystemInfoT | None:
        """获取公开信息"""
        raise NotImplementedError

    @abstractmethod
    def get_all_items(self) -> AsyncIterator[BaseItemT_co]:
        """获取所有媒体项的异步生成器"""
        raise NotImplementedError


def compute_external_disabled_state(
    *, is_banned: bool, is_traffic_suspended: bool
) -> bool:
    """计算外部媒体服务器账号组合禁用状态：过期封禁 OR 超流停用（遵循 ADR-0002）

    Args:
        is_banned: 账号是否处于过期封禁状态。
        is_traffic_suspended: 账号是否处于超流停用状态。

    Returns:
        外部服务器 ``IsDisabled`` 应保持的目标布尔状态。
    """
    return bool(is_banned or is_traffic_suspended)


@dataclass(frozen=True, slots=True)
class ExternalDisableSyncResult:
    """外部媒体服务器禁用态同步结果

    Attributes:
        ok: 是否已完成目标禁用态同步（含外部 404 按策略视为已同步的情况）。
        error: 同步失败时捕获的异常对象；客户端缺失或同步成功时为 ``None``。
        missing_client: 是否因媒体服务器客户端未连接而未执行外部请求。
        not_found: 外部媒体服务器是否返回 HTTP 404（账号已不存在）。
    """

    ok: bool
    error: Exception | None = None
    missing_client: bool = False
    not_found: bool = False


async def sync_external_disabled_state(
    client: MediaService[Any, Any, Any, Any, Any] | None,
    *,
    media_id: str,
    target_disabled: bool,
    treat_404_as_synced: bool = False,
    log_context: str = "同步媒体账号禁用态",
    user_id: int | None = None,
    server_id: int | None = None,
) -> ExternalDisableSyncResult:
    """统一执行外部媒体服务器禁用/解封调用及客户端缺失、404 与传输异常处理

    Args:
        client: 目标服务器的 ``MediaService`` 客户端实例（可为 ``None``）。
        media_id: 外部媒体账号 ID。
        target_disabled: 期望写入外部服务器的 ``IsDisabled`` 状态。
        treat_404_as_synced: 外部返回 HTTP 404 时是否视作目标状态已同步。
        log_context: 日志场景描述，用于区分续期、兑换、停用或批量释放调用方。
        user_id: 关联 Telegram 用户 ID（可选，仅用于日志）。
        server_id: 关联媒体服务器 ID（可选，仅用于日志）。

    Returns:
        包含成功标志、异常对象与客户端缺失/404 状态的 ``ExternalDisableSyncResult``。
    """
    if client is None:
        logger.warning(
            "{}跳过，服务器客户端未连接: server_id={}, user_id={}, media_id={}",
            log_context,
            server_id,
            user_id,
            media_id,
        )
        return ExternalDisableSyncResult(ok=False, missing_client=True)

    try:
        await client.ban_or_unban(media_id, is_ban=target_disabled)
        return ExternalDisableSyncResult(ok=True)
    except HTTPStatusError as exc:
        if exc.response.status_code == 404 and treat_404_as_synced:
            logger.info(
                "{}时外部账号已不存在(404)，按已同步继续处理: server_id={}, user_id={}, media_id={}",
                log_context,
                server_id,
                user_id,
                media_id,
            )
            return ExternalDisableSyncResult(ok=True, not_found=True)
        logger.error(
            "{}失败: server_id={}, user_id={}, media_id={}, status={}",
            log_context,
            server_id,
            user_id,
            media_id,
            exc.response.status_code,
        )
        return ExternalDisableSyncResult(
            ok=False,
            error=exc,
            not_found=exc.response.status_code == 404,
        )
    except (HTTPError, ValueError, RuntimeError) as exc:
        logger.error(
            "{}异常: server_id={}, user_id={}, media_id={}, err={}",
            log_context,
            server_id,
            user_id,
            media_id,
            exc,
        )
        return ExternalDisableSyncResult(ok=False, error=exc)
