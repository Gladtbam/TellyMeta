import json
import logging

import httpx

from clients.base_client import AuthenticatedClient
from models.qbittorrent import (QbittorrentPreference,
                                QbittorrentTorrentProperties)

logger = logging.getLogger(__name__)

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
            raise RuntimeError("HTTP client is not initialized. Call login() first.")
        response = await self._client.post("/api/v2/auth/login", data=data)
        response.raise_for_status()
        return response.text  # Returns the session cookie on successful login

    async def _apply_auth(self):
        return {}

    async def app_version(self):
        """获取 qBittorrent 的版本信息"""
        response = await self.get("/api/v2/app/version")
        return response.text

    async def app_webapi_version(self):
        """获取 qBittorrent Web API 的版本信息"""
        response = await self.get("/api/v2/app/webapiVersion")
        return response.text

    async def app_build_info(self):
        """获取 qBittorrent 的构建信息"""
        response = await self.get("/api/v2/app/buildInfo")
        return response.json()

    async def app_shutdown(self):
        """关闭 qBittorrent"""
        response = await self.post("/api/v2/app/shutdown")
        return response.status_code == 200

    async def app_preferences(self):
        """获取 qBittorrent 的首选项"""
        response = await self.get("/api/v2/app/preferences")
        return QbittorrentPreference.model_validate(response.json())

    async def app_set_preferences(self, preferences: QbittorrentPreference):
        """设置 qBittorrent 的首选项"""
        payload = preferences.model_dump(exclude_unset=True)
        if not payload:
            raise ValueError("No preferences to set")
        data = {'json': json.dumps(payload)}
        response = await self.post("/api/v2/app/setPreferences", data=data)
        return response.status_code == 200

    async def torrents_properties(self, torrent_hash: str):
        """获取指定 torrent 的属性"""
        response = await self.get("/api/v2/torrents/properties", params={'hash': torrent_hash})
        return QbittorrentTorrentProperties.model_validate(response.json())

    async def torrents_stop(self, torrent_hash: list[str] | str):
        """停止指定的 torrent"""
        if not torrent_hash:
            raise ValueError("No torrent hashes provided to stop")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("All torrent hashes must be strings")
        params = {'hashes': '|'.join(hashes)}
        response = await self.post("/api/v2/torrents/stop", params=params)
        return response.status_code == 200

    async def torrents_start(self, torrent_hash: list[str] | str):
        """恢复指定的 torrent"""
        if not torrent_hash:
            raise ValueError("No torrent hashes provided to start/resume")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("All torrent hashes must be strings")
        params = {'hashes': '|'.join(hashes)}
        response = await self.post("/api/v2/torrents/start", params=params)
        return response.status_code == 200

    async def torrents_delete(self, torrent_hash: list[str] | str, delete_files: bool = False):
        """删除指定的 torrent"""
        if not torrent_hash:
            raise ValueError("No torrent hashes provided to delete")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("All torrent hashes must be strings")
        params = {'hashes': '|'.join(hashes), 'deleteFiles': 'true' if delete_files else 'false'}
        response = await self.post("/api/v2/torrents/delete", params=params)
        return response.status_code == 200

    async def torrents_download_limit(self, torrent_hash: list[str] | str):
        """获取指定 torrent 的下载限速"""
        if not torrent_hash:
            raise ValueError("No torrent hashes provided to get download limit")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("All torrent hashes must be strings")
        params = {'hashes': '|'.join(hashes)}
        response = await self.get("/api/v2/torrents/downloadLimit", params=params)
        return response.json()

    async def torrents_set_download_limit(self, torrent_hash: list[str] | str, limit: int):
        """设置指定 torrent 的下载限速"""
        if not torrent_hash:
            raise ValueError("No torrent hashes provided to set download limit")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("All torrent hashes must be strings")
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("Download limit must be a non-negative integer")
        params = {'hashes': '|'.join(hashes), 'limit': limit}
        response = await self.post("/api/v2/torrents/setDownloadLimit", params=params)
        return response.status_code == 200

    async def torrents_set_share_limits(
        self,
        torrent_hash: list[str] | str,
        ratio_limit: float = -2.0,
        seeding_time_limit: int = -2,
        inactive_seeding_time_limit: int = -2):
        """设置指定 torrent 的分享限制"""
        if not torrent_hash:
            raise ValueError("No torrent hashes provided to set share limits")

        hashes = [torrent_hash] if isinstance(torrent_hash, str) else torrent_hash
        if not all(isinstance(h, str) for h in hashes):
            raise ValueError("All torrent hashes must be strings")
        data = {
            'hashes': '|'.join(hashes),
            'ratioLimit': ratio_limit if ratio_limit is not None else '',
            'seedingTimeLimit': seeding_time_limit if seeding_time_limit is not None else '',
            'inactiveSeedingTimeLimit': inactive_seeding_time_limit if inactive_seeding_time_limit is not None else ''
        }
        response = await self.post("/api/v2/torrents/setShareLimits", data=data)
        return response.status_code == 200
