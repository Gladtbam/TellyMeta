import random
import warnings
from typing import Any

import httpx
from loguru import logger

from clients.base_client import AuthenticatedClient
from models.emby import BaseItemDtoQueryResult, UserDto, UserPolicy
from models.protocols import BaseItem
from services.media_service import MediaService


class EmbyClient(AuthenticatedClient, MediaService[UserDto, BaseItemDtoQueryResult]):
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

    async def _login(self) -> None:
        # Emby 使用 API Key 进行认证，无需登录
        self._is_logged_in = True

    async def _apply_auth(self):
        return {
            "X-Emby-Token": self._api_key,
            "accept": "application/json",
            "Content-Type": "application/json"
        }

    async def create(self, name: str) -> tuple[UserDto | None, str | None]:
        """创建用户。
        Args:
            name (str): 用户名。
        Returns:
            UserDto: 创建的 Emby 用户对象。
        """
        url = "/Users/New"
        payload = {'Name': name}

        response = await self.post(url, json=payload, response_model=UserDto)

        if isinstance(response, UserDto):
            logger.info("创建用户 {} 成功", name)
            pw = await self.post_password(response.Id)
            if not pw:
                logger.error("用户 {} 密码设置失败", name)
                return response, pw
            return response, pw
        else:
            logger.error("创建用户 {} 失败: {}", name, response)
            return None, None

    async def delete_by_id(self, user_id: str | list[str]) -> bool | None:
        """删除用户。

        Args:
            user_id (str): Emby 用户的唯一标识符。
        """

        if isinstance(user_id, str):
            user_id = [user_id]
        for uid in user_id:
            url = f"/Users/{uid}"
            response = await self.delete(url)
            if response:
                logger.info("删除用户 {} 成功", uid)
            else:
                logger.error("删除用户 {} 失败", uid)
        return True

    async def update_policy(self, user_id: str, policy: dict[str, Any], is_none: bool = False) -> bool:
        """更新用户策略。
        Args:
            user_id (str): Jellyfin 用户的唯一标识符。
            policy (dict): 用户策略字典。
            is_none (bool): 是否包含 None 值，默认为 False。
        Returns:
            bool: 更新是否成功。
        """
        url = f"/Users/{user_id}/Policy"
        if is_none:
            payload = UserPolicy(**policy).model_dump(exclude_none=True)
        else:
            payload = UserPolicy(**policy).model_dump(exclude_unset=True)

        response = await self.post(url, json=payload, response_model=UserPolicy)
        if isinstance(response, UserPolicy):
            logger.info("{} 的用户政策已成功更新", user_id)
            return True
        else:
            logger.error("更新 {} 的用户策略失败：{}", user_id, response)
            return False

    async def get_item_info(self, item_id: str) -> BaseItemDtoQueryResult | None:
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
            'Ids': item_id
        }

        response = await self.get(url, params=params, response_model=BaseItemDtoQueryResult)
        return response if isinstance(response, BaseItemDtoQueryResult) else None

    async def post_item_info(self, item_id: str, item_info: BaseItem) -> bool:
        """更新指定媒体项的信息。

        Args:
            item_id (str): 媒体项的唯一标识符。
            item_info (BaseItemDto): 包含更新信息的媒体项对象。

        Returns:
            bool: 如果更新成功返回 True，否则返回 False。
        """
        url = f"/Items/{item_id}"

        response = await self.post(url, json=item_info.model_dump(exclude_unset=True))
        if not response:
            logger.error("更新项目 {} 失败", item_id)
            return False
        logger.info("已成功更新项目 {}", item_id)
        return True

    async def get_user_info(self, user_id: str) -> UserDto | None:
        """获取指定用户的信息。

        Args:
            user_id (str): Emby 用户的唯一标识符。

        Returns:
            UserDto: Emby 用户对象，如果未找到则返回 None。
        """
        url = f"/Users/{user_id}"
        response = await self.get(url, response_model=UserDto)
        if not isinstance(response, UserDto):
            logger.error("获取用户 {} 信息失败: {}", user_id, response)
            return None
        return response

    async def post_password(self, user_id: str, reset_password: bool = False) -> str | None:
        """更新用户密码。

        Args:
            user_id (str): Emby 用户的唯一标识符。
            reset_password (bool): 是否重置密码。如果为 True，则 Emby 会生成一个新密码。
        """
        passwd = ''.join(random.sample('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=12))
        url = f"/Users/{user_id}/Password"
        payload = {
            'Id': user_id,
            'NewPw': passwd if not reset_password else None,
            'ResetPassword': reset_password
        }
        response = await self.post(url, json=payload)
        return passwd if not response else None

    async def ban_or_unban(self, user_id: str | list[str], is_ban: bool = True) -> bool:
        """封禁或解封用户。

        Args:
            user_id (str): Emby 用户的唯一标识符。
            is_ban (bool): 如果为 True 则封禁用户，否则解封用户。
        """
        if isinstance(user_id, str):
            user_id = [user_id]
        for uid in user_id:
            user: UserDto | None = await self.get_user_info(uid)
            if not user:
                logger.error("获取用户 {} 信息失败，无法进行封禁或解封操作", user_id)
                continue
            policy = user.Policy.model_copy(update={'IsDisabled': is_ban}).model_dump()
            success = await self.update_policy(uid, policy)
            if not success:
                logger.error("更新用户 {} 策略失败，无法进行封禁或解封操作", uid)
        return True

    async def get_user_playlist(self, user_id: str, expires_at: str) -> float:
        """获取用户的播放记录。
        Args:
            emby_id (str): Emby 用户的唯一标识符。
            expires_at (str): 过期时间，格式为 'YYYY-MM-DD'。
        """
        warnings.warn("EmbyClient.get_user_playlist 方法未实现完整功能，仅返回 0.0", DeprecationWarning)
        total_duration = 0
        url = "/user_usage_stats/UserPlaylist"
        params = {
            'user_id': user_id,
            'aggregate_data': 'false',
            'days': '30',
            'end_date': expires_at
        }
        response = await self.get(url, params=params)
        if not response:
            return 0.0
        data = response.json()
        for item in data:
            total_duration += int(item.get('duration', 0))
        total_ratio = total_duration / 86400
        return total_ratio

    async def get_session_list(self) -> int:
        """获取用户在线数量"""
        url = "/Sessions"
        response = await self.get(url)
        if not response:
            return 0
        data = response.json()
        return len([session for session in data if session.get('NowPlayingItem')])
