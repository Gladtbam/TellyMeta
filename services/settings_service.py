import base64
import textwrap

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button
from telethon.tl.types import ForumTopicDeleted

from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from models.emby import LibraryMediaFolder
from models.orm import LibraryBindingModel
from repositories.config_repo import ConfigRepository
from repositories.telegram_repo import TelegramRepository
from services.media_service import MediaService
from services.user_service import Result

settings = get_settings()

class SettingsServices:
    def __init__(self, app: FastAPI, session: AsyncSession) -> None:
        self.app = app
        self.client: TelethonClientWarper = app.state.telethon_client
        self.telegram_repo = TelegramRepository(session)
        self.config_repo = ConfigRepository(session)
        self.media_client: MediaService = app.state.media_client
        self._sonarr_client = app.state.sonarr_client
        self._radarr_client = app.state.radarr_client

    @property
    def sonarr_client(self) -> SonarrClient:
        if self._sonarr_client is None:
            raise RuntimeError("Sonarr 客户端未配置")
        return self._sonarr_client

    @property
    def radarr_client(self) -> RadarrClient:
        if self._radarr_client is None:
            raise RuntimeError("Radarr 客户端未配置")
        return self._radarr_client

    async def get_admin_management_keyboard(self) -> Result:
        """获取管理员管理面板的键盘布局。
        
        Returns:
            list[list]: 返回键盘布局的二维列表。
        """
        keyboard = [
            [Button.inline("管理员设置", b"manage_admins")],
            [Button.inline("通知设置", b"manage_notify")],
            [Button.inline("媒体设置", b"manage_media")],
            [Button.inline("功能开关", b"manage_system")]
        ]
        msg = "请选择一个管理选项："
        return Result(success=True, message=msg, keyboard=keyboard)

    async def get_admins_panel(self) -> Result:
        """获取管理员列表面板。
        
        Returns:
            Result: 包含管理员列表和键盘布局的结果对象。
        """
        bot_admins = await self.telegram_repo.get_admins()
        group_admins = await self.client.get_chat_admin_ids()

        keyboard = []
        msg = textwrap.dedent("""\
            **Bot 管理员设置**
            点击按钮以添加或撤销用的 Bot 管理员权限。
        """)

        for admin in group_admins:
            status = "✅" if admin.id in bot_admins else "❌"
            button_text = f"{status} {admin.first_name or ''} {admin.last_name or ''} (@{admin.username or '无用户名'})"
            callback_data = f"toggle_admin_{admin.id}"
            keyboard.append([Button.inline(button_text, callback_data.encode('utf-8'))])
        keyboard.append([Button.inline("« 返回管理面板", b"manage_main")])

        return Result(success=True, message=msg, keyboard=keyboard)

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
                return Result(success=True, message=f"已撤销用户 {user_id} 的管理员权限。")
            else:
                await self.telegram_repo.toggle_admin(user_id, is_admin=True)
                self.app.state.admin_ids.add(user_id)
                return Result(success=True, message=f"已授予用户 {user_id} 管理员权限。")
        except (ValueError, KeyError) as e:
            return Result(success=False, message=str(e))

    async def get_notification_panel(self) -> Result:
        """获取通知设置面板。
        
        Returns:
            Result: 包含通知设置和键盘布局的结果对象。
        """
        sonarr_notify_topic = await self.config_repo.get_settings(
            "sonarr_notify_topic", "未设置"
        )
        radarr_notify_topic = await self.config_repo.get_settings(
            "radarr_notify_topic", "未设置"
        )
        media_notify_topic = await self.config_repo.get_settings(
            "media_notify_topic", "未设置"
        )
        requested_notify_topic = await self.config_repo.get_settings(
            "requested_notify_topic", "未设置"
        )

        keyboard = [
            [Button.inline("设置 Sonarr 通知话题", b"notify_sonarr")],
            [Button.inline("设置 Radarr 通知话题", b"notify_radarr")],
            [Button.inline("设置 媒体通知话题", b"notify_media")],
            [Button.inline("设置 求片通知话题", b"notify_requested")],
            [Button.inline("« 返回管理面板", b"manage_main")]
        ]
        msg = textwrap.dedent(f"""\
            **通知设置面板**
            Sonarr 通知话题: `{sonarr_notify_topic}`
            Radarr 通知话题: `{radarr_notify_topic}`
            媒体通知话题: `{media_notify_topic}`
            求片通知话题: `{requested_notify_topic}`

            点击按钮以更改相应的通知话题。
        """)
        return Result(success=True, message=msg, keyboard=keyboard)

    async def get_notification_keyboard(self, setting_key: str):
        """获取通知设置的键盘布局。
        Args:
            setting_key (str): 设置的通知键。
        Returns:
            list[list]: 返回键盘布局的二维列表。
        """
        topics = await self.client.get_group_topics()
        keyboard = []
        msg = textwrap.dedent(f"""\
            **选择 {setting_key} 通知话题**
            请选择一个话题以设置为 {setting_key} 通知的话题。
        """)
        if isinstance(topics, int):
            keyboard.append([Button.inline(str(topics), f"set_notify_{setting_key}_{topics}".encode('utf-8'))])
            return Result(success=False, message=msg, keyboard=keyboard)
        for topic in topics:
            if isinstance(topic, ForumTopicDeleted):
                continue
            button_text = topic.title
            callback_data = f"set_notify_{setting_key}_{topic.id}"
            keyboard.append([Button.inline(button_text, callback_data.encode('utf-8'))])
        return Result(success=True, message=msg, keyboard=keyboard)

    async def set_notification_topic(self, setting_key: str, topic: int) -> Result:
        """设置通知话题。
        Args:
            setting_key (str): 设置的通知键。
            topic (int): 要设置的话题。
        Returns:
            Result: 包含操作结果的对象。
        """
        await self.config_repo.set_settings(f'{setting_key}_notify_topic', str(topic))
        return Result(success=True, message=f"已将 {setting_key} 通知设置为 `{topic}`。")

    async def get_media_panel(self):
        """获取媒体设置面板。
        
        Returns:
            Result: 包含媒体设置和键盘布局的结果对象。
        """
        libraries = await self.media_client.get_libraries()
        if libraries is None:
            return Result(success=False, message="获取媒体库名称失败，请检查媒体服务器连接。")

        bindings = await self.config_repo.get_all_library_bindings()
        keyboard = []

        for lib in libraries:
            library_name = lib.Name

            binding = bindings.get(library_name, LibraryBindingModel(library_name=library_name))

            status = "❓ 未配置"
            if binding.quality_profile_id and binding.root_folder:
                status = "✅"
            else:
                status = "⚠️"
            button_text = textwrap.dedent(f"""\
                {library_name} {status} - {binding.arr_type or '未知类型'}: {binding.quality_profile_id or '未设置'}({binding.root_folder or '未设置'})
            """)
            library_name_base64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8') # 避免回调数据中出现特殊字符
            keyboard.append([Button.inline(button_text, f"bind_library_{library_name_base64}".encode('utf-8'))])
        keyboard.append([Button.inline("设置 NSFW 媒体库", b"manage_nsfw_library")])
        keyboard.append([Button.inline("« 返回管理面板", b"manage_main")])


        msg = textwrap.dedent("""\
            **媒体设置面板**
            点击按钮以配置媒体库绑定设置。
            媒体库名 状态 - 类型: 质量配置文件ID(根文件夹)
        """)
        return Result(success=True, message=msg, keyboard=keyboard)

    async def get_library_binding_panel(self, library_name: str) -> Result:
        """获取媒体库绑定设置面板。
        Args:
            library_name (str): 媒体库名称。
        Returns:
            Result: 包含媒体库绑定设置和键盘布局的结果对象。
        """
        binding = await self.config_repo.get_library_binding(library_name)
        arr_type = binding.arr_type or "未知类型"
        quality_id = binding.quality_profile_id or "未设置"
        root_folder = binding.root_folder or "未设置"
        library_name_base64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        keyboard = [
            [Button.inline("更改类型 (Sonarr/Radarr)", data=f"select_typed_{library_name_base64}".encode('utf-8'))],
            [Button.inline("选择质量配置文件", f"select_quality_{library_name_base64}".encode('utf-8'))],
            [Button.inline("选择根文件夹", f"select_folder_{library_name_base64}".encode('utf-8'))],
            [Button.inline("« 返回媒体库列表", data="manage_media")]
        ]
        msg = textwrap.dedent(f"""\
            **媒体库绑定设置 - {library_name}**
            
            类型: `{arr_type}`
            质量配置文件 ID: `{quality_id}`
            根文件夹: `{root_folder}`

            点击按钮以更改相应的设置。
        """)
        return Result(success=True, message=msg, keyboard=keyboard)

    async def get_type_selection_keyboard(self, library_name: str) -> Result:
        """获取类型选择的键盘布局。
        Args:
            library_name (str): 媒体库名称。
        Returns:
            Result: 包含键盘布局的结果对象。
        """
        library_name_base64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        keyboard = [
            [Button.inline("Sonarr", data=f"set_typed_sonarr_{library_name_base64}".encode('utf-8'))],
            [Button.inline("Radarr", data=f"set_typed_radarr_{library_name_base64}".encode('utf-8'))],
            [Button.inline("« 返回媒体库绑定设置", data=f"bind_library_{library_name_base64}".encode('utf-8'))]
        ]
        msg = textwrap.dedent(f"""\
            **选择 {library_name} 的类型**
            请选择该媒体库对应的管理类型。
        """)
        return Result(success=True, message=msg, keyboard=keyboard)

    async def get_quality_selection_keyboard(self, library_name: str) -> Result:
        """获取质量配置文件选择的键盘布局。
        Args:
            library_name (str): 媒体库名称。
        Returns:
            Result: 包含键盘布局的结果对象。
        """
        try:
            binding = await self.config_repo.get_library_binding(library_name)
            library_name_base64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
            arr_type = binding.arr_type

            if arr_type == 'sonarr':
                quality_profiles = await self.sonarr_client.get_quality_profiles()
            elif arr_type == 'radarr':
                quality_profiles = await self.radarr_client.get_quality_profiles()
            else:
                return Result(success=False, message="请先设置媒体库的类型为 Sonarr 或 Radarr。",
                    keyboard=[Button.inline("去设置类型", data=f"select_typed_{library_name_base64}".encode('utf-8'))])

            if quality_profiles is None:
                return Result(success=False, message="获取质量配置文件失败，请检查 Sonarr/Radarr 连接。")

            keyboard = []
            for profile in quality_profiles:
                callback_data = f"set_quality_{profile.id}_{library_name_base64}"
                keyboard.append([Button.inline(f"{profile.name}: {profile.id}", callback_data.encode('utf-8'))])

            keyboard.append([Button.inline("« 返回媒体库绑定设置", data=f"bind_library_{library_name_base64}".encode('utf-8'))])

            msg = textwrap.dedent(f"""\
                **选择 {library_name} 的质量配置文件**
                请选择一个质量配置文件。
            """)
            return Result(success=True, message=msg, keyboard=keyboard)
        except RuntimeError:
            return Result(success=False, message="Sonarr 或 Radarr 客户端未配置，无法获取媒体设置面板。")

    async def get_root_folder_selection_keyboard(self, library_name: str) -> Result:
        """获取根文件夹选择的键盘布局。
        Args:
            library_name (str): 媒体库名称。
        Returns:
            Result: 包含键盘布局的结果对象.
        """
        try:
            binding = await self.config_repo.get_library_binding(library_name)
            library_name_base64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
            arr_type = binding.arr_type

            if arr_type == 'sonarr':
                root_folders = await self.sonarr_client.get_root_folders()
            elif arr_type == 'radarr':
                root_folders = await self.radarr_client.get_root_folders()
            else:
                return Result(success=False, message="请先设置媒体库的类型为 Sonarr 或 Radarr。",
                    keyboard=[Button.inline("去设置类型", data=f"select_typed_{library_name_base64}".encode('utf-8'))])

            if root_folders is None:
                return Result(success=False, message="获取根文件夹失败，请检查 Sonarr/Radarr 连接。")

            keyboard = []
            for folder in root_folders:
                callback_data = f"set_folder_{folder.path}_{library_name_base64}"
                keyboard.append([Button.inline(folder.path, callback_data.encode('utf-8'))])

            keyboard.append([Button.inline("« 返回媒体库绑定设置", data=f"bind_library_{library_name_base64}".encode('utf-8'))])

            msg = textwrap.dedent(f"""\
                **选择 {library_name} 的根文件夹**
                请选择一个根文件夹。
            """)
            return Result(success=True, message=msg, keyboard=keyboard)
        except RuntimeError:
            return Result(success=False, message="Sonarr 或 Radarr 客户端未配置，无法获取媒体设置面板。")

    async def set_library_binding(self, library_name: str, key: str, value: str | int) -> Result:
        """设置媒体库绑定的某个属性。
        Args:
            library_name (str): 媒体库名称。
            key (str): 要设置的属性键。
            value (str): 要设置的属性值。
        Returns:
            Result: 包含操作结果的对象。
        """
        binding = await self.config_repo.get_library_binding(library_name)
        setattr(binding, key, value)
        await self.config_repo.set_library_binding(binding)
        return Result(success=True, message=f"已将媒体库 {library_name} 的 {key} 设置为 `{value}`。")

    async def get_system_panel(self) -> Result:
        """获取系统功能设置面板"""

        points_enabled = self.config_repo.cache.get(ConfigRepository.KEY_ENABLE_POINTS) == "true"
        verify_enabled = self.config_repo.cache.get(ConfigRepository.KEY_ENABLE_VERIFICATION) == "true"
        request_enabled = self.config_repo.cache.get(ConfigRepository.KEY_ENABLE_REQUESTMEDIA) == "true"

        points_status = "✅ 开启" if points_enabled else "❌ 关闭"
        verify_status = "✅ 开启" if verify_enabled else "❌ 关闭"
        request_status = "✅ 开启" if request_enabled else "❌ 关闭"

        nsfw_enabled = await self.config_repo.get_settings('nsfw_enabled', 'true') == 'true'
        nsfw_status = "✅ 开启" if nsfw_enabled else "❌ 关闭"

        keyboard = [
            [Button.inline(f"积分/签到功能: {points_status}", f"toggle_system_{ConfigRepository.KEY_ENABLE_POINTS}".encode('utf-8'))],
            [Button.inline(f"入群验证: {verify_status}", f"toggle_system_{ConfigRepository.KEY_ENABLE_VERIFICATION}".encode('utf-8'))],
            [Button.inline(f"求片: {request_status}", f"toggle_system_{ConfigRepository.KEY_ENABLE_REQUESTMEDIA}".encode('utf-8'))],
            [Button.inline(f"新用户默认开启 NSFW: {nsfw_status}", b"toggle_system_nsfw_enabled")],
            [Button.inline("« 返回管理面板", b"manage_main")]
        ]
        msg = textwrap.dedent("""\
            **系统功能开关**
            点击按钮以开启或关闭相应功能。
            开关状态已缓存，无需担心性能问题。
        """)
        return Result(success=True, message=msg, keyboard=keyboard)

    async def toggle_system_setting(self, key: str) -> Result:
        """切换系统功能设置"""
        try:
            current = await self.config_repo.get_settings(key, "true")
            new_state_str = "false" if current == "true" else "true"
            await self.config_repo.set_settings(key, new_state_str)

            status_text = "开启" if new_state_str == "true" else "关闭"
            return Result(success=True, message=f"已{status_text}该功能。")
        except Exception as e:
            return Result(success=False, message=f"设置失败: {str(e)}")

    async def get_nsfw_library_panel(self) -> Result:
        """获取 nsfw 媒体库设置面板"""
        libraries = await self.media_client.get_libraries()

        if libraries is None:
            return Result(success=False, message="获取媒体库列表失败，请检查媒体服务器连接。")


        nsfw_ids_str = await self.config_repo.get_settings('nsfw_library', '')
        nsfw_ids = nsfw_ids_str.split('|') if nsfw_ids_str else []

        keyboard = []
        for lib in libraries:
            lib_name = lib.Name
            lib_id = lib.ItemId if settings.media_server.lower() == 'jellyfin' else lib.Guid

            if not lib_id:
                continue

            status = "✅" if lib_id in nsfw_ids else "❌"

            button_text = f"{status} {lib_name}"
            callback_data = f"toggle_nsfw_lib_{lib_id}"
            keyboard.append([Button.inline(button_text, callback_data.encode('utf-8'))])

        keyboard.append([Button.inline("« 返回媒体设置", b"manage_media")])

        msg = textwrap.dedent("""\
            **nsfw 媒体库设置**
            点击按钮以将其标记为 NSFW 媒体库。
            标记为 NSFW 的媒体库将允许用户自行选择是否单独开启/关闭。
        """)
        return Result(success=True, message=msg, keyboard=keyboard)

    async def toggle_nsfw_library(self, lib_id: str) -> Result:
        """切换 nsfw 媒体库设置"""
        is_emby = settings.media_server.lower() == 'emby'
        nsfw_ids_str = await self.config_repo.get_settings('nsfw_library', '')
        # nsfw_ids = set(nsfw_ids_str.split('|')) if nsfw_ids_str else set()
        nsfw_ids = {i for i in nsfw_ids_str.split('|') if i} if nsfw_ids_str else set()
        sub_folders: list[LibraryMediaFolder] | None = None
        nsfw_sub_ids: set[str] = set()

        if is_emby:
            nsfw_sub_ids_str = await self.config_repo.get_settings('nsfw_sub_library', '')
            nsfw_sub_ids = {i for i in nsfw_sub_ids_str.split('|') if i} if nsfw_sub_ids_str else set()
            sub_folders = await self.media_client.get_selectable_media_folders()

        if lib_id in nsfw_ids:
            nsfw_ids.remove(lib_id)
            if is_emby:
                nsfw_sub_ids = {sub_id for sub_id in nsfw_sub_ids if not sub_id.startswith(f"{lib_id}_")}
            action = "移除"
        else:
            nsfw_ids.add(lib_id)
            if is_emby and sub_folders:
                for folder in sub_folders:
                    if folder.Guid == lib_id:
                        nsfw_sub_ids.update(f"{lib_id}_{sub.Id}" for sub in folder.SubFolders)
            action = "添加"

        new_ids_str = '|'.join(nsfw_ids)
        await self.config_repo.set_settings('nsfw_library', new_ids_str)
        if is_emby:
            new_sub_ids_str = '|'.join(nsfw_sub_ids)
            await self.config_repo.set_settings('nsfw_sub_library', value=new_sub_ids_str)

        return Result(success=True, message=f"已{action}该媒体库。")
