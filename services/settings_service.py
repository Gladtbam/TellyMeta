import textwrap
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import Button
from telethon.tl.types import ForumTopicDeleted

from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from repositories.telegram_repo import TelegramRepository
from services.user_service import Result

settings = get_settings()

class SettingsServices:
    def __init__(self, session: AsyncSession, app: FastAPI ) -> None:
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
            if user_id in await self.telegram_repo.get_admins():
                await self.telegram_repo.toggle_admin(user_id, is_admin=False)
                return Result(success=True, message=f"已撤销用户 {user_id} 的管理员权限。")
            else:
                await self.telegram_repo.toggle_admin(user_id, is_admin=True)
                return Result(success=True, message=f"已授予用户 {user_id} 管理员权限。")
        except ValueError as e:
            return Result(success=False, message=str(e))
