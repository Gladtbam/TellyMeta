from collections.abc import Callable
from dataclasses import dataclass
import textwrap
from typing import Any

from httpx import HTTPError
from loguru import logger

from core.config import get_settings
from core.database import async_session, backup_database
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
from repositories.telegram_repo import TelegramRepository
from services.cache_service import CacheService
from services.media_service import MediaService
from services.score_service import ScoreService
from services.user_service import UserService

settings = get_settings()

@dataclass
class JobConfig:
    func: Callable
    trigger: str
    kwargs: dict[str, Any]

SCHEDULER_JOBS_REGISTRY: list[JobConfig] = []

def scheduled_job(trigger: str, **kwargs):
    """
    定时任务注册装饰器
    用法: @scheduled_job('cron', hour=0, minute=15, id='xxx')
    """
    def decorator(func: Callable):
        SCHEDULER_JOBS_REGISTRY.append(
            JobConfig(func=func, trigger=trigger, kwargs=kwargs)
        )
        return func
    return decorator

@scheduled_job('cron', hour=0, minute=15, id='ban_expired_users', replace_existing=True)
async def ban_expired_users() -> None:
    """封禁过期用户
    检查所有用户的订阅状态，封禁那些订阅已过期的用户。
    """
    from main import app  # 避免循环导入
    async with async_session() as session:
        try:
            media_repo = MediaRepository(session)
            media_clients: dict[int, MediaService] = app.state.media_clients

            users = await media_repo.find_expired_for_ban()
            if not users:
                logger.info("没有需要封禁的用户。")
                return

            for user in users:
                client = media_clients.get(user.server_id)
                if not client:
                    logger.warning("未找到服务器实例(ID: {})，跳过封禁用户: {} (ID: {})", user.server_id, user.id, user.media_id)
                    continue
                logger.info("封禁用户: {} (ID: {}) Server: {}", user.id, user.media_id, user.server_id)
                await client.ban_or_unban(
                    user_id=user.media_id,
                    is_ban=True
                )
        except Exception as e:
            logger.exception("封禁过期用户时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()

@scheduled_job('cron', hour=0, minute=30, id='delete_expired_banned_users', replace_existing=True)
async def delete_expired_banned_users() -> None:
    """删除已封禁且过期的用户
    删除那些已经被封禁且订阅过期的用户，以释放系统资源。
    """
    from main import app  # 避免循环导入
    async with async_session() as session:
        try:
            media_repo = MediaRepository(session)
            media_clients: dict[int, MediaService] = app.state.media_clients

            users = await media_repo.find_ban()
            if not users:
                logger.info("没有需要删除的封禁用户。")
                return

            for user in users:
                client = media_clients.get(user.server_id)
                if not client:
                    logger.warning("未找到服务器实例(ID: {})，仅清理数据库记录: {} (ID: {})", user.server_id, user.id, user.media_id)
                    continue
                try:
                    await client.delete_user(user.media_id)
                except HTTPError:
                    logger.error("删除封禁用户失败: {} (ID: {})", user.id, user.media_id)
        except Exception as e:
            logger.exception("删除封禁用户时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()

@scheduled_job('cron', hour=23, minute=0, id='settle_scores', replace_existing=True)
async def settle_scores() -> None:
    """结算用户积分"""
    if ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_POINTS, "true") != "true":
        return

    from main import app  # 避免循环导入
    async with async_session() as session:
        try:
            score_service = ScoreService(session, app.state.message_tracker)
            result = await score_service.settle_and_clear_scores()
            client: TelethonClientWarper = app.state.telethon_client

            if result is None:
                await client.send_message(settings.telegram_chat_id, "📊 **每日积分结算报告**\n\n今日无活跃积分变动。")
                return

            # 构建今日榜单 (按 increment 倒序)
            sorted_changes = sorted(result.user_score_changes.items(), key=lambda x: x[1], reverse=True)[:10]

            daily_lines = ["📈 **今日积分飙升榜 (Top 10)**"]
            for idx, (user_id, score_change) in enumerate(sorted_changes, 1):
                user_name = await client.get_user_name(user_id)
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
                daily_lines.append(f"{medal} [{user_name}](tg://user?id={user_id}) — `+{score_change}`")

            # 构建总榜单
            top_users = await score_service.telegram_repo.get_top_users(10)

            total_lines = ["🏆 **积分总榜 (Top 10)**"]
            for idx, user in enumerate(top_users, 1):
                try:
                    user_name = await client.get_user_name(user.id)
                    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
                    total_lines.append(f"{medal} [{user_name}](tg://user?id={user.id}) — `{user.score}`")
                except Exception as e:
                    logger.warning("获取{}的名称失败: {}", user.id, e)
                    total_lines.append(f"{idx}. `Unknown` — `{user.score}`")

            # 合并消息
            msg = textwrap.dedent(f"""\
                📊 **每日积分结算报告**
                
                今日共发放 **{result.total_score_settled}** 非签到活跃积分。
                
                """)
            msg += "\n".join(daily_lines) + "\n\n"
            msg += "\n".join(total_lines)

            await client.send_message(settings.telegram_chat_id, msg)

        except Exception as e:
            logger.exception("结算用户积分时出错: {}", e)
            await session.rollback()
            await session.close()

@scheduled_job('cron', hour=1, minute=0, id='cleanup_inactive_users', replace_existing=True)
async def cleanup_inactive_users() -> None:
    """清理不在群组内的成员账户和数据"""
    if ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_CLEANUP_INACTIVE_USERS, "false") != "true":
        return

    from main import app  # 避免循环导入

    async with async_session() as session:
        try:
            telegram_repo = TelegramRepository(session)
            user_service = UserService(app, session)
            client: TelethonClientWarper = app.state.telethon_client

            users = await telegram_repo.get_all_users()
            logger.info("开始检查非群组成员清理任务，当前总用户数: {}", len(users))

            for user in users:
                if user.is_admin:
                    continue

                participant = await client.get_participant(user.id)
                if not participant:
                    logger.info("用户 {} 不在群组中，开始清理...", user.id)
                    result = await user_service.delete_account(user.id, 'both')
                    if result.success:
                        logger.info("用户 {} 清理成功: {}", user.id, result.message)
                    else:
                        logger.error("用户 {} 清理失败: {}", user.id, result.message)
        except Exception as e:
            logger.exception("清理非群组成员任务出错: {}", e)
            await session.rollback()
        finally:
            await session.close()

@scheduled_job('cron', hour=3, minute=0, id='cleanup_api_cache', replace_existing=True)
async def cleanup_api_cache_task() -> None:
    """清理过期的 API 缓存"""
    await CacheService.cleanup_expired()

@scheduled_job('cron', hour=4, minute=0, id='auto_backup_db', replace_existing=True)
async def auto_backup_db() -> None:
    """自动备份数据库"""
    await backup_database()
