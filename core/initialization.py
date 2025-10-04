import logging
import sqlite3
import sys
import textwrap

from sqlalchemy.ext.asyncio import AsyncSession

from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from repositories.telegram_repo import TelegramRepository

logger = logging.getLogger(__name__)

def check_sqlite_version():
    """检查SQLite版本，确保其支持所需的功能"""
    min_required_version = (3, 35, 0)  # 需要支持的最低版本
    version = sqlite3.sqlite_version_info
    if version < min_required_version:
        logger.error("SQLite 版本过低，当前版本为 %s。请升级到 3.35.0 或更高版本以支持所需功能。", sqlite3.sqlite_version)
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
        logger.info("SQLite 版本检查通过，当前版本为 %s。", sqlite3.sqlite_version)

async def initialize_admin(
    session: AsyncSession,
    telethon_client: TelethonClientWarper):
    """初始化管理员用户"""

    admin_ids = []
    sql_service = TelegramRepository(session)
    existing_admins = await sql_service.get_admins()
    if existing_admins:
        admin_ids = [admin.id for admin in existing_admins]
        logger.info("已存在管理员用户: %s", admin_ids)
        return admin_ids

    logger.warning("未找到管理员用户，正在初始化...")
    creator_id = await telethon_client.get_chat_creator_id()
    if creator_id:
        await sql_service.add_admin(creator_id)
        return [creator_id]
    else:
        logger.error("无法获取频道/群组创建者ID，请确保机器人已加入频道/群组并具有足够权限。")
        return []

async def initialize_bot_configuration(session: AsyncSession):
    """初始化Bot配置"""
    config_repo = ConfigRepository(session)


    registration_mode = await config_repo.get_settings('registration_mode')
    if not registration_mode:
        await config_repo.set_settings('registration_mode', 'default')
        await config_repo.set_settings('registration_count_limit', '0')
        await config_repo.set_settings('registration_time_limit', '0')
        logger.info("设置注册模式为 积分/注册码注册。")
    else:
        logger.info("注册模式已存在: %s", registration_mode)

    code_expiry_days = await config_repo.get_settings('code_expiry_days')
    if not code_expiry_days:
        await config_repo.set_settings('code_expiry_days', '30')
        logger.info("已设置用户生成注册码/激活码过期时间为30天。")
    else:
        logger.info("激活码过期时间已存在: %s 天", code_expiry_days)

    nsfw_library = await config_repo.get_settings('nsfw_library')
    if not nsfw_library:
        await config_repo.set_settings('nsfw_library', 'Japan|Hentai|Erotica|Adult')

    # 默认开启 NSFW 过滤（注册用户时生效）
    nsfw_enabled = await config_repo.get_settings('nsfw_enabled')
    if not nsfw_enabled:
        await config_repo.set_settings('nsfw_enabled', 'true')
        logger.info("已设置 NSFW 过滤为开启。")

    return True
