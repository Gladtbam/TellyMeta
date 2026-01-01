import base64
import json
import re
import textwrap
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button
from telethon.tl.types import ForumTopicDeleted

from clients.emby_client import EmbyClient
from clients.jellyfin_client import JellyfinClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from models.emby import LibraryMediaFolder
from models.orm import (LibraryBindingModel, RegistrationMode, ServerInstance,
                        ServerType)
from repositories.config_repo import ConfigRepository
from repositories.server_repo import ServerRepository
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
        self.server_repo = ServerRepository(session)
        self.media_clients: dict[int, MediaService] = app.state.media_clients
        self._sonarr_clients = app.state.sonarr_clients
        self._radarr_clients = app.state.radarr_clients

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

    async def _get_topic_name(self, topic_id: int | None, topic_map: dict[int, str] | None = None) -> str:
        """内部辅助：根据 ID 获取名称"""
        if not topic_id:
            return "未设置"

        if topic_map is None:
            topic_map = await self.client.get_topic_map()

        return topic_map.get(topic_id, f"未知目标({topic_id})")

    async def get_admin_management_keyboard(self) -> Result:
        """获取管理员管理面板的键盘布局。
        
        Returns:
            list[list]: 返回键盘布局的二维列表。
        """
        keyboard = [
            [Button.inline("👥 管理员设置", b"manage_admins")],
            [Button.inline("🖥️ 服务器与媒体设置", b"manage_media")],
            [Button.inline("⚙️ 系统功能开关", b"manage_system")]
        ]
        msg = "🔧 请选择一个管理选项："
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
        keyboard.append([Button.inline("« 返回主菜单", b"manage_main")])

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
        all_servers = await self.server_repo.get_all()

        media_servers = [s for s in all_servers if s.server_type in (ServerType.EMBY, ServerType.JELLYFIN)]
        arr_servers = [s for s in all_servers if s.server_type in (ServerType.SONARR, ServerType.RADARR)]

        keyboard = []

        keyboard.append([Button.inline("—— 📺 媒体服务器 (点击管理) ——", data=b"ignore")])
        if media_servers:
            for server in media_servers:
                status = "🟢" if server.is_enabled else "🔴"
                keyboard.append([
                    Button.inline(f"{status} {server.name} ({server.server_type})",
                                data=f"view_server_{server.id}".encode('utf-8'))
                ])
        else:
            keyboard.append([Button.inline("⚠️ 暂无，点击添加", data=b"add_server_flow")])

        keyboard.append([Button.inline("—— 📥 媒体管理服务器 (点击管理) ——", data=b"ignore")])
        if arr_servers:
            for server in arr_servers:
                status = "🟢" if server.is_enabled else "🔴"
                icon = "📺" if server.server_type == ServerType.SONARR else "🎬"
                keyboard.append([
                    Button.inline(f"{status} {icon} {server.name}",
                                data=f"view_server_{server.id}".encode('utf-8'))
                ])
        else:
            keyboard.append([Button.inline("⚠️ 暂无，点击添加", data=b"add_server_flow")])

        # 3. 全局操作
        keyboard.append([Button.inline("—— 🛠️ 操作 ——", data=b"ignore")])
        keyboard.append([
            Button.inline("➕ 添加服务器", data=b"add_server_flow"),
            Button.inline("🔄 刷新缓存", data=b"refresh_cache") # 预留
        ])
        keyboard.append([Button.inline("« 返回主菜单", b"manage_main")])

        msg = textwrap.dedent("""\
            **🎛 服务器管理面板**
            
            在此管理所有的 Emby/Jellyfin, Sonarr, Radarr 实例。
            
            • **媒体服务器**: 配置策略、有效期、NSFW 及媒体库绑定。
            • **媒体管理服务器**: 查看状态、修改信息。
            🔴 代表已停用，🟢 代表运行中。
        """)
        return Result(success=True, message=msg, keyboard=keyboard)

    async def get_server_detail_panel(self, server_id: int) -> Result:
        """获取单个服务器的详细管理面板"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在。")

        topic_map = await self.client.get_topic_map()
        notify_name = await self._get_topic_name(server.notify_topic_id, topic_map)
        req_notify_name = await self._get_topic_name(server.request_notify_topic_id, topic_map)

        status_text = "运行中" if server.is_enabled else "已停用"
        status_icon = "🟢" if server.is_enabled else "🔴"
        toggle_label = "🔴 停用" if server.is_enabled else "🟢 启用"
        info = textwrap.dedent(f"""\
            **🖥️ 服务器详情 - {server.name}**

            🆔 ID: `{server.id}`
            状态: {status_icon} **{status_text}**
            类型: `{server.server_type.capitalize()}`
            地址: `{server.url}`

            🔗 **Webhook URL**:

            `/webhook/{server.server_type}?server_id={server.id}`

            🔔 **常规通知**: `{notify_name}`
        """)

        if server.server_type in (ServerType.SONARR, ServerType.RADARR):
            info += f"\n🙋 **求片通知**: `{req_notify_name}`\n"
            if server.path_mappings:
                try:
                    mappings = json.loads(server.path_mappings)
                    info += "\n\n**📂 路径映射 (Remote -> Local)**:\n"
                    for remote, local in mappings.items():
                        info += f"`{remote}` ➡️ `{local}`\n"
                except:
                    info += "\n\n**📂 路径映射**: 解析错误"

        keyboard = []
        keyboard.append([
            Button.inline("✏️ 修改名称", data=f"srv_edit_name_{server.id}".encode('utf-8')),
            Button.inline("✏️ 修改地址", data=f"srv_edit_url_{server.id}".encode('utf-8'))
        ])
        keyboard.append([
            Button.inline("🔑 修改 API Key", data=f"srv_edit_key_{server.id}".encode('utf-8')),
            Button.inline(toggle_label, data=f"srv_toggle_enable_{server.id}".encode('utf-8'))
        ])

        keyboard.append([Button.inline("—— 🔔 通知频道 ——", data=b"ignore")])
        short_notify = (notify_name[:12] + '..') if len(notify_name) > 12 else notify_name
        keyboard.append([
            Button.inline(f"常规: {short_notify}", data=f"srv_set_notify_{server.id}_normal".encode('utf-8'))
        ])
        if server.server_type in (ServerType.SONARR, ServerType.RADARR):
            short_req = (req_notify_name[:12] + '..') if len(req_notify_name) > 12 else req_notify_name
            keyboard.append([
                Button.inline(f"求片: {short_req}", data=f"srv_set_notify_{server.id}_request".encode('utf-8'))
            ])
        # 针对媒体服务器 (Emby/Jellyfin) 的特有配置
        if server.server_type in (ServerType.EMBY, ServerType.JELLYFIN):
            nsfw_status = "✅ 开启" if server.nsfw_enabled else "❌ 关闭"
            mode_map = {'default': '默认(邀请/积分)', 'open': '开放', 'count': '限额', 'time': '限时', 'close': '关闭'}
            reg_mode = mode_map.get(server.registration_mode, server.registration_mode)

            info += textwrap.dedent(f"""\
                **策略配置**:

                • 注册模式: `{reg_mode}`
                • 默认有效期: `{server.registration_expiry_days} 天`
                • NSFW 限制: `{nsfw_status}`
            """)

            # 功能按钮
            keyboard.append([
                Button.inline("📝 修改用户协议 (TOS)", data=f"srv_edit_tos_{server.id}".encode('utf-8'))
            ])
            keyboard.append([
                Button.inline(f"🔞 NSFW: {nsfw_status}", data=f"srv_nsfw_toggle_{server.id}".encode('utf-8')),
                Button.inline("🔞 管理 NSFW 库", data=f"srv_nsfw_libs_{server.id}".encode('utf-8'))
            ])
            keyboard.append([
                Button.inline("📝 注册模式", data=f"srv_reg_mode_{server.id}".encode('utf-8')),
                Button.inline("⏳ 有效期", data=f"srv_expiry_{server.id}".encode('utf-8'))
            ])
            keyboard.append([Button.inline("📂 媒体库绑定 (关联媒体管理服务器)", data=f"manage_libs_{server.id}".encode('utf-8'))])

        elif server.server_type in (ServerType.SONARR, ServerType.RADARR):
            keyboard.append([Button.inline("—— 高级设置 ——", data=b"ignore")])
            keyboard.append([
                Button.inline("📂 修改路径映射", data=f"srv_edit_mapping_{server.id}".encode('utf-8'))
            ])

        # 通用按钮
        keyboard.append([
            Button.inline("🗑️ 删除服务器", data=f"delete_server_confirm_{server.id}".encode('utf-8'))
        ])
        keyboard.append([Button.inline("« 返回列表", data=b"manage_media")])

        return Result(True, info, keyboard=keyboard)

    async def update_server_field(self, server_id: int, field: str, value: str) -> Result:
        """更新服务器字段并热重载客户端"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        kwargs = {field: value}
        await self.server_repo.update_basic_info(server_id, **kwargs)

        if field in ['url', 'api_key'] and server.is_enabled:
            await self._reload_server_client(server)

        return Result(True, f"✅ 已更新服务器 {field}。")

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
            try:
                mappings = json.loads(server.path_mappings)
            except:
                pass

        if server.server_type == ServerType.EMBY:
            client = EmbyClient(
                httpx.AsyncClient(base_url=f"{server.url}/emby"),
                server.api_key,
                server.name,
                server.notify_topic_id
            )
            self.media_clients[server.id] = client
        elif server.server_type == ServerType.JELLYFIN:
            client = JellyfinClient(
                httpx.AsyncClient(base_url=server.url),
                server.api_key,
                server.name,
                server.notify_topic_id
            )
            self.media_clients[server.id] = client
        elif server.server_type == ServerType.SONARR:
            client = SonarrClient(
                httpx.AsyncClient(base_url=server.url),
                server.api_key,
                server.name,
                mappings,
                server.notify_topic_id,
                server.request_notify_topic_id
            )
            self.sonarr_clients[server.id] = client
        elif server.server_type == ServerType.RADARR:
            client = RadarrClient(
                httpx.AsyncClient(base_url=server.url),
                server.api_key,
                server.name,
                mappings,
                server.notify_topic_id,
                server.request_notify_topic_id
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
            await client.close() # type: ignore

    async def _reload_server_client(self, server: ServerInstance):
        """(内部) 重载客户端"""
        await self._remove_and_close_client(server)
        await self._init_and_add_client(server)

    async def update_server_mapping(self, server_id: int, mappings: dict[str, str]) -> Result:
        """更新路径映射"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        mapping_str = json.dumps(mappings)
        await self.server_repo.update_basic_info(server_id, path_mappings=mapping_str)

        if server.is_enabled:
            await self._reload_server_client(server)

        return Result(True, "✅ 路径映射已更新。")

    async def get_server_libraries_panel(self, server_id: int) -> Result:
        """列出指定媒体服务器的所有库，进行绑定管理"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        client = self.media_clients.get(server.id)
        if not client:
            return Result(False, f"客户端未运行: {server.name}")

        try:
            libraries = await client.get_libraries()
        except Exception as e:
            return Result(False, f"连接失败: {e}")

        if libraries is None:
            return Result(False, "无法获取媒体库列表。")

        bindings = await self.config_repo.get_all_library_bindings()
        keyboard = []

        for lib in libraries:
            lib_name = lib.Name
            binding = bindings.get(lib_name, LibraryBindingModel(library_name=lib_name))

            status_icon = "⚪"
            bind_name = "未绑定"

            if binding and binding.server_id:
                arr_server = await self.server_repo.get_by_id(binding.server_id)
                if arr_server:
                    status_icon = "🟢"
                    bind_name = arr_server.name
                else:
                    status_icon = "⚠️"
                    bind_name = "实例失效"

            lib_name_b64 = base64.b64encode(lib_name.encode('utf-8')).decode('utf-8')
            keyboard.append([
                Button.inline(f"{status_icon} {lib_name} -> {bind_name}",
                            data=f"bind_lib_menu_{lib_name_b64}".encode('utf-8'))
            ])

        keyboard.append([Button.inline("« 返回服务器详情", data=f"view_server_{server.id}".encode('utf-8'))])

        msg = textwrap.dedent(f"""\
            **📂 媒体库绑定 - {server.name}**
            
            请点击下方媒体库，将其绑定到 Sonarr 或 Radarr 实例。
            绑定后才可使用求片和字幕上传功能。
        """)
        return Result(True, msg, keyboard=keyboard)

    async def get_library_binding_menu(self, library_name: str) -> Result:
        """获取单个媒体库的绑定设置菜单"""
        binding = await self.config_repo.get_library_binding(library_name)

        server_name = "未设置"
        if binding.server_id:
            server = await self.server_repo.get_by_id(binding.server_id)
            server_name = f"{server.name} ({server.server_type})" if server else "⚠️ ID失效"

        lib_b64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')

        keyboard = [
            [Button.inline(f"📡 实例: {server_name}", data=f"bind_sel_server_{lib_b64}".encode('utf-8'))],
        ]

        # 只有选了服务器才显示后续配置
        if binding.server_id:
            keyboard.append([Button.inline(f"⚙️ 质量: {binding.quality_profile_id or '未设置'}", f"bind_sel_quality_{lib_b64}".encode('utf-8'))])
            keyboard.append([Button.inline(f"📂 路径: {binding.root_folder or '未设置'}", f"bind_sel_folder_{lib_b64}".encode('utf-8'))])

        keyboard.append([Button.inline("« 返回主面板", data="manage_media")])

        msg = textwrap.dedent(f"""\
            **⚙️ 绑定配置 - {library_name}**
            
            当前绑定实例: `{server_name}`
            质量配置 ID: `{binding.quality_profile_id}`
            根目录路径: `{binding.root_folder}`
        """)
        return Result(True, msg, keyboard=keyboard)

    async def get_arr_server_selection(self, library_name: str) -> Result:
        """选择要绑定的 Sonarr/Radarr 实例"""
        sonarrs = await self.server_repo.get_by_type(ServerType.SONARR)
        radarrs = await self.server_repo.get_by_type(ServerType.RADARR)

        if not sonarrs and not radarrs:
            return Result(False, "未找到任何媒体管理服务器实例。")

        lib_b64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        keyboard = []

        for s in sonarrs:
            keyboard.append([Button.inline(f"📺 Sonarr: {s.name}", f"bind_set_srv_{s.id}_{lib_b64}".encode('utf-8'))])
        for r in radarrs:
            keyboard.append([Button.inline(f"🎬 Radarr: {r.name}", f"bind_set_srv_{r.id}_{lib_b64}".encode('utf-8'))])

        keyboard.append([Button.inline("« 返回", f"bind_lib_menu_{lib_b64}".encode('utf-8'))])

        return Result(True, "请选择要绑定的实例：", keyboard=keyboard)

    async def bind_server_to_library(self, library_name: str, server_id: int) -> Result:
        """执行绑定：将媒体库绑定到特定服务器 ID"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        binding = await self.config_repo.get_library_binding(library_name)

        # 切换服务器需重置具体配置
        if binding.server_id != server_id:
            binding.quality_profile_id = None
            binding.root_folder = None

        binding.server_id = server.id
        # arr_type 冗余字段，保持兼容或用于显示
        binding.arr_type = server.server_type

        await self.config_repo.set_library_binding(binding)
        return Result(True, f"已绑定到 **{server.name}**")

    async def get_quality_selection(self, library_name: str) -> Result:
        """获取质量配置文件选择的键盘布局。
        Args:
            library_name (str): 媒体库名称。
        Returns:
            Result: 包含键盘布局的结果对象。
        """
        binding = await self.config_repo.get_library_binding(library_name)
        if not binding.server_id:
            return Result(False, "未绑定实例")

        client = self.sonarr_clients.get(binding.server_id) or self.radarr_clients.get(binding.server_id)
        if not client:
            return Result(False, "实例未运行")

        try:
            profiles = await client.get_quality_profiles() or []
        except Exception as e:
            return Result(False, f"获取失败: {e}")

        lib_b64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        keyboard = []
        for p in profiles:
            keyboard.append([Button.inline(f"{p.name}", f"bind_set_quality_{p.id}_{lib_b64}".encode('utf-8'))])
        keyboard.append([Button.inline("« 返回", f"bind_lib_menu_{lib_b64}".encode('utf-8'))])

        msg = textwrap.dedent(f"""\
            **选择 {library_name} 的质量配置**
            请选择质量配置。
        """)
        return Result(True, msg, keyboard=keyboard)

    async def get_folder_selection(self, library_name: str) -> Result:
        """获取根文件夹选择的键盘布局。
        Args:
            library_name (str): 媒体库名称。
        Returns:
            Result: 包含键盘布局的结果对象.
        """
        binding = await self.config_repo.get_library_binding(library_name)
        if not binding.server_id:
            return Result(False, "未绑定实例")

        client = self.sonarr_clients.get(binding.server_id) or self.radarr_clients.get(binding.server_id)
        if not client:
            return Result(False, "实例未运行")

        try:
            folders = await client.get_root_folders() or []
        except Exception as e:
            return Result(False, f"获取失败: {e}")

        if not folders:
            return Result(False, "获取根目录为空")

        lib_b64 = base64.b64encode(library_name.encode('utf-8')).decode('utf-8')
        keyboard = []
        for f in folders:
            # path base64 编码防止特殊字符
            if f.path is None:
                continue
            # path_b64 = base64.b64encode(f.path.encode('utf-8')).decode('utf-8')
            keyboard.append([
                Button.inline(
                    f"{f.path} ({f.free_space_human})",
                    f"bind_set_folder_{f.id}_{lib_b64}".encode('utf-8')
                )])
        keyboard.append([Button.inline("« 返回", f"bind_lib_menu_{lib_b64}".encode('utf-8'))])

        msg = textwrap.dedent(f"""\
            **选择 {library_name} 的根目录**
            请选择根目录。
        """)
        return Result(True, msg, keyboard=keyboard)

    async def set_library_root_folder_by_id(self, library_name: str, folder_id: int) -> Result:
        """根据 Folder ID 查找并保存真实的 Path"""
        binding = await self.config_repo.get_library_binding(library_name)
        if not binding.server_id:
            return Result(False, "未绑定实例")

        client = self.sonarr_clients.get(binding.server_id) or self.radarr_clients.get(binding.server_id)
        if not client:
            return Result(False, "实例未运行")

        try:
            folders = await client.get_root_folders() or []
        except Exception as e:
            return Result(False, f"获取根目录失败，无法解析路径: {e}")

        target_folder = next((f for f in folders if f.id == folder_id), None)

        if not target_folder or not target_folder.path:
            return Result(False, "无效的根目录 ID，可能该目录已被移除。")

        binding.root_folder = target_folder.path
        await self.config_repo.set_library_binding(binding)

        return Result(True, f"已将媒体库 {library_name} 的 root_folder 设置为 `{target_folder.path}`。")

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
        points = "✅" if self.config_repo.cache.get(ConfigRepository.KEY_ENABLE_POINTS) == "true" else "❌"
        verify = "✅" if self.config_repo.cache.get(ConfigRepository.KEY_ENABLE_VERIFICATION) == "true" else "❌"
        request = "✅" if self.config_repo.cache.get(ConfigRepository.KEY_ENABLE_REQUESTMEDIA) == "true" else "❌"

        keyboard = [
            [Button.inline(f"积分/签到功能: {points}", f"toggle_system_{ConfigRepository.KEY_ENABLE_POINTS}".encode('utf-8'))],
            [Button.inline(f"入群验证: {verify}", f"toggle_system_{ConfigRepository.KEY_ENABLE_VERIFICATION}".encode('utf-8'))],
            [Button.inline(f"求片: {request}", f"toggle_system_{ConfigRepository.KEY_ENABLE_REQUESTMEDIA}".encode('utf-8'))],
            [Button.inline("« 返回主菜单", b"manage_main")]
        ]
        msg = textwrap.dedent("""\
            **⚙️ 系统功能开关**
            点击按钮以开启或关闭相应功能。
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

    async def toggle_server_nsfw(self, server_id: int) -> Result:
        """切换服务器 NSFW 开关"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        new_state = not server.nsfw_enabled
        await self.server_repo.update_nsfw_config(server_id, enabled=new_state)
        return Result(True, f"已{'开启' if new_state else '关闭'} NSFW 限制")

    async def get_nsfw_library_panel(self, server_id: int) -> Result:
        """获取 nsfw 媒体库设置面板"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        client = self.media_clients.get(server_id)
        if not client:
            return Result(False, "客户端未运行")

        libraries = await client.get_libraries() or []
        if not libraries:
            return Result(success=False, message="获取媒体库列表失败，请检查媒体服务器连接。")
        current_ids = server.nsfw_library_ids.split('|') if server.nsfw_library_ids else []

        keyboard = []
        for lib in libraries:
            # Emby 用 Guid, Jellyfin 用 ItemId
            lib_id = lib.ItemId if server.server_type == ServerType.JELLYFIN else lib.Guid
            if not lib_id:
                continue

            is_nsfw = lib_id in current_ids
            icon = "🔞" if is_nsfw else "🟢"
            lib_id_b64 = base64.b64encode(lib_id.encode()).decode()
            keyboard.append([
                Button.inline(f"{icon} {lib.Name}", f"srv_nsfw_setlib_{server.id}_{lib_id_b64}".encode())
            ])
        keyboard.append([Button.inline("« 返回", f"view_server_{server.id}".encode())])
        msg = textwrap.dedent("""\
            **NSFW 媒体库设置**
            点击按钮以将其标记为 NSFW 媒体库。
            标记为 NSFW 的媒体库将允许用户自行选择是否单独开启/关闭。
        """)

        return Result(True, msg, keyboard=keyboard)

    async def toggle_nsfw_library(self, server_id: int, lib_id: str) -> Result:
        """切换 nsfw 媒体库设置"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        client = self.media_clients.get(server_id)
        if not client:
            return Result(False, "客户端未运行")

        is_emby = server.server_type == ServerType.EMBY
        nsfw_ids = {i for i in server.nsfw_library_ids.split('|') if i} if server.nsfw_library_ids else set()
        nsfw_sub_ids = {i for i in server.nsfw_sub_library_ids.split('|') if i} if server.nsfw_sub_library_ids else set()

        sub_folders: list[LibraryMediaFolder] | None = None

        if is_emby:
            sub_folders = await client.get_selectable_media_folders()

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

        await self.server_repo.update_nsfw_config(server_id, lib_ids='|'.join(nsfw_ids))
        if is_emby:
            await self.server_repo.update_nsfw_config(server_id, sub_lib_ids='|'.join(nsfw_sub_ids))

        return Result(success=True, message=f"已{action}该媒体库。")

    async def get_registration_mode_panel(self, server_id: int) -> Result:
        """获取注册模式设置面板"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在。")

        # 当前状态描述
        mode = server.registration_mode
        desc = "未知"
        if mode == RegistrationMode.DEFAULT:
            desc = "默认为 **仅邀请码/积分兑换**，不开放直接注册。"
        elif mode == RegistrationMode.OPEN:
            desc = "当前 **完全开放**，任何人均可注册。"
        elif mode == RegistrationMode.CLOSE:
            desc = "当前 **完全关闭**，禁止任何形式注册。"
        elif mode == RegistrationMode.COUNT:
            desc = f"当前为 **名额限制**，剩余名额: `{server.registration_count_limit}`。"
        elif mode == RegistrationMode.TIME:
            try:
                dt = datetime.fromtimestamp(float(server.registration_time_limit))
                desc = f"当前为 **限时开放**，截止时间: `{dt.strftime('%Y-%m-%d %H:%M')}`。"
            except:
                desc = "限时配置错误。"
        elif mode == RegistrationMode.EXTERNAL:
            desc = f"当前为 **外部验证**，验证链接前缀: `{server.registration_external_url}`。"

        keyboard = [
            [
                Button.inline("🔒 默认(邀请/积分)", data=f"srv_set_mode_{server.id}_default".encode()),
                Button.inline("🔓 完全开放", data=f"srv_set_mode_{server.id}_open".encode())
            ],
            [
                Button.inline("🔢 设置名额限制", data=f"srv_input_mode_{server.id}_count".encode()),
                Button.inline("⏰ 设置限时开放", data=f"srv_input_mode_{server.id}_time".encode())
            ],
            [
                Button.inline("🌐 设置外部验证", data=f"srv_input_mode_{server.id}_external".encode())
            ],
            [
                Button.inline("🚫 完全关闭", data=f"srv_set_mode_{server.id}_close".encode())
            ],
            [Button.inline("« 返回服务器详情", data=f"view_server_{server.id}".encode())]
        ]

        msg = textwrap.dedent(f"""\
            **📝 注册模式配置 - {server.name}**
            
            {desc}
            
            请选择新的模式：
        """)
        return Result(True, msg, keyboard=keyboard)

    async def set_server_registration_mode(
        self,
        server_id: int,
        mode_input: str,
        external_parser: str | None = None
    ) -> Result:
        """设置服务器的注册模式 (包含正则解析逻辑)"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        # 纯数字 -> 名额模式
        if re.fullmatch(r'\d+', mode_input):
            count = int(mode_input)
            if count <= 0:
                return Result(False, "名额必须为正整数")
            await self.server_repo.update_policy_config(server.id, mode=RegistrationMode.COUNT, count=count)
            return Result(True, f"已设置为 **名额限制** 模式，剩余: {count}")

        # 时间格式 -> 限时模式 (1h30m)
        elif re.fullmatch(r'(\d+h)?(\d+m)?(\d+s)?', mode_input):
            hours = int((re.search(r'(\d+)h', mode_input) or [0,0])[1])
            minutes = int((re.search(r'(\d+)m', mode_input) or [0,0])[1])
            seconds = int((re.search(r'(\d+)s', mode_input) or [0,0])[1])

            if hours == 0 and minutes == 0 and seconds == 0:
                return Result(False, "时间格式无效")

            end_time = datetime.now() + timedelta(hours=hours, minutes=minutes, seconds=seconds)
            ts = str(end_time.timestamp())

            await self.server_repo.update_policy_config(server.id, mode=RegistrationMode.TIME, time=ts)
            return Result(True, f"已设置为 **限时开放**，截止: {end_time.strftime('%Y-%m-%d %H:%M')}")

        # http -> 外部验证模式
        elif mode_input.startswith("http"):
            await self.server_repo.update_policy_config(
                server.id,
                mode=RegistrationMode.EXTERNAL,
                external_url=mode_input,
                external_parser=external_parser
            )
            return Result(True, f"已设置为 **外部验证** 模式，链接: `{mode_input}`")

        # 关键字模式
        elif mode_input == RegistrationMode.DEFAULT:
            await self.server_repo.update_policy_config(server.id, mode=RegistrationMode.DEFAULT)
            return Result(True, "已恢复 **默认模式** (仅限邀请码/积分)。")

        elif mode_input == RegistrationMode.OPEN:
            await self.server_repo.update_policy_config(server.id, mode=RegistrationMode.OPEN)
            return Result(True, "已开启 **完全开放** 注册。")

        elif mode_input == RegistrationMode.CLOSE:
            await self.server_repo.update_policy_config(server.id, mode=RegistrationMode.CLOSE)
            return Result(True, "已 **完全关闭** 注册。")

        else:
            return Result(False, "无效的输入格式。")

    async def get_registration_expiry_panel(self, server_id: int) -> Result:
        """获取 账户有效期 设置面板"""
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        keyboard = [
            [Button.inline("一月(30 天)", f"srv_set_exp_{server_id}_30".encode())],
            [Button.inline("一季(90 天)", f"srv_set_exp_{server_id}_90".encode())],
            [Button.inline("一年(365 天)", f"srv_set_exp_{server_id}_365".encode())],
            [Button.inline("永久(9999 天)", f"srv_set_exp_{server_id}_9999".encode())],
            [Button.inline("« 返回", f"view_server_{server_id}".encode())]
        ]

        msg = textwrap.dedent(f"""\
            **账户有效期设置**
            注册和续期账户有效时长

            当前有效期 {server.registration_expiry_days} 天
        """)
        return Result(success=True, message=msg, keyboard=keyboard)

    async def set_registration_expiry(self, server_id: int, days: int) -> Result:
        """设置 账户有效期"""
        await self.server_repo.update_expiry_config(server_id, expiry_days=days)
        return Result(success=True, message=f"已设为 {days} 天")

    async def get_server_notify_topic_selection(self, server_id: int, notify_type: str) -> Result:
        """获取话题选择键盘"""
        topic_map = await self.client.get_topic_map()
        server = await self.server_repo.get_by_id(server_id)
        if not server:
            return Result(False, "服务器不存在")

        type_cn = "常规通知" if notify_type == 'normal' else "求片通知"

        keyboard = []
        msg = textwrap.dedent(f"""\
            **🔔 设置 {server.name} 的 {type_cn}**
            
            请选择消息发送的目标：
        """)

        # 遍历 map 生成按钮
        for tid, title in topic_map.items():
            keyboard.append([
                Button.inline(title, f"srv_save_topic_{server_id}_{notify_type}_{tid}".encode('utf-8'))
            ])

        keyboard.append([Button.inline("🚫 清除设置", f"srv_save_topic_{server_id}_{notify_type}_0".encode('utf-8'))])
        keyboard.append([Button.inline("« 返回", f"view_server_{server_id}".encode('utf-8'))])

        return Result(True, msg, keyboard=keyboard)

    async def set_server_notify_topic(self, server_id: int, notify_type: str, topic_id: int) -> Result:
        """保存话题设置"""
        target_id = topic_id if topic_id != 0 else None

        if notify_type == 'normal':
            await self.server_repo.update_notify_config(server_id, notify_topic_id=target_id)
        elif notify_type == 'request':
            await self.server_repo.update_notify_config(server_id, request_notify_topic_id=target_id)

        return Result(True, "设置已保存")

    async def add_server(self, name: str, server_type: str, url: str, api_key: str) -> Result:
        """添加新服务器并初始化客户端"""
        try:
            # 默认优先级设为 0
            instance = await self.server_repo.add(name, server_type, url, api_key, priority=0)
        except Exception as e:
            return Result(False, f"数据库添加失败 (可能名称重复): {str(e)}")

        try:
            new_client = None
            if server_type == ServerType.EMBY:
                new_client = EmbyClient(httpx.AsyncClient(base_url=f"{url}/emby"), api_key)
                self.media_clients[instance.id] = new_client
            elif server_type == ServerType.JELLYFIN:
                new_client = JellyfinClient(httpx.AsyncClient(base_url=url), api_key)
                self.media_clients[instance.id] = new_client
            elif server_type == ServerType.SONARR:
                new_client = SonarrClient(httpx.AsyncClient(base_url=url), api_key)
                self.sonarr_clients[instance.id] = new_client
            elif server_type == ServerType.RADARR:
                new_client = RadarrClient(httpx.AsyncClient(base_url=url), api_key)
                self.radarr_clients[instance.id] = new_client

            return Result(True, f"✅ 服务器 **{name}** 添加成功并已上线！")

        except Exception as e:
            # 初始化失败，回滚数据库
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
