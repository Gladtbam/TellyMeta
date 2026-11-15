from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any

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
