import random
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from loguru import logger
from pydantic import TypeAdapter

from clients.base_client import AuthenticatedClient
from models.emby import (BaseItemDto, BaseItemDtoQueryResult,
                         DevicesDeviceInfo, LibraryMediaFolder,
                         QueryResult_VirtualFolderInfo, SessionInfoDto,
                         UserDto, UserPolicy, VirtualFolderInfo)
from models.protocols import BaseItem
from services.media_service import MediaService


class EmbyClient(AuthenticatedClient, MediaService[UserDto, BaseItemDto, VirtualFolderInfo, DevicesDeviceInfo]):
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

        if response is not None:
            logger.info("创建用户 {} 成功", name)
            pw = await self.post_password(response.Id)
            if not pw:
                logger.error("用户 {} 密码设置失败", name)
                return response, pw
            return response, pw

        logger.error("创建用户 {} 失败: {}", name, response)
        return None, None

    async def delete_user(self, user_id: str) -> None:
        """删除用户。
        Args:
            user_id (str): Emby 用户的唯一标识符。
        """
        url = f"/Users/{user_id}"
        await self.delete(url)


    async def update_policy(self, user_id: str, policy: dict[str, Any], is_none: bool = False) -> None:
        """更新用户策略。
        Args:
            user_id (str): Emby 用户的唯一标识符。
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

    async def get_item_info(self, item_id: str) -> None | BaseItemDto:
        """获取指定媒体项的信息。

        Args:
            item_id (str): 媒体项的唯一标识符。

        Returns:
            BaseItemDto: 媒体项对象，如果未找到则返回 None。
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
        if response is None or response.TotalRecordCount == 0 or not response.Items:
            logger.warning("获取 Emby 项目 {} 信息失败: {}", item_id, response)
            return None
        return response.Items[0]

    async def post_item_info(self, item_id: str, item_info: BaseItem) -> None:
        """更新指定媒体项的信息。

        Args:
            item_id (str): 媒体项的唯一标识符。
            item_info (BaseItemDto): 包含更新信息的媒体项对象。

        Returns:
            bool: 如果更新成功返回 True，否则返回 False。
        """
        url = f"/Items/{item_id}"

        await self.post(url, json=item_info.model_dump(exclude_unset=True))


    async def get_user_info(self, user_id: str) -> UserDto | None:
        """获取指定用户的信息。

        Args:
            user_id (str): Emby 用户的唯一标识符。

        Returns:
            UserDto: Emby 用户对象，如果未找到则返回 None。
        """
        url = f"/Users/{user_id}"
        return await self.get(url, response_model=UserDto)

    async def post_password(self, user_id: str, reset_password: bool = False) -> str:
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
        await self.post(url, json=payload)
        return passwd

    async def ban_or_unban(self, user_id: str, is_ban: bool = True) -> None:
        """封禁或解封用户。

        Args:
            user_id (str): Emby 用户的唯一标识符。
            is_ban (bool): 如果为 True 则封禁用户，否则解封用户。
        """
        user: UserDto | None = await self.get_user_info(user_id)
        if user is not None:
            policy = user.Policy.model_copy(update={'IsDisabled': is_ban}).model_dump()
            await self.update_policy(user_id, policy)
        logger.error("获取用户 {} 信息失败，无法进行封禁或解封操作", user_id)

    async def get_session_list(self) -> int:
        """获取用户在线数量"""
        url = "/Sessions"
        response = await self.get(url,
            parser=lambda data: TypeAdapter(
                list[SessionInfoDto]).validate_python(data)
        )
        if response is None:
            return 0
        return len([session for session in response if session.NowPlayingItem])

    async def get_libraries(self) -> list[VirtualFolderInfo] | None:
        """获取 Emby 的媒体库列表。
        Returns:
            list[VirtualFolderInfo] | None: 返回媒体库信息的列表，如果查询失败则返回 None。
        """
        url = "/Library/VirtualFolders/Query"
        response = await self.get(url, response_model=QueryResult_VirtualFolderInfo)
        if response is None:
            return None
        return response.Items

    async def get_selectable_media_folders(self) -> list[LibraryMediaFolder] | None:
        """获取 Emby 媒体文件夹"""
        url = "/Library/SelectableMediaFolders"
        return await self.get(url,
            parser=lambda data: TypeAdapter(list[LibraryMediaFolder]).validate_python(data))

    async def get_user_id_by_device_id(self, device_id: str) -> DevicesDeviceInfo | None:
        """通过设备 ID 获取用户 ID
        Args:
            device_id (str): Emby 用户设备 ID
        Returns:
            DevicesDeviceInfo | None
        """
        url = "/Devices/Info"
        params = {'Id': device_id}
        return await self.get(url, params=params, response_model=DevicesDeviceInfo)

    async def get_all_items(self) -> AsyncGenerator[BaseItemDto, None]:
        """获取 Emby 媒体库中的所有媒体项。
        Yields:
            BaseItemDto: 媒体项对象。
        """
        url = "/Items"
        start_index = 0
        page_size = 200
        while True:
            params = {
                'Recursive': 'true',
                'StartIndex': start_index,
                'Limit': page_size
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
