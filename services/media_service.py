from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Generic

from models.protocols import BaseItem, UserT, BaseItemT_co


class MediaService(ABC, Generic[UserT, BaseItemT_co]):
    """定义媒体服务的抽象基类"""

    @abstractmethod
    async def create(self, name:str) -> tuple[UserT | None, str | None]:
        """创建用户"""
        raise NotImplementedError

    @abstractmethod
    async def delete_by_id(self, user_id: str | list[str]) -> bool | None:
        """删除用户"""
        raise NotImplementedError

    @abstractmethod
    async def update_policy(self, user_id: str, policy: dict[str, Any], is_none: bool = False) -> bool:
        """更新用户策略"""
        raise NotImplementedError

    @abstractmethod
    async def get_item_info(self, item_id: str) -> BaseItemT_co | None:
        """获取媒体项信息"""
        raise NotImplementedError

    @abstractmethod
    async def post_item_info(self, item_id: str, item_info: BaseItem) -> bool:
        """更新媒体项信息"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_info(self, user_id: str) -> UserT | None:
        """获取用户信息"""
        raise NotImplementedError

    @abstractmethod
    async def post_password(self, user_id: str, reset_password: bool = False) -> str | None:
        """更新用户密码"""
        raise NotImplementedError

    @abstractmethod
    async def ban_or_unban(self, user_id: str, is_ban: bool = True) -> bool:
        """封禁或解封用户"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_playlist(self, user_id: str, expires_at: str) -> float:
        """获取用户的播放记录"""
        raise NotImplementedError

    @abstractmethod
    async def get_session_list(self) -> int:
        """获取用户在线数量"""
        raise NotImplementedError

    @abstractmethod
    async def get_library_names(self) -> list[str] | None:
        """获取媒体库名称列表"""
        raise NotImplementedError

    @abstractmethod
    def get_all_items(self) -> AsyncIterator[BaseItemT_co]:
        """获取所有媒体项的异步生成器"""
        raise NotImplementedError
