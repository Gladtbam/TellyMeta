from abc import ABC, abstractmethod
from typing import Any

from models.emby import EmbySetUserPolicy, EmbyUser


class MediaService(ABC):
    """定义媒体服务的抽象基类"""

    @abstractmethod
    async def create(self, name:str) -> EmbyUser | None:
        """创建用户"""
        raise NotImplementedError

    @abstractmethod
    async def delete_by_id(self, user_id: str | list[str]) -> bool:
        """删除用户"""
        raise NotImplementedError

    @abstractmethod
    async def update_policy(self, user_id: str, policy: Any) -> bool:
        """更新用户策略"""
        raise NotImplementedError

    @abstractmethod
    async def get_item_info(self, item_id: str):
        """获取媒体项信息"""
        raise NotImplementedError

    @abstractmethod
    async def post_item_info(self, item_id: str, item_info: dict) -> bool:
        """更新媒体项信息"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_info(self, user_id: str) -> EmbyUser:
        """获取用户信息"""
        raise NotImplementedError

    @abstractmethod
    async def post_password(self, user_id: str, reset_password: bool = False) -> str | None:
        """更新用户密码"""
        raise NotImplementedError

    @abstractmethod
    async def ban_or_unban(self, user_id: str, is_ban: bool = True):
        """封禁或解封用户"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_playlist(self, user_id: str, expires_at: str) -> float:
        """获取用户的播放记录"""
        raise NotImplementedError

    @abstractmethod
    async def get_session_list(self):
        """获取用户在线数量"""
        raise NotImplementedError
