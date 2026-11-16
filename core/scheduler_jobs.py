from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from httpx2 import HTTPError, HTTPStatusError
from loguru import logger
from telethon import errors

from core.config import get_settings
from core.database import async_session, backup_database
from core.telegram_manager import TelethonClientWarper
from core.telegram_message import TelegramMessage
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


def _split_hhmm(hhmm: str) -> tuple[int, int]:
    """将 ``HH:MM`` 字符串拆分为 ``(hour, minute)`` 整数元组。"""
    hour_str, minute_str = hhmm.split(":")
    return int(hour_str), int(minute_str)


def get_ops_schedule_overrides(
    ops_cfg: dict[str, Any] | None = None,
) -> dict[str, tuple[str, dict[str, Any]]]:
    """根据运维配置生成各定时任务的触发器类型与参数映射。"""
    cfg = ops_cfg or ConfigRepository.get_ops_config_from_cache()
    cron_map = {
        "ban_expired_users": (
            cfg["schedule_ban_expired_day"],
            cfg["schedule_ban_expired_time"],
        ),
        "delete_expired_banned_users": (
            cfg["schedule_delete_banned_day"],
            cfg["schedule_delete_banned_time"],
        ),
        "cleanup_inactive_users": (
            cfg["schedule_cleanup_inactive_day"],
            cfg["schedule_cleanup_inactive_time"],
        ),
        "settle_scores": (
            cfg["schedule_settle_scores_day"],
            cfg["schedule_settle_scores_time"],
        ),
        "cleanup_api_cache": (
            cfg["schedule_cleanup_cache_day"],
            cfg["schedule_cleanup_cache_time"],
        ),
        "auto_backup_db": (
            cfg["schedule_backup_db_day"],
            cfg["schedule_backup_db_time"],
        ),
    }
    overrides: dict[str, tuple[str, dict[str, Any]]] = {}
    for job_id, (day_expr, hhmm) in cron_map.items():
        hour, minute = _split_hhmm(hhmm)
        overrides[job_id] = ("cron", {"day": day_expr, "hour": hour, "minute": minute})
    overrides["expire_pending_media_requests"] = (
        "interval",
        {"hours": int(cfg["schedule_request_expire_hours"])},
    )
    overrides["retry_pending_nsfw_revokes"] = (
        "interval",
        {"minutes": int(cfg["schedule_nsfw_retry_minutes"])},
    )
    return overrides


def apply_ops_schedules(scheduler, ops_cfg: dict[str, Any] | None = None) -> None:
    """将当前运维定时配置热应用到 APScheduler 调度器实例。"""
    if scheduler is None:
        return
    overrides = get_ops_schedule_overrides(ops_cfg)
    for job_id, (trigger, trigger_args) in overrides.items():
        if scheduler.get_job(job_id) is not None:
            scheduler.reschedule_job(job_id, trigger=trigger, **trigger_args)


