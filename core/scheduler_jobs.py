import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from httpx2 import HTTPError, HTTPStatusError
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


@scheduled_job("cron", hour=0, minute=15, id="ban_expired_users", replace_existing=True)
async def ban_expired_users() -> None:
    """封禁过期用户
    检查所有用户的订阅状态，先在媒体服务器上禁用已过期账户，再标记本地封禁状态并设置7天宽限期后的删除时间。
    """
    from main import app  # 避免循环导入

    async with async_session() as session:
        try:
            media_repo = MediaRepository(session)
            media_clients: dict[int, MediaService] = app.state.media_clients

            # 兼容历史数据：为已封禁但未设置删除时间的过期账户补全删除时间
            await media_repo.backfill_missing_delete_at()

            users = await media_repo.find_expired_for_ban()
            if not users:
                logger.info("没有需要封禁的用户。")
                return

            for user in users:
                client = media_clients.get(user.server_id)
                if not client:
                    logger.warning(
                        "未找到服务器实例(ID: {})，跳过封禁用户: {} (ID: {})",
                        user.server_id,
                        user.id,
                        user.media_id,
                    )
                    continue
                try:
                    logger.info(
                        "封禁用户: {} (ID: {}) Server: {}",
                        user.id,
                        user.media_id,
                        user.server_id,
                    )
                    await client.ban_or_unban(user_id=user.media_id, is_ban=True)
                except HTTPStatusError as e:
                    if e.response.status_code == 404:
                        # 媒体服务器账户已不存在时，仍标记封禁并按到期时间+7天进入待删除队列
                        logger.warning(
                            "封禁用户时媒体服务器账户不存在(404)，标记待删除: {} (ID: {})",
                            user.id,
                            user.media_id,
                        )
                        await media_repo.mark_banned(user)
                    else:
                        logger.error(
                            "封禁过期用户失败，保留未封禁状态以便重试: {} (ID: {})，状态码: {}",
                            user.id,
                            user.media_id,
                            e.response.status_code,
                        )
                    continue
                except Exception as e:
                    logger.error(
                        "封禁过期用户失败，保留未封禁状态以便重试: {} (ID: {})，错误: {}",
                        user.id,
                        user.media_id,
                        e,
                    )
                    continue

                await media_repo.mark_banned(user)
        except Exception as e:
            logger.exception("封禁过期用户时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


@scheduled_job(
    "cron", hour=0, minute=30, id="delete_expired_banned_users", replace_existing=True
)
async def delete_expired_banned_users() -> None:
    """删除已封禁且过期的用户
    对到期满七天仍未续期的账户，先删除媒体服务器账户，确认已删除或已不存在后再移除本地记录。
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
                    logger.warning(
                        "未找到服务器实例(ID: {})，保留本地记录以便重试: {} (ID: {})",
                        user.server_id,
                        user.id,
                        user.media_id,
                    )
                    continue
                try:
                    await client.delete_user(user.media_id)
                except HTTPStatusError as e:
                    if e.response.status_code == 404:
                        logger.info(
                            "媒体服务器账户已不存在(404)，清理本地记录: {} (ID: {})",
                            user.id,
                            user.media_id,
                        )
                    else:
                        logger.error(
                            "删除封禁用户失败，保留本地记录以便重试: {} (ID: {})，状态码: {}",
                            user.id,
                            user.media_id,
                            e.response.status_code,
                        )
                        continue
                except (HTTPError, Exception) as e:
                    logger.error(
                        "删除封禁用户失败，保留本地记录以便重试: {} (ID: {})，错误: {}",
                        user.id,
                        user.media_id,
                        e,
                    )
                    continue

                await media_repo.delete(user)
        except Exception as e:
            logger.exception("删除封禁用户时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


@scheduled_job(
    "interval", minutes=10, id="retry_pending_nsfw_revokes", replace_existing=True
)
async def retry_pending_nsfw_revokes() -> None:
    """重试此前因外部服务器不可用而未完成的 NSFW 撤权任务。"""
    from main import app  # 避免循环导入
    from services.account_service import AccountService

    pending: dict[int, set[str]] | None = getattr(
        app.state, "_pending_nsfw_revokes", None
    )
    if not pending:
        return

    async with async_session() as session:
        try:
            account_service = AccountService(app, session)
            for server_id, media_ids in list(pending.items()):
                if not media_ids:
                    pending.pop(server_id, None)
                    continue
                revoked, failed = await account_service.revoke_server_nsfw(
                    server_id, list(media_ids)
                )
                logger.info(
                    "重试 NSFW 撤权完成: server_id={}, 成功={}, 仍待重试={}",
                    server_id,
                    revoked,
                    failed,
                )
        except Exception as e:
            logger.exception("重试 NSFW 撤权任务时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


@scheduled_job("cron", hour=23, minute=0, id="settle_scores", replace_existing=True)
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
                await client.send_message(
                    settings.telegram_chat_id,
                    "📊 **每日积分结算报告**\n\n今日无活跃积分变动。",
                )
                return

            # 构建今日榜单 (按 increment 倒序)
            sorted_changes = sorted(
                result.user_score_changes.items(), key=lambda x: x[1], reverse=True
            )[:10]

            daily_lines = ["📈 **今日积分飙升榜 (Top 10)**"]
            for idx, (user_id, score_change) in enumerate(sorted_changes, 1):
                user_name = await client.get_user_name(user_id)
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
                daily_lines.append(
                    f"{medal} [{user_name}](tg://user?id={user_id}) — `+{score_change}`"
                )

            # 构建总榜单
            top_users = await score_service.telegram_repo.get_top_users(10)

            total_lines = ["🏆 **积分总榜 (Top 10)**"]
            for idx, user in enumerate(top_users, 1):
                try:
                    user_name = await client.get_user_name(user.id)
                    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
                    total_lines.append(
                        f"{medal} [{user_name}](tg://user?id={user.id}) — `{user.score}`"
                    )
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


@scheduled_job(
    "cron", hour=1, minute=0, id="cleanup_inactive_users", replace_existing=True
)
async def cleanup_inactive_users() -> None:
    """清理不在群组内的成员账户和数据"""
    if (
        ConfigRepository.cache.get(
            ConfigRepository.KEY_ENABLE_CLEANUP_INACTIVE_USERS, "false"
        )
        != "true"
    ):
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
                    result = await user_service.delete_account(user.id, "both")
                    if result.success:
                        logger.info("用户 {} 清理成功: {}", user.id, result.message)
                    else:
                        logger.error("用户 {} 清理失败: {}", user.id, result.message)
        except Exception as e:
            logger.exception("清理非群组成员任务出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


@scheduled_job("cron", hour=3, minute=0, id="cleanup_api_cache", replace_existing=True)
async def cleanup_api_cache_task() -> None:
    """清理过期的 API 缓存"""
    await CacheService.cleanup_expired()


@scheduled_job("cron", hour=4, minute=0, id="auto_backup_db", replace_existing=True)
async def auto_backup_db() -> None:
    """自动备份数据库"""
    await backup_database()
