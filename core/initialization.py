import sqlite3
import sys
import textwrap

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from core.telegram_manager import TelethonClientWarper
from repositories.telegram_repo import TelegramRepository


def check_sqlite_version():
    """检查SQLite版本，确保其支持所需的功能"""
    min_required_version = (3, 35, 0)  # 需要支持的最低版本
    version = sqlite3.sqlite_version_info
    if version < min_required_version:
        logger.error("SQLite 版本过低，当前版本为 {}。请升级到 3.35.0 或更高版本以支持所需功能。", sqlite3.sqlite_version)
        sys.exit(textwrap.dedent(f"""\
            ==================================================================
            |                      错误提示                       |
            ==================================================================
            | SQLite 版本过低，当前版本为 {sqlite3.sqlite_version}。
            | 请升级到 3.35.0 或更高版本以支持所需功能。
            | 你可以访问 https://www.sqlite.org/download.html 下载最新版本。
            | 如果你使用的是系统自带的 SQLite，请参考以下命令进行升级：
            | - 对于 Debian/Ubuntu 系统：
            |     sudo apt-get update
            |     sudo apt-get install sqlite3
            | - 对于 CentOS/RHEL 系统：
            |     sudo yum update
            |     sudo yum install sqlite3
            | - 对于 macOS 系统（使用 Homebrew）：
            |     brew update
            |     brew upgrade sqlite3
            ==================================================================
            """))
    else:
        logger.info("SQLite 版本检查通过，当前版本为 {}。", sqlite3.sqlite_version)

async def initialize_admin(session: AsyncSession, telethon_client: TelethonClientWarper):
    """初始化管理员用户"""

    sql_service = TelegramRepository(session)
    existing_admins = await sql_service.get_admins()
    if existing_admins:
        logger.info("已存在管理员用户: {}", existing_admins)
        return existing_admins

    logger.warning("未找到管理员用户，正在初始化...")
    creator_id = await telethon_client.get_chat_creator_id()
    if creator_id:
        await sql_service.toggle_admin(creator_id, is_admin=True)
        return [creator_id]

    logger.error("无法获取频道/群组创建者ID，请确保机器人已加入频道/群组并具有足够权限。")
    return []
