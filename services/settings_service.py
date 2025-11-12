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
from models.orm import LibraryBindingModel
from repositories.config_repo import ConfigRepository
from repositories.telegram_repo import TelegramRepository
from services.media_service import MediaService
from services.user_service import Result
from loguru import logger

settings = get_settings()

class SettingsServices:
    def __init__(self, session: AsyncSession, app: FastAPI ) -> None:
        self.app = app
        self.session = session
        self.client: TelethonClientWarper = app.state.telethon_client
        self.telegram_repo = TelegramRepository(session)
        self.config_repo = ConfigRepository(session)

    async def get_admin_management_keyboard(self) -> Result:
        """获取管理员管理面板的键盘布局。
        
        Returns:
            list[list]: 返回键盘布局的二维列表。
        """
        keyboard = [
            [Button.inline("管理员设置", b"manage_admins")],
            [Button.inline("通知设置", b"manage_notify")],
            [Button.inline("媒体设置", b"manage_media")]
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

    async def get_media_panel(self):
        """获取媒体设置面板。
        
        Returns:
            Result: 包含媒体设置和键盘布局的结果对象。
        """
        media_client: MediaService = self.app.state.media_client

        library_names = await media_client.get_library_names()
        if library_names is None:
            return Result(success=False, message="获取媒体库名称失败，请检查媒体服务器连接。")

        bindings = await self.config_repo.get_all_library_bindings()
        keyboard = []

        for library_name in library_names:
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
        sonarr_client: SonarrClient | None = self.app.state.sonarr_client if self.app.state.sonarr_client else None
        radarr_client: RadarrClient | None = self.app.state.radarr_client if self.app.state.radarr_client else None
        if sonarr_client is None or radarr_client is None:
            return Result(success=False, message="Sonarr 或 Radarr 客户端未配置，无法获取媒体设置面板。")
        binding = await self.config_repo.get_library_binding(library_name)
        library_name_base64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        arr_type = binding.arr_type

        if arr_type == 'sonarr':
            quality_profiles = await sonarr_client.get_quality_profiles()
        elif arr_type == 'radarr':
            quality_profiles = await radarr_client.get_quality_profiles()
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

    async def get_root_folder_selection_keyboard(self, library_name: str) -> Result:
        """获取根文件夹选择的键盘布局。
        Args:
            library_name (str): 媒体库名称。
        Returns:
            Result: 包含键盘布局的结果对象.
        """
        sonarr_client: SonarrClient | None = self.app.state.sonarr_client if self.app.state.sonarr_client else None
        radarr_client: RadarrClient | None = self.app.state.radarr_client if self.app.state.radarr_client else None
        if sonarr_client is None or radarr_client is None:
            return Result(success=False, message="Sonarr 或 Radarr 客户端未配置，无法获取媒体设置面板。")
        binding = await self.config_repo.get_library_binding(library_name)
        library_name_base64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        arr_type = binding.arr_type

        if arr_type == 'sonarr':
            root_folders = await sonarr_client.get_root_folders()
        elif arr_type == 'radarr':
            root_folders = await radarr_client.get_root_folders()
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