@scheduled_job("cron", hour=0, minute=15, id="ban_expired_users", replace_existing=True)
async def ban_expired_users() -> None:
    """封禁过期媒体账号
    检查所有过期媒体账号的订阅状态，封禁前先复核是否已开启自动保活且时长达标；
    未达标或未开启时，在账号串行锁内二次核验最新到期状态，随后先在媒体服务器上禁用已过期账号，
    再标记本地封禁状态并设置宽限期后的删除时间。
    """
    from main import app  # 避免循环导入
    from services.account_service import AccountService
    from services.watch_time_service import get_account_lock

    async with async_session() as session:
        try:
            media_repo = MediaRepository(session)
            account_service = AccountService(app, session)
            media_clients: dict[int, MediaService] = app.state.media_clients

            # 兼容历史数据：为已封禁但未设置删除时间的过期账户补全删除时间
            await media_repo.backfill_missing_delete_at()

            users = await media_repo.find_expired_for_ban()
            if not users:
                logger.info("没有需要封禁的用户。")
                return
            target_accounts = [(u.id, u.server_id, u.media_id) for u in users]
            # 释放列表扫描的只读快照，保证每个账号进入锁后都能读取到最新并发提交
            await media_repo.end_clean_read_tx()

            for user_id, server_id, media_id in target_accounts:
                # 到期封禁前复核自动保活资格：仅用户手动开启且时长达标时免分抢救，绝不自动扣分
                auto_outcome = await account_service.try_auto_renew_account(
                    user_id, server_id
                )
                if auto_outcome.renewed:
                    logger.info(
                        "到期封禁前复核触发自动保活成功，跳过封禁: {} (ID: {}) Server: {}",
                        user_id,
                        media_id,
                        server_id,
                    )
                    continue
                if auto_outcome.retryable_error:
                    logger.warning(
                        "自动保活账号外部核验临时失败，本次跳过封禁等待重试: {} (ID: {}) Server: {}",
                        user_id,
                        media_id,
                        server_id,
                    )
                    continue

                async with get_account_lock(user_id, server_id):
                    await media_repo.end_clean_read_tx()
                    fresh_user = await media_repo.get_by_id(
                        user_id, server_id, fresh=True
                    )
                    if (
                        fresh_user is None
                        or fresh_user.is_banned
                        or fresh_user.expires_at >= datetime.now()
                    ):
                        logger.info(
                            "媒体账号状态已变更或并发完成续期，跳过本次封禁: {} (ID: {}) Server: {}",
                            user_id,
                            media_id,
                            server_id,
                        )
                        continue

                    client = media_clients.get(fresh_user.server_id)
                    if not client:
                        logger.warning(
                            "未找到服务器实例(ID: {})，跳过封禁用户: {} (ID: {})",
                            fresh_user.server_id,
                            fresh_user.id,
                            fresh_user.media_id,
                        )
                        continue
                    try:
                        logger.info(
                            "封禁用户: {} (ID: {}) Server: {}",
                            fresh_user.id,
                            fresh_user.media_id,
                            fresh_user.server_id,
                        )
                        await client.ban_or_unban(
                            user_id=fresh_user.media_id, is_ban=True
                        )
                    except HTTPStatusError as e:
                        if e.response.status_code == 404:
                            # 媒体服务器账户已不存在时，仍标记封禁并按到期时间+7天进入待删除队列
                            logger.warning(
                                "封禁用户时媒体服务器账户不存在(404)，标记待删除: {} (ID: {})",
                                fresh_user.id,
                                fresh_user.media_id,
                            )
                            await media_repo.mark_banned(fresh_user)
                        else:
                            logger.error(
                                "封禁过期用户失败，保留未封禁状态以便重试: {} (ID: {})，状态码: {}",
                                fresh_user.id,
                                fresh_user.media_id,
                                e.response.status_code,
                            )
                        continue
                    except (HTTPError, ValueError) as e:
                        logger.error(
                            "封禁过期用户失败，保留未封禁状态以便重试: {} (ID: {})，错误: {}",
                            fresh_user.id,
                            fresh_user.media_id,
                            e,
                        )
                        continue

                    await media_repo.mark_banned(fresh_user)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("封禁过期用户时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


# 在 Issue 09 引入每账号单次 add_job（pre_expiry_check/expiry_ban）前，该周期扫描承担临近到期窗口自动保活，
# 并在 Issue 09 上线后降级保留为停机恢复兜底巡检（遵循 ADR-0003）
@scheduled_job(
    "interval",
    minutes=30,
    id="auto_renew_watch_time_users",
    replace_existing=True,
)
async def auto_renew_watch_time_users() -> None:
    """周期检查临近到期窗口内开启自动保活且时长达标的媒体账号并自动免分续期"""
    from main import app  # 避免循环导入
    from services.account_service import AccountService

    async with async_session() as session:
        try:
            account_service = AccountService(app, session)
            renewed_count = await account_service.process_auto_renewals()
            if renewed_count > 0:
                logger.info("本次自动保活续期完成账号数: {}", renewed_count)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("执行自动保活续期任务时出错: {}", e)
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
        except Exception as e:  # pylint: disable=broad-exception-caught
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
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("重试 NSFW 撤权任务时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


@scheduled_job("cron", hour=23, minute=0, id="settle_scores", replace_existing=True)
async def settle_scores() -> None:
    """定时结算用户群聊发言积分，受总开关与群聊发言积分子开关共同控制。

    副作用：放行时按队列批量发放当日发言积分并广播榜单；关闭时清空待结算队列且不发送结算报告。
    """
    from main import app  # 避免循环导入

    async with async_session() as session:
        try:
            score_service = ScoreService(session, app.state.message_tracker)
            # 优先通过数据库/缓存统一校验，并在关闭时清理历史暂存计数以防日后误发
            if not await score_service.config_repo.is_points_feature_enabled(
                ConfigRepository.KEY_ENABLE_POINTS_CHAT
            ):
                app.state.message_tracker.message_counts.clear()
                return

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
                    TelegramMessage.render_markdown(
                        t"{medal} [{user_name}](tg://user?id={user_id}) — `+{score_change}`"
                    )
                )

            # 构建总榜单
            top_users = await score_service.telegram_repo.get_top_users(10)

            total_lines = ["🏆 **积分总榜 (Top 10)**"]
            for idx, user in enumerate(top_users, 1):
                try:
                    user_name = await client.get_user_name(user.id)
                    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
                    total_lines.append(
                        TelegramMessage.render_markdown(
                            t"{medal} [{user_name}](tg://user?id={user.id}) — `{user.score}`"
                        )
                    )
                except (errors.RPCError, ValueError) as e:
                    logger.warning("获取{}的名称失败: {}", user.id, e)
                    total_lines.append(f"{idx}. `Unknown` — `{user.score}`")

            # 合并消息
            msg = TelegramMessage.render_markdown(t"""\
                📊 **每日积分结算报告**

                今日共发放 **{result.total_score_settled}** 非签到活跃积分。

                """)
            msg += "\n".join(daily_lines) + "\n\n"
            msg += "\n".join(total_lines)

            await client.send_message(settings.telegram_chat_id, msg)

        except Exception as e:  # pylint: disable=broad-exception-caught
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
        except Exception as e:  # pylint: disable=broad-exception-caught
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


@scheduled_job(
    "interval",
    hours=1,
    id="expire_pending_media_requests",
    replace_existing=True,
)
async def expire_pending_media_requests() -> None:
    """定时清理超过 7 天未审批的求片请求并释放全部预留积分"""
    from main import app  # 避免循环导入
    from services.request_service import RequestService

    async with async_session() as session:
        try:
            service = RequestService(app, session)
            expired_count = await service.expire_pending_requests()
            if expired_count > 0:
                logger.info("本次自动取消超时求片数量: {}", expired_count)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("清理超时求片任务出错: {}", e)
            await session.rollback()
        finally:
            await session.close()
