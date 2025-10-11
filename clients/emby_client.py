import logging
import random

import httpx

from clients.base_client import BaseClient
from models.emby import (BaseItemDto, UserPolicy,
                         QueryResult_BaseItemDto, UserDto)
from services.media_service import MediaService

logger = logging.getLogger(__name__)

class EmbyClient(BaseClient, MediaService):
    """Emby 客户端
    用于与 Emby 媒体服务器交互。
    继承自 MediaService 抽象基类，提供获取和更新媒体项信息的方法。
    """

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        """初始化 EmbyClient 实例。

        Args:
            client (httpx.AsyncClient): 异步 HTTP 客户端实例。
            api_key (str): Emby API 密钥，用于认证请求。
        """
        super().__init__(client)
        self._api_key = api_key

    async def create(self, name: str) -> UserDto | None:
        """创建用户。
        Args:
            name (str): 用户名。
        Returns:
            UserDto: 创建的 Emby 用户对象。
        """
        url = "/Users/New"
        params = {'api_key': self._api_key}
        payload = {'Name': name}

        response = await self.post(url, params=params, json=payload)
        response.raise_for_status()
        logger.info("创建用户 %s 成功", name)
        return UserDto.model_validate(response.json())
    
    async def delete_by_id(self, user_id: str | list[str]) -> bool | None:
        """删除用户。

        Args:
            emby_id (str): Emby 用户的唯一标识符。
        """
        params = {'api_key': self._api_key}

        if isinstance(user_id, str):
            user_id = [user_id]
        for uid in user_id:
            url = f"/Users/{uid}"
            response = await self.delete(url, params=params)
            response.raise_for_status()
            logger.info("删除用户 %s 成功", uid)
        return True

    async def update_policy(self, user_id: str, policy: UserPolicy, is_none: bool = False) -> bool | None:
        url = f"/Users/{user_id}/Policy"
        params = {'api_key': self._api_key}
        if is_none:
            payload = policy.model_dump(exclude_none=True)
        else:
            payload = policy.model_dump(exclude_unset=True)

        response = await self.post(url, params=params, json=payload)
        response.raise_for_status()
        logger.info("User policy for %s updated successfully", user_id)
        return True

    async def get_item_info(self, item_id: str) -> QueryResult_BaseItemDto | None:
        """获取指定媒体项的信息。

        Args:
            item_id (str): 媒体项的唯一标识符。

        Returns:
            dict: 媒体项信息字典，如果未找到则返回 None。
        """
        url = "/Items"
        fields = ["ProductionYear", "Budget", "Chapters", "DateCreated", "PremiereDate",
              "Genres", "HomePageUrl", "IndexOptions", "MediaStreams", "Overview",
              "ParentId", "Path", "People", "ProviderIds", "PrimaryImageAspectRatio",
              "Revenue", "SortName", "Studios", "Taglines", "CommunityRating",
              "CriticRating"]
        params = {
            'Recursive': 'true',
            'Fields': ', '.join(fields),
            'EnableImages': 'true',
            'EnableUserData': 'true',
            'Ids': item_id,
            'api_key': self._api_key,
        }

        response = await self.get(url, params=params)
        response.raise_for_status()
        return QueryResult_BaseItemDto.model_validate(response.json())

    async def post_item_info(self, item_id: str, item_info: BaseItemDto) -> bool | None:
        """更新指定媒体项的信息。

        Args:
            item_id (str): 媒体项的唯一标识符。
            item_info (BaseItemDto): 包含更新信息的媒体项对象。

        Returns:
            bool: 如果更新成功返回 True，否则返回 False。
        """
        url = f"/Items/{item_id}"
        params = {'api_key': self._api_key}

        response = await self.post(url, params=params,
                                   json=item_info.model_dump(exclude_unset=True))
        response.raise_for_status()
        logger.info("Successfully updated item %s", item_id)
        return True

    async def get_user_info(self, user_id: str) -> UserDto | None:
        """获取指定用户的信息。

        Args:
            emby_id (str): Emby 用户的唯一标识符。

        Returns:
            UserDto: Emby 用户对象，如果未找到则返回 None。
        """
        url = f"/Users/{user_id}"
        params = {'api_key': self._api_key}
        response = await self.get(url, params=params)
        response.raise_for_status()
        return UserDto.model_validate(response.json())

    async def post_password(self, user_id: str, reset_password: bool = False) -> str | None:
        """更新用户密码。

        Args:
            emby_id (str): Emby 用户的唯一标识符。
            reset_password (bool): 是否重置密码。如果为 True，则 Emby 会生成一个新密码。
        """
        passwd = ''.join(random.sample('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=12))
        url = f"/Users/{user_id}/Password"
        params = {'api_key': self._api_key}
        payload = {
            'Id': user_id,
            'NewPw': passwd if not reset_password else None,
            'ResetPassword': reset_password
        }
        response = await self.post(url, params=params, json=payload)
        response.raise_for_status()
        return passwd if not reset_password else None

    async def ban_or_unban(self, user_id: str | list[str], is_ban: bool = True) -> bool:
        """封禁或解封用户。

        Args:
            emby_id (str): Emby 用户的唯一标识符。
            is_ban (bool): 如果为 True 则封禁用户，否则解封用户。
        """
        if isinstance(user_id, str):
            user_id = [user_id]
        for uid in user_id:
            user: UserDto | None = await self.get_user_info(uid)
            if not user:
                logger.error("User %s not found for ban/unban operation", user_id)
                return False
            policy = user.Policy.model_copy(update={'IsDisabled': is_ban})
            await self.update_policy(uid, policy)
        return True

    async def get_user_playlist(self, user_id: str, expires_at: str) -> float:
        """获取用户的播放记录。
        Args:
            emby_id (str): Emby 用户的唯一标识符。
            expires_at (str): 过期时间，格式为 'YYYY-MM-DD'。
        """
        total_duration = 0
        url = "/emby/user_usage_stats/UserPlaylist"
        params = {
            'user_id': user_id,
            'aggregate_data': 'false',
            'days': '30',
            'end_date': expires_at,
            'api_key': self._api_key
        }
        response = await self.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        for item in data:
            total_duration += int(item.get('duration', 0))
        total_ratio = total_duration / 86400
        return total_ratio

    async def get_session_list(self) -> int:
        """获取用户在线数量"""
        url = "/emby/user_usage_stats/session_list"
        params = {'api_key': self._api_key}
        response = await self.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        return len([session for session in data if session.get('NowPlayingItem')])
