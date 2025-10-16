import json

import httpx
from loguru import logger

from clients.base_client import AuthenticatedClient
from models.qbittorrent import (QbittorrentPreference,
                                QbittorrentTorrentProperties)


class QbittorrentClient(AuthenticatedClient):
    def __init__(self, client: httpx.AsyncClient, username: str, password: str):
        super().__init__(client)
        self.username = username
        self.password = password

    async def _login(self):
        data = {
            'username': self.username,
            'password': self.password
        }
        if self._client is None:
            logger.warning("HTTP 客户端未初始化。首先调用 login()。")
        response = await self._client.post("/api/v2/auth/login", data=data)
        response.raise_for_status()
        return response.text  # Returns the session cookie on successful login

    async def _apply_auth(self):
        return {}

    async def app_version(self) -> str | None:
        """获取 qBittorrent 的版本信息"""
        response = await self.get("/api/v2/app/version")
        return response.text if response else None

    async def app_webapi_version(self) -> str | None:
        """获取 qBittorrent Web API 的版本信息"""
        response = await self.get("/api/v2/app/webapiVersion")
        return response.text if response else None

    async def app_build_info(self):
        """获取 qBittorrent 的构建信息"""
        response = await self.get("/api/v2/app/buildInfo")
        return response.json() if response else None

    async def app_shutdown(self) -> bool:
        """关闭 qBittorrent"""
        response = await self.post("/api/v2/app/shutdown")
        return response.status_code == 200 if response else False

    async def app_preferences(self) -> QbittorrentPreference | None:
        """获取 qBittorrent 的首选项"""
        response = await self.get("/api/v2/app/preferences", response_model=QbittorrentPreference)
        return response if isinstance(response, QbittorrentPreference) else None

    async def app_set_preferences(self, preferences: QbittorrentPreference) -> bool:
        """设置 qBittorrent 的首选项"""
        payload = preferences.model_dump(exclude_unset=True)
        if not payload:
            raise ValueError("无设置首选项设置")
        data = {'json': json.dumps(payload)}
        response = await self.post("/api/v2/app/setPreferences", data=data)
        return response.status_code == 200 if response else False

    async def torrents_properties(self, torrent_hash: str) -> QbittorrentTorrentProperties | None:
        """获取指定 torrent 的属性"""
        response = await self.get("/api/v2/torrents/properties",
                                  params={'hash': torrent_hash},
                                  response_model=QbittorrentTorrentProperties)
        return response if isinstance(response, QbittorrentTorrentProperties) else None

    async def torrents_stop(self, torrent_hash: list[str] | str) -> bool:
        """停止指定的 torrent"""
        if not torrent_hash:
            raise ValueError("请提供 torrent 哈希值")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("torrent 哈希值必须是字符串")
        params = {'hashes': '|'.join(hashes)}
        response = await self.post("/api/v2/torrents/stop", params=params)
        return response.status_code == 200 if response else False

    async def torrents_start(self, torrent_hash: list[str] | str) -> bool:
        """恢复指定的 torrent"""
        if not torrent_hash:
            raise ValueError("请提供 torrent 哈希值")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("torrent 哈希值必须是字符串")
        params = {'hashes': '|'.join(hashes)}
        response = await self.post("/api/v2/torrents/start", params=params)
        return response.status_code == 200 if response else False

    async def torrents_delete(self, torrent_hash: list[str] | str, delete_files: bool = False) -> bool:
        """删除指定的 torrent"""
        if not torrent_hash:
            raise ValueError("请提供 torrent 哈希值")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("torrent 哈希值必须是字符串")
        params = {'hashes': '|'.join(hashes), 'deleteFiles': 'true' if delete_files else 'false'}
        response = await self.post("/api/v2/torrents/delete", params=params)
        return response.status_code == 200 if response else False

    async def torrents_download_limit(self, torrent_hash: list[str] | str) -> dict[str, int] | None:
        """获取指定 torrent 的下载限速"""
        if not torrent_hash:
            raise ValueError("请提供 torrent 哈希值")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("torrent 哈希值必须是字符串")
        params = {'hashes': '|'.join(hashes)}
        response = await self.get("/api/v2/torrents/downloadLimit", params=params)
        return response.json() if response else None

    async def torrents_set_download_limit(self, torrent_hash: list[str] | str, limit: int) -> bool:
        """设置指定 torrent 的下载限速"""
        if not torrent_hash:
            raise ValueError("请提供 torrent 哈希值")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("torrent 哈希值必须是字符串")
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("Download limit must be a non-negative integer")
        params = {'hashes': '|'.join(hashes), 'limit': limit}
        response = await self.post("/api/v2/torrents/setDownloadLimit", params=params)
        return response.status_code == 200 if response else False

    async def torrents_set_share_limits(
        self,
        torrent_hash: list[str] | str,
        ratio_limit: float = -2.0,
        seeding_time_limit: int = -2,
        inactive_seeding_time_limit: int = -2) -> bool:
        """设置指定 torrent 的分享限制"""
        if not torrent_hash:
            raise ValueError("请提供 torrent 哈希值")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("torrent 哈希值必须是字符串")
        data = {
            'hashes': '|'.join(hashes),
            'ratioLimit': ratio_limit if ratio_limit is not None else '',
            'seedingTimeLimit': seeding_time_limit if seeding_time_limit is not None else '',
            'inactiveSeedingTimeLimit': inactive_seeding_time_limit if inactive_seeding_time_limit is not None else ''
        }
        response = await self.post("/api/v2/torrents/setShareLimits", data=data)
        return response.status_code == 200 if response else False
