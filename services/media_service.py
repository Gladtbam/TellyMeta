from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any, Generic

from models.emby import LibraryMediaFolder
from models.protocols import (BaseItem, BaseItemT_co, DeviceInfoT, LibraryT, PublicSystemInfoT,
                              UserT)


class MediaService(ABC, Generic[UserT, BaseItemT_co, LibraryT, DeviceInfoT, PublicSystemInfoT]):
    """定义媒体服务的抽象基类"""

    @abstractmethod
    async def create(self, name:str) -> tuple[UserT | None, str | None]:
        """创建用户"""
        raise NotImplementedError

    @abstractmethod
    async def delete_user(self, user_id: str) -> None:
        """删除用户"""
        raise NotImplementedError

    @abstractmethod
    async def update_policy(self, user_id: str, policy: dict[str, Any], is_none: bool = False) -> None:
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
    async def get_libraries(self) -> Sequence[LibraryT] | None:
        """获取媒体库列表"""
        raise NotImplementedError

    @abstractmethod
    async def get_selectable_media_folders(self) -> list[LibraryMediaFolder] | None:
        """获取 Emby 媒体文件夹
        仅 Emby
        """
        raise NotImplementedError

    @abstractmethod
    async def get_user_id_by_device_id(self, device_id: str) -> DeviceInfoT | None:
        """"通过设备 ID 获取用户 ID"""
        raise NotImplementedError

    @abstractmethod
    async def get_system_info_public(self) -> PublicSystemInfoT | None:
        """获取公开信息"""
        raise NotImplementedError

    @abstractmethod
    def get_all_items(self) -> AsyncIterator[BaseItemT_co]:
        """获取所有媒体项的异步生成器"""
        raise NotImplementedError
