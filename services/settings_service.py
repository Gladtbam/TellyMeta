import contextlib
import json

import httpx2
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from clients.ai_client import AIClientWarper
from clients.emby_client import EmbyClient
from clients.jellyfin_client import JellyfinClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from models.emby import LibraryMediaFolder
from models.orm import ServerInstance, ServerType
from models.schemas import (
    ArrServerDto,
    BindingDto,
    LibraryDto,
    LlmConfigUpdateDto,
    NsfwLibraryDto,
    QualityProfileDto,
    RootFolderDto,
)
from repositories.binding_repo import BindingRepository
from repositories.config_repo import ConfigRepository
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.media_service import MediaService
from services.user_service import Result

settings = get_settings()


class SettingsServices:
    def __init__(self, app: FastAPI, session: AsyncSession) -> None:
        self.app = app
        self.session = session
        self.client: TelethonClientWarper = app.state.telethon_client
        self.telegram_repo = TelegramRepository(session)
        self.config_repo = ConfigRepository(session)
        self.binding_repo = BindingRepository(session)
        self.server_repo = ServerRepository(session)
        self.media_clients: dict[int, MediaService] = app.state.media_clients
        self._sonarr_clients = app.state.sonarr_clients
        self._radarr_clients = app.state.radarr_clients

    async def reload_ai_client(self) -> AIClientWarper | None:
        """根据数据库中的 LLM 配置重新加载或关闭全局 AI 客户端。

        Returns:
            启用且配置了 API Key 时返回新建的 ``AIClientWarper``，否则返回 ``None``。
        """
        old_client: AIClientWarper | None = getattr(self.app.state, "ai_client", None)
        if old_client is not None:
            with contextlib.suppress(Exception):
                await old_client.close()

        llm_cfg = await self.config_repo.get_llm_config()
        if llm_cfg["enable_llm_translate"] and llm_cfg["ai_api_key"]:
            new_client = AIClientWarper(
                base_url=llm_cfg["ai_base_url"],
                api_key=llm_cfg["ai_api_key"],
                model=llm_cfg["ai_model"],
                temperature=llm_cfg["ai_temperature"],
                rpm=llm_cfg["ai_rpm"],
                rpd=llm_cfg["ai_rpd"],
                tpm=llm_cfg["ai_tpm"],
                concurrency=llm_cfg["ai_concurrency"],
                proxy=settings.proxy,
            )
            self.app.state.ai_client = new_client
            return new_client

        self.app.state.ai_client = None
        return None

    @property
    def sonarr_clients(self) -> dict[int, SonarrClient]:
        if self._sonarr_clients is None:
            raise RuntimeError("Sonarr 客户端未配置")
        return self._sonarr_clients

    @property
    def radarr_clients(self) -> dict[int, RadarrClient]:
        if self._radarr_clients is None:
            raise RuntimeError("Radarr 客户端未配置")
        return self._radarr_clients

    async def toggle_admin(self, user_id: int) -> Result:
        """切换用户的管理员状态。
        Args:
            user_id (int): 用户的 Telegram ID。
        Returns:
            Result: 包含操作结果的对象。
        """
        try:
            if user_id in self.app.state.admin_ids:
                await self.telegram_repo.toggle_admin(user_id, is_admin=False)
                self.app.state.admin_ids.discard(user_id)
                return Result(
                    success=True, message=f"已撤销用户 {user_id} 的管理员权限。"
                )
            else:
                await self.telegram_repo.toggle_admin(user_id, is_admin=True)
                self.app.state.admin_ids.add(user_id)
                return Result(
                    success=True, message=f"已授予用户 {user_id} 管理员权限。"
                )
        except (ValueError, KeyError) as e:
            return Result(success=False, message=str(e))

    async def toggle_server_status(self, server_id: int) -> Result:
        """切换服务器启用状态并处理客户端连接"""
        server = await self.server_repo.toggle_enabled(server_id)
        if not server:
            return Result(False, "服务器不存在")

        if server.is_enabled:
            try:
                await self._init_and_add_client(server)
                return Result(True, f"✅ 服务器 **{server.name}** 已启用并连接。")
            except Exception as e:
                await self.server_repo.toggle_enabled(server_id)
                return Result(False, f"❌ 启用失败，无法连接到服务器: {str(e)}")
        else:
            await self._remove_and_close_client(server)
            return Result(True, f"🔴 服务器 **{server.name}** 已停用并断开连接。")

    async def _init_and_add_client(self, server: ServerInstance):
        """(内部) 初始化单个客户端并添加到 app.state"""
        client = None
        mappings = {}
        if server.path_mappings:
            with contextlib.suppress(json.JSONDecodeError):
                mappings = json.loads(server.path_mappings)

        if server.server_type == ServerType.EMBY:
            client = EmbyClient(
                client=httpx2.AsyncClient(
                    base_url=f"{server.url}/emby",
                    timeout=httpx2.Timeout(10.0, read=30.0),
                ),
                api_key=server.api_key,
                server_name=server.name,
                notify_topic_id=server.notify_topic_id,
            )
            self.media_clients[server.id] = client
        elif server.server_type == ServerType.JELLYFIN:
            client = JellyfinClient(
                client=httpx2.AsyncClient(
                    base_url=server.url, timeout=httpx2.Timeout(10.0, read=30.0)
                ),
                api_key=server.api_key,
                server_name=server.name,
                notify_topic_id=server.notify_topic_id,
            )
            self.media_clients[server.id] = client
        elif server.server_type == ServerType.SONARR:
            client = SonarrClient(
                client=httpx2.AsyncClient(
                    base_url=server.url, timeout=httpx2.Timeout(10.0, read=30.0)
                ),
                api_key=server.api_key,
                server_name=server.name,
                path_mappings=mappings,
                notify_topic_id=server.notify_topic_id,
                request_notify_topic_id=server.request_notify_topic_id,
            )
            self.sonarr_clients[server.id] = client
        elif server.server_type == ServerType.RADARR:
            client = RadarrClient(
                client=httpx2.AsyncClient(
                    base_url=server.url, timeout=httpx2.Timeout(10.0, read=30.0)
                ),
                api_key=server.api_key,
                server_name=server.name,
                path_mappings=mappings,
                notify_topic_id=server.notify_topic_id,
                request_notify_topic_id=server.request_notify_topic_id,
            )
            self.radarr_clients[server.id] = client

        if client:
            await client.login()

    async def _remove_and_close_client(self, server: ServerInstance):
        """(内部) 移除并关闭单个客户端"""
        client = None
        if server.server_type in (ServerType.EMBY, ServerType.JELLYFIN):
            client = self.media_clients.pop(server.id, None)
        elif server.server_type == ServerType.SONARR:
            client = self.sonarr_clients.pop(server.id, None)
        elif server.server_type == ServerType.RADARR:
            client = self.radarr_clients.pop(server.id, None)

        if client:
            await client.close()  # type: ignore

    async def _reload_server_client(self, server: ServerInstance):
        """(内部) 重载客户端"""
        await self._remove_and_close_client(server)
        await self._init_and_add_client(server)

    async def get_libraries_data(self, server_id: int) -> list[LibraryDto]:
        """获取服务器媒体库及绑定状态 (API)"""
        server = await self.server_repo.get_by_id(server_id)
        if not server or server.id not in self.media_clients:
            raise ValueError("未找到服务器或未连接")

        client = self.media_clients[server.id]
        libraries = await client.get_libraries() or []

        # 获取该媒体服务器下所有绑定
        bindings = await self.binding_repo.get_by_media_id(server_id)
        binding_map = {b.library_name: b for b in bindings}

        result = []
        for lib in libraries:
            lib_name = lib.Name
            # Emby uses Guid, Jellyfin uses ItemId (sometimes Id in API response)
            lib_id = (
                getattr(lib, "ItemId", None)
                or getattr(lib, "Guid", None)
                or getattr(lib, "Id", None)
            )

            dto = LibraryDto(name=lib_name, id=lib_id)

            # 填充绑定信息
            if lib_name in binding_map:
                binding = binding_map[lib_name]
                arr_server = await self.server_repo.get_by_id(binding.arr_id)
                dto.binding = BindingDto(
                    arr_id=binding.arr_id,
                    arr_name=arr_server.name if arr_server else "Unknown",
                    arr_type=arr_server.server_type if arr_server else "unknown",
                    quality_profile_id=binding.quality_profile_id,
                    root_folder=binding.root_folder,
                )
            result.append(dto)

        return result

    async def get_arr_servers_data(self) -> list[ArrServerDto]:
        """获取所有 Sonarr/Radarr 实例 (API)"""
        servers = []
        for s in await self.server_repo.get_by_type(ServerType.SONARR):
            servers.append(ArrServerDto(id=s.id, name=s.name, type="sonarr"))
        for r in await self.server_repo.get_by_type(ServerType.RADARR):
            servers.append(ArrServerDto(id=r.id, name=r.name, type="radarr"))
        return servers

    async def get_arr_resources(
        self, server_id: int
    ) -> tuple[list[QualityProfileDto], list[RootFolderDto]]:
        """获取 Sonarr/Radarr 的资源 (API)"""
        client = self.sonarr_clients.get(server_id) or self.radarr_clients.get(
            server_id
        )
        if not client:
            raise ValueError("Server instance not found")

        profiles = await client.get_quality_profiles() or []
        folders = await client.get_root_folders() or []

        p_dtos = [QualityProfileDto(id=p.id, name=p.name) for p in profiles]
        f_dtos = [
            RootFolderDto(id=f.id, path=f.path, freeSpace=f.freeSpace)
            for f in folders
            if f.path
        ]

        return p_dtos, f_dtos

    async def save_library_binding(
        self,
        library_name: str,
        media_server_id: int,
        arr_server_id: int,
        quality_id: int,
        root_folder: str,
    ) -> None:
        """保存媒体库绑定 (API)"""
        arr_server = await self.server_repo.get_by_id(arr_server_id)
        if not arr_server:
            raise ValueError("Arr server not found")

        await self.binding_repo.upsert(
            library_name=library_name,
            media_id=media_server_id,
            arr_id=arr_server_id,
            quality_profile_id=quality_id,
            root_folder=root_folder,
        )

    async def unbind_library(self, media_server_id: int, library_name: str) -> None:
        """解绑媒体库 (API)"""
        await self.binding_repo.delete(media_server_id, library_name)

    async def toggle_system_setting(self, key: str) -> Result:
        """切换系统功能设置"""
        try:
            default_val = (
                "false" if key == ConfigRepository.KEY_ENABLE_LLM_TRANSLATE else "true"
            )
            current = await self.config_repo.get_settings(key, default_val)
            new_state_str = "false" if current == "true" else "true"
            await self.config_repo.set_settings(key, new_state_str)

            if key == ConfigRepository.KEY_ENABLE_LLM_TRANSLATE:
                await self.reload_ai_client()

            status_text = "开启" if new_state_str == "true" else "关闭"
            return Result(success=True, message=f"已{status_text}该功能。")
        except Exception as e:
            return Result(success=False, message=f"设置失败: {str(e)}")

    async def update_llm_config(self, payload: LlmConfigUpdateDto) -> Result:
        """保存 Mini App 提交的 LLM 翻译参数并热重载 AI 客户端。

        Args:
            payload: LLM 配置更新请求体。

        Returns:
            操作结果。
        """
        base_url = payload.ai_base_url.strip()
        model = payload.ai_model.strip()
        if not base_url or not model:
            return Result(success=False, message="Base URL 和模型名称不能为空")
        if payload.ai_temperature < 0 or payload.ai_temperature > 2:
            return Result(success=False, message="Temperature 必须在 0 到 2 之间")
        if payload.ai_concurrency < 1:
            return Result(success=False, message="并发数至少为 1")

        # 保留掩码对应的已有密钥，仅在输入新密钥或显式清空时覆盖
        submitted_key = payload.ai_api_key.strip()
        if submitted_key != "******":
            await self.config_repo.set_settings(
                ConfigRepository.KEY_AI_API_KEY, submitted_key
            )

        await self.config_repo.set_settings(ConfigRepository.KEY_AI_BASE_URL, base_url)
        await self.config_repo.set_settings(ConfigRepository.KEY_AI_MODEL, model)
        await self.config_repo.set_settings(
            ConfigRepository.KEY_AI_TEMPERATURE, str(payload.ai_temperature)
        )
        await self.config_repo.set_settings(
            ConfigRepository.KEY_AI_RPM,
            str(payload.ai_rpm) if payload.ai_rpm and payload.ai_rpm > 0 else "",
        )
        await self.config_repo.set_settings(
            ConfigRepository.KEY_AI_RPD,
            str(payload.ai_rpd) if payload.ai_rpd and payload.ai_rpd > 0 else "",
        )
        await self.config_repo.set_settings(
            ConfigRepository.KEY_AI_TPM,
            str(payload.ai_tpm) if payload.ai_tpm and payload.ai_tpm > 0 else "",
        )
        await self.config_repo.set_settings(
            ConfigRepository.KEY_AI_CONCURRENCY, str(payload.ai_concurrency)
        )

        client = await self.reload_ai_client()
        llm_cfg = await self.config_repo.get_llm_config()
        if llm_cfg["enable_llm_translate"] and client is None:
            return Result(
                success=True,
                message="LLM 配置已保存（当前未填写 API Key，翻译客户端尚未激活）",
            )
        return Result(success=True, message="LLM 翻译配置已保存并生效")

    async def get_nsfw_libraries_data(self, server_id: int) -> list[NsfwLibraryDto]:
        """获取服务器所有媒体库的 NSFW 状态"""
        server = await self.server_repo.get_by_id(server_id)
        if not server or server.id not in self.media_clients:
            raise ValueError("未找到服务器或未连接")

        client = self.media_clients[server.id]
        libraries = await client.get_libraries() or []

        # 解析当前已存储的 NSFW ID 列表
        current_ids = (
            set(server.nsfw_library_ids.split("|"))
            if server.nsfw_library_ids
            else set()
        )

        result = []
        for lib in libraries:
            lib_id = (
                getattr(lib, "ItemId", None)
                or getattr(lib, "Guid", None)
                or getattr(lib, "Id", None)
            )
            if not lib_id:
                continue

            result.append(
                NsfwLibraryDto(
                    id=lib_id, name=lib.Name, is_nsfw=(lib_id in current_ids)
                )
            )
        return result

    async def toggle_nsfw_library(self, server_id: int, lib_id: str) -> Result:
        """切换 nsfw 媒体库设置"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        client = self.media_clients.get(server_id)
        if not client:
            return Result(False, "客户端未运行")

        is_emby = server.server_type == ServerType.EMBY
        nsfw_ids = (
            {i for i in server.nsfw_library_ids.split("|") if i}
            if server.nsfw_library_ids
            else set()
        )
        nsfw_sub_ids = (
            {i for i in server.nsfw_sub_library_ids.split("|") if i}
            if server.nsfw_sub_library_ids
            else set()
        )

        sub_folders: list[LibraryMediaFolder] | None = None

        if is_emby:
            sub_folders = await client.get_selectable_media_folders()

        if lib_id in nsfw_ids:
            nsfw_ids.remove(lib_id)
            if is_emby:
                nsfw_sub_ids = {
                    sub_id
                    for sub_id in nsfw_sub_ids
                    if not sub_id.startswith(f"{lib_id}_")
                }
            action = "移除"
        else:
            nsfw_ids.add(lib_id)
            if is_emby and sub_folders:
                for folder in sub_folders:
                    if folder.Guid == lib_id:
                        nsfw_sub_ids.update(
                            f"{lib_id}_{sub.Id}" for sub in folder.SubFolders
                        )
            action = "添加"

        await self.server_repo.update_nsfw_config(server_id, lib_ids="|".join(nsfw_ids))
        if is_emby:
            await self.server_repo.update_nsfw_config(
                server_id, sub_lib_ids="|".join(nsfw_sub_ids)
            )

        return Result(success=True, message=f"已{action}该媒体库。")

    async def add_server(
        self, name: str, server_type: str, url: str, api_key: str
    ) -> Result:
        """添加新服务器并初始化客户端"""
        try:
            # 默认优先级设为 0
            instance = await self.server_repo.add(
                name, server_type, url, api_key, priority=0
            )
        except IntegrityError:
            return Result(False, "服务器名称已存在，请勿重复添加。")
        except SQLAlchemyError as e:
            logger.error("数据库错误 when add_server: {}", e)
            return Result(False, "系统数据库错误，请联系管理员")
        except Exception as e:
            return Result(False, f"添加失败: {str(e)}")

        try:
            new_client = None
            if server_type == ServerType.EMBY:
                new_client = EmbyClient(
                    httpx2.AsyncClient(base_url=f"{url}/emby"), api_key
                )
                self.media_clients[instance.id] = new_client
            elif server_type == ServerType.JELLYFIN:
                new_client = JellyfinClient(httpx2.AsyncClient(base_url=url), api_key)
                self.media_clients[instance.id] = new_client
            elif server_type == ServerType.SONARR:
                new_client = SonarrClient(httpx2.AsyncClient(base_url=url), api_key)
                self.sonarr_clients[instance.id] = new_client
            elif server_type == ServerType.RADARR:
                new_client = RadarrClient(httpx2.AsyncClient(base_url=url), api_key)
                self.radarr_clients[instance.id] = new_client

            return Result(True, f"✅ 服务器 **{name}** 添加成功并已上线！")

        except httpx2.HTTPError as e:
            # 初始化连接失败
            await self.server_repo.delete(instance.id)
            return Result(False, f"❌ 连接服务器失败 (已回滚): {e}")
        except Exception as e:
            # 其他初始化失败，回滚数据库
            await self.server_repo.delete(instance.id)
            return Result(False, f"❌ 客户端初始化失败 (已回滚): {str(e)}")

    async def delete_server(self, server_id: int) -> Result:
        """删除服务器"""
        if server_id in self.media_clients:
            del self.media_clients[server_id]
        if server_id in self.sonarr_clients:
            del self.sonarr_clients[server_id]
        if server_id in self.radarr_clients:
            del self.radarr_clients[server_id]

        await self.server_repo.delete(server_id)
        return Result(True, "服务器已删除")
