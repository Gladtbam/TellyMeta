import random
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from loguru import logger
from pydantic import TypeAdapter

from clients.base_client import AuthenticatedClient
from models.jellyfin import (BaseItemDto, BaseItemDtoQueryResult,
                             SessionInfoDto, UserDto, UserPolicy,
                             VirtualFolderInfo)
from models.protocols import BaseItem
from services.media_service import MediaService


class JellyfinClient(AuthenticatedClient, MediaService[UserDto, BaseItemDto, VirtualFolderInfo]):
    """Jellyfin 客户端
    用于与 Jellyfin 媒体服务器交互。
    继承自 MediaService 抽象基类，提供获取和更新媒体项信息的方法。
    """

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        """初始化 JellyfinClient 实例。

        Args:
            client (httpx.AsyncClient): 异步 HTTP 客户端实例。
            api_key (str): Jellyfin API 密钥，用于认证请求。
        """
        super().__init__(client)
        self._api_key = api_key

    async def _login(self) -> None:
        # Jellyfin 使用 API Key 进行认证，无需登录
        self._is_logged_in = True

    async def _apply_auth(self):
        return {
            "Authorization": f"MediaBrowser Token={self._api_key}",
            "accept": "application/json",
            "Content-Type": "application/json"
        }

    async def create(self, name: str) -> tuple[UserDto | None, str | None]:
        """创建用户。
        Args:
            name (str): 用户名。
        Returns:
            User: 创建的 Jellyfin 用户对象。
        """
        url = "/Users/New"
        pw = ''.join(random.sample('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=12))
        payload = {'Name': name, 'Password': pw}

        response = await self.post(url, json=payload, response_model=UserDto)
        if response is None:
            logger.error("创建用户 {} 失败: {}", name, response)
            return None, None

        logger.info("创建用户 {} 成功", name)
        return response, pw

    async def delete_user(self, user_id: str) ->None:
        """删除用户。
        Args:
            user_id (str): Jellyfin 用户的唯一标识符。
        """
        url = f"/Users/{user_id}"
        await self.delete(url)

    async def update_policy(self, user_id: str, policy: dict[str, Any], is_none: bool = False) -> None:
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

        await self.post(url, json=payload)

    async def get_item_info(self, item_id: str) -> BaseItemDto | None:
        """获取媒体项信息。
        Args:
            item_id (str): 媒体项的唯一标识符。
        Returns:
            BaseItemDto: 媒体项对象，如果未找到则返回 None。
        """
        url = f"/Items/{item_id}"
        fields = ["AirTime", "CanDelete", "CanDownload", "ChannelInfo", "Chapters", "Trickplay",
            "ChildCount", "CumulativeRunTimeTicks", "CustomRating", "DateCreated", "DateLastMediaAdded",
            "DisplayPreferencesId", "Etag", "ExternalUrls", "Genres", "ItemCounts", "MediaSourceCount",
            "MediaSources", "OriginalTitle", "Overview", "ParentId", "Path", "People", "PlayAccess",
            "ProductionLocations", "ProviderIds", "PrimaryImageAspectRatio", "RecursiveItemCount", "Settings",
            "SeriesStudio", "SortName", "SpecialEpisodeNumbers", "Studios", "Taglines", "Tags", "RemoteTrailers",
            "MediaStreams", "SeasonUserData", "DateLastRefreshed", "DateLastSaved", "RefreshState", "ChannelImage",
            "EnableMediaSourceDisplay", "Width", "Height", "ExtraIds", "LocalTrailerCount", "IsHD",
            "SpecialFeatureCount"]
        params = {
            'recursive': 'true',
            'filters': ', '.join(fields),
            'enableImages': 'true',
            'enableUserData': 'true',
            'ids': item_id
        }
        response = await self.get(url, params=params, response_model=BaseItemDtoQueryResult)
        if response is None or response.TotalRecordCount == 0 or not response.Items:
            logger.warning("获取 Jellyfin 项目 {} 信息失败: {}", item_id, response)
            return None
        return response.Items[0]

    async def post_item_info(self, item_id: str, item_info: BaseItem) -> None:
        """更新媒体项信息。
        Args:
            item_id (str): 媒体项的唯一标识符。
            item_info (BaseItemDto): 包含更新信息的媒体项对象。
        Returns:
            bool: 更新是否成功。
        """
        url = f"/Items/{item_id}"
        await self.post(url, json=item_info.model_dump(exclude_unset=True))

    async def get_user_info(self, user_id: str) -> UserDto | None:
        """获取用户信息。
        Args:
            user_id (str): Jellyfin 用户的唯一标识符。
        Returns:
            UserDto: 包含用户信息的响应数据。
        """
        url = f"/Users/{user_id}"
        return await self.get(url, response_model=UserDto)

    async def post_password(self, user_id: str, reset_password: bool = False) -> str:
        """更新用户密码。
        Args:
            user_id (str): Jellyfin 用户的唯一标识符。
            reset_password (bool): 是否重置密码，默认为 False。
        Returns:
            str: 新密码，如果更新失败则返回 None。
        """
        passwd = ''.join(random.sample('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=12))
        url = "/Users/Password"
        params = {'userId': user_id}
        payload = {
            'NewPw': passwd,
            'ResetPassword': reset_password
        }
        await self.post(url, params=params, json=payload)
        return passwd

    async def ban_or_unban(self, user_id: str, is_ban: bool = True) -> None:
        """封禁或解封用户。
        Args:
            user_id (str): Jellyfin 用户的唯一标识符。
            is_ban (bool): 如果为 True 则封禁用户，否则解封用户。
        """
        user = await self.get_user_info(user_id)
        if user is not None:
            policy = user.Policy.model_copy(update={'IsDisabled': is_ban}).model_dump()
            await self.update_policy(user_id, policy)
        else:
            logger.error("获取用户 {} 信息失败，无法进行封禁或解封操作", user_id)

    async def get_session_list(self) -> int:
        """获取用户在线数量。
        Returns:
            int: 在线用户数量。
        """
        url = "/Sessions"
        response = await self.get(url,
            parser=lambda data: TypeAdapter(
                list[SessionInfoDto]).validate_python(data))
        if response is None:
            return 0
        return len([session for session in response if session.NowPlayingItem])

    async def get_libraries(self) -> list[VirtualFolderInfo] | None:
        """获取 Jellyfin 的媒体库列表。
        Returns:
            list[VirtualFolderInfo] | None: 返回媒体库信息的列表，如果查询失败则返回 None。
        """
        url = "/Library/VirtualFolders"
        response = await self.get(url,
            parser=lambda data: TypeAdapter(list[VirtualFolderInfo]).validate_python(data))
        if response is None:
            return None
        return response

    async def get_all_items(self) -> AsyncGenerator[BaseItemDto, None]:
        """获取 Jellyfin 媒体库中的所有媒体项。
        Yields:
            BaseItemDto: 媒体项对象。
        """
        url = "/Items"
        start_index = 0
        page_size = 200
        while True:
            params = {
                'recursive': 'true',
                'startIndex': start_index,
                'limit': page_size
            }
            response = await self.get(url, params=params, response_model=BaseItemDtoQueryResult)
            if response is None:
                break
            items = response.Items
            if not items:
                break
            for item in items:
                yield item
            start_index += len(items)
            if start_index >= response.TotalRecordCount:
                break
