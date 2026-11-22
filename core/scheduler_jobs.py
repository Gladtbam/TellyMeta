from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.date import DateTrigger
from loguru import logger
from telethon import errors

from core.config import get_settings
from core.database import async_session, backup_database
from core.telegram_manager import TelethonClientWarper
from core.telegram_message import TelegramMessage
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
from repositories.telegram_repo import TelegramRepository

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from fastapi import FastAPI

    from models.orm import MediaUser

settings = get_settings()

PRE_EXPIRY_WINDOW = timedelta(hours=24)
TRAFFIC_RESET_JOB_PREFIX = "traffic_reset_"
TRAFFIC_UNSUSPEND_JOB_PREFIX = "traffic_unsuspend_"
PRE_EXPIRY_CHECK_JOB_PREFIX = "pre_expiry_check_"
EXPIRY_BAN_JOB_PREFIX = "expiry_ban_"
GRACE_DELETE_JOB_PREFIX = "grace_delete_"


def traffic_reset_job_id(user_id: int, server_id: int) -> str:
    """生成媒体账号账期重置单次任务 ID。"""
    return f"{TRAFFIC_RESET_JOB_PREFIX}{user_id}_{server_id}"


def traffic_unsuspend_job_id(user_id: int, server_id: int) -> str:
    """生成账期重置后超流停用延迟解封重试单次任务 ID。"""
    return f"{TRAFFIC_UNSUSPEND_JOB_PREFIX}{user_id}_{server_id}"


def pre_expiry_check_job_id(user_id: int, server_id: int) -> str:
    """生成媒体账号临近到期检查/提醒单次任务 ID。"""
    return f"{PRE_EXPIRY_CHECK_JOB_PREFIX}{user_id}_{server_id}"


def expiry_ban_job_id(user_id: int, server_id: int) -> str:
    """生成媒体账号到期封禁单次任务 ID。"""
    return f"{EXPIRY_BAN_JOB_PREFIX}{user_id}_{server_id}"


def grace_delete_job_id(user_id: int, server_id: int) -> str:
    """生成媒体账号宽限期满删号单次任务 ID。"""
    return f"{GRACE_DELETE_JOB_PREFIX}{user_id}_{server_id}"


def parse_lifecycle_job_account(job_id: str) -> tuple[int, int] | None:
    """从生命周期单次任务 ID 解析 ``(user_id, server_id)``；非生命周期任务返回 ``None``。"""
    for prefix in (
        TRAFFIC_RESET_JOB_PREFIX,
        TRAFFIC_UNSUSPEND_JOB_PREFIX,
        PRE_EXPIRY_CHECK_JOB_PREFIX,
        EXPIRY_BAN_JOB_PREFIX,
        GRACE_DELETE_JOB_PREFIX,
    ):
        if job_id.startswith(prefix):
            rest = job_id[len(prefix) :]
            uid_str, sep, sid_str = rest.partition("_")
            if not sep:
                return None
            try:
                return int(uid_str), int(sid_str)
            except ValueError:
                return None
    return None


def _remove_job_if_exists(
    scheduler: AsyncIOScheduler | Any | None, job_id: str
) -> None:
    """安全移除调度器中的指定任务，任务不存在时静默忽略。"""
    if scheduler is None:
        return
    with contextlib.suppress(JobLookupError, Exception):
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)


def clear_traffic_unsuspend_job(
    scheduler: AsyncIOScheduler | Any | None,
    user_id: int,
    server_id: int,
) -> None:
    """清理指定媒体账号待执行的超流停用延迟解封重试任务。"""
    _remove_job_if_exists(scheduler, traffic_unsuspend_job_id(user_id, server_id))


def schedule_traffic_unsuspend_retry(
    scheduler: AsyncIOScheduler | Any | None,
    user_id: int,
    server_id: int,
    *,
    run_date: datetime,
) -> None:
    """在指定时间节点调度一次超流停用延迟解封重试任务。"""
    if scheduler is None:
        return
    _add_or_replace_date_job(
        scheduler,
        run_account_traffic_unsuspend,
        run_date=run_date,
        user_id=user_id,
        server_id=server_id,
        job_id=traffic_unsuspend_job_id(user_id, server_id),
    )


def remove_account_lifecycle_jobs(
    scheduler: AsyncIOScheduler | Any | None,
    user_id: int,
    server_id: int,
) -> None:
    """移除指定媒体账号关联的全部生命周期单次调度任务。

    Args:
        scheduler: APScheduler 调度器实例；为 ``None`` 时安全跳过。
        user_id: Telegram 用户 ID。
        server_id: 媒体服务器 ID。
    """
    if scheduler is None:
        return
    for job_id in (
        traffic_reset_job_id(user_id, server_id),
        traffic_unsuspend_job_id(user_id, server_id),
        pre_expiry_check_job_id(user_id, server_id),
        expiry_ban_job_id(user_id, server_id),
        grace_delete_job_id(user_id, server_id),
    ):
        _remove_job_if_exists(scheduler, job_id)


def remove_server_lifecycle_jobs(
    scheduler: AsyncIOScheduler | Any | None,
    server_id: int,
) -> None:
    """清理指定服务器上所有媒体账号的生命周期单次调度任务。

    Args:
        scheduler: APScheduler 调度器实例；为 ``None`` 时安全跳过。
        server_id: 目标服务器 ID。
    """
    if scheduler is None:
        return
    for job in list(scheduler.get_jobs()):
        job_id = getattr(job, "id", "")
        if not isinstance(job_id, str):
            continue
        parsed = parse_lifecycle_job_account(job_id)
        if parsed is not None and parsed[1] == server_id:
            _remove_job_if_exists(scheduler, job_id)


def _add_or_replace_date_job(
    scheduler: AsyncIOScheduler | Any,
    func: Callable[[int, int], Any],
    *,
    run_date: datetime,
    user_id: int,
    server_id: int,
    job_id: str,
) -> None:
    """注册或覆盖持久化单次定时任务，仅传递可序列化顶层函数与纯标量参数。"""
    # 确保 DateTrigger 在模块加载阶段常驻 sys.modules，避免测试局部 mock sys.modules 后类对象漂移导致 Pickle 失败
    _ = DateTrigger
    scheduler.add_job(
        func,
        "date",
        run_date=run_date,
        args=[int(user_id), int(server_id)],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=None,
    )


def schedule_account_lifecycle_jobs(
    scheduler: AsyncIOScheduler | Any | None,
    media_user: MediaUser,
    *,
    now: datetime | None = None,
    pre_expiry_window: timedelta = PRE_EXPIRY_WINDOW,
) -> None:
    """按媒体账号最新状态注册或更新生命周期精准调度单次节点任务。

    任务函数均为顶层可序列化函数且仅传递纯标量 ``(user_id, server_id)`` 参数，
    以兼容 ``SQLAlchemyJobStore`` 跨进程重启持久化。

    Args:
        scheduler: APScheduler 调度器实例；为 ``None`` 时直接跳过。
        media_user: 最新的媒体账号 ORM 对象。
        now: 可选当前时间（用于确定性测试及临近窗口判断）。
        pre_expiry_window: 临近到期提前窗口（默认 24 小时）。
    """
    if scheduler is None:
        return

    current_now = now or datetime.now()
    uid = int(media_user.id)
    sid = int(media_user.server_id)

    if media_user.next_reset_at is not None:
        _add_or_replace_date_job(
            scheduler,
            run_account_traffic_reset,
            run_date=media_user.next_reset_at,
            user_id=uid,
            server_id=sid,
            job_id=traffic_reset_job_id(uid, sid),
        )

    if media_user.is_banned:
        _remove_job_if_exists(scheduler, pre_expiry_check_job_id(uid, sid))
        _remove_job_if_exists(scheduler, expiry_ban_job_id(uid, sid))
        if media_user.delete_at is not None:
            _add_or_replace_date_job(
                scheduler,
                run_account_grace_delete,
                run_date=media_user.delete_at,
                user_id=uid,
                server_id=sid,
                job_id=grace_delete_job_id(uid, sid),
            )
        else:
            _remove_job_if_exists(scheduler, grace_delete_job_id(uid, sid))
        return

    _remove_job_if_exists(scheduler, grace_delete_job_id(uid, sid))
    _add_or_replace_date_job(
        scheduler,
        run_account_expiry_ban,
        run_date=media_user.expires_at,
        user_id=uid,
        server_id=sid,
        job_id=expiry_ban_job_id(uid, sid),
    )

    pre_expiry_at = media_user.expires_at - pre_expiry_window
    pre_id = pre_expiry_check_job_id(uid, sid)
    if pre_expiry_at > current_now:
        _add_or_replace_date_job(
            scheduler,
            run_account_pre_expiry_check,
            run_date=pre_expiry_at,
            user_id=uid,
            server_id=sid,
            job_id=pre_id,
        )
    elif media_user.expires_at <= current_now:
        _remove_job_if_exists(scheduler, pre_id)
    # 若已处于 [expires_at - pre_expiry_window, expires_at) 临近窗口内，重启或常规重调度不再补发
    # 新的 pre_expiry_check，避免重复发送临近到期私聊轰炸；到期保活抢救仍由 expiry_ban 兜底。


def _schedule_transient_retry(
    scheduler: AsyncIOScheduler | Any | None,
    func: Callable[[int, int], Any],
    job_id: str,
    user_id: int,
    server_id: int,
    *,
    delay_minutes: int = 15,
) -> None:
    """外部媒体服暂时不可用时为单次生命周期任务安排短期延迟重试。"""
    if scheduler is None:
        return
    _add_or_replace_date_job(
        scheduler,
        func,
        run_date=datetime.now() + timedelta(minutes=delay_minutes),
        user_id=user_id,
        server_id=server_id,
        job_id=job_id,
    )


async def _execute_account_lifecycle_job(
    *,
    user_id: int,
    server_id: int,
    action_label: str,
    runner: Callable[[Any], Awaitable[str | bool]],
    retry_func: Callable[[int, int], Any] | None = None,
    retry_job_id: str | None = None,
    retry_outcome: str = "retryable_error",
) -> None:
    """统一执行单账号生命周期异步任务会话打开、异常回滚与短期重试调度模板。

    Args:
        user_id: Telegram 用户 ID。
        server_id: 目标媒体服务器 ID。
        action_label: 用于错误日志识别的任务中文名称。
        runner: 接收 ``AccountService`` 实例并返回状态字符串/布尔值的异步回调。
        retry_func: 返回可重试状态时需要调度重试的顶层任务函数。
        retry_job_id: 重试任务 ID。
        retry_outcome: 触发延迟重试的返回值标识。
    """
    from main import app  # 避免循环导入
    from services.account_service import AccountService

    async with async_session() as session:
        try:
            account_service = AccountService(app, session)
            outcome = await runner(account_service)
            if (
                retry_func is not None
                and retry_job_id is not None
                and outcome == retry_outcome
            ):
                _schedule_transient_retry(
                    getattr(app.state, "scheduler", None),
                    retry_func,
                    retry_job_id,
                    user_id,
                    server_id,
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception(
                "执行{}单次任务失败: user_id={}, server_id={}, err={}",
                action_label,
                user_id,
                server_id,
                exc,
            )
            await session.rollback()
        finally:
            await session.close()


async def run_account_traffic_reset(user_id: int, server_id: int) -> None:
    """顶层可序列化单次任务：按锚定自然月准点重置媒体账号当期基础流量并自调度下一次重置。

    若重置后解除外部媒体服超流停用失败（可重试通信错误），保留已推进的下月重置时间，
    并额外调度 ``traffic_unsuspend`` 短期重试解封。

    Args:
        user_id: Telegram 用户 ID。
        server_id: 媒体服务器 ID。
    """
    await _execute_account_lifecycle_job(
        user_id=user_id,
        server_id=server_id,
        action_label="账期重置",
        runner=lambda svc: svc.reset_account_traffic_cycle(user_id, server_id),
        retry_func=run_account_traffic_unsuspend,
        retry_job_id=traffic_unsuspend_job_id(user_id, server_id),
        retry_outcome="retry_unsuspend",
    )


async def run_account_traffic_unsuspend(user_id: int, server_id: int) -> None:
    """顶层可序列化单次任务：短期重试账期重置后未能及时解除的外部超流停用状态。

    Args:
        user_id: Telegram 用户 ID。
        server_id: 媒体服务器 ID。
    """
    await _execute_account_lifecycle_job(
        user_id=user_id,
        server_id=server_id,
        action_label="超流停用解封重试",
        runner=lambda svc: svc.release_account_traffic_suspension(user_id, server_id),
        retry_func=run_account_traffic_unsuspend,
        retry_job_id=traffic_unsuspend_job_id(user_id, server_id),
    )


async def run_account_pre_expiry_check(user_id: int, server_id: int) -> None:
    """顶层可序列化单次任务：临近到期节点发送续期提醒与当前条件状态。

    Args:
        user_id: Telegram 用户 ID。
        server_id: 媒体服务器 ID。
    """
    await _execute_account_lifecycle_job(
        user_id=user_id,
        server_id=server_id,
        action_label="临近到期检查",
        runner=lambda svc: svc.process_pre_expiry_check(user_id, server_id),
        retry_func=run_account_pre_expiry_check,
        retry_job_id=pre_expiry_check_job_id(user_id, server_id),
    )


async def run_account_expiry_ban(user_id: int, server_id: int) -> None:
    """顶层可序列化单次任务：到期准点复核自动续期条件，未续期则执行过期封禁并调度宽限删号。

    Args:
        user_id: Telegram 用户 ID。
        server_id: 媒体服务器 ID。
    """
    await _execute_account_lifecycle_job(
        user_id=user_id,
        server_id=server_id,
        action_label="到期封禁",
        runner=lambda svc: svc.ban_expired_account(user_id, server_id),
        retry_func=run_account_expiry_ban,
        retry_job_id=expiry_ban_job_id(user_id, server_id),
    )


async def run_account_grace_delete(user_id: int, server_id: int) -> None:
    """顶层可序列化单次任务：宽限期结束准点删除已过期封禁的媒体账号。

    Args:
        user_id: Telegram 用户 ID。
        server_id: 媒体服务器 ID。
    """
    await _execute_account_lifecycle_job(
        user_id=user_id,
        server_id=server_id,
        action_label="宽限删号",
        runner=lambda svc: svc.delete_expired_banned_account(user_id, server_id),
        retry_func=run_account_grace_delete,
        retry_job_id=grace_delete_job_id(user_id, server_id),
    )


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
    """每日兜底巡检：封禁漏网过期媒体账号（降级保留，主路径由每账号 expiry_ban 准点驱动）

    检查所有过期媒体账号的订阅状态，封禁前先复核是否已开启自动续期并满足服务器准许条件；
    未满足或未开启时，在账号串行锁内二次核验最新到期状态，随后先在媒体服务器上禁用已过期账号，
    再标记本地封禁状态、设置宽限期删除时间并补全 grace_delete 调度。
    """
    from main import app  # 避免循环导入
    from services.account_service import AccountService

    async with async_session() as session:
        try:
            media_repo = MediaRepository(session)
            account_service = AccountService(app, session)
            scheduler = getattr(app.state, "scheduler", None)

            # 兼容历史数据：为已封禁但未设置删除时间的过期账户补全删除时间，仅对已启用服务器补调度删号任务
            backfilled = await media_repo.backfill_missing_delete_at()
            if backfilled > 0 and scheduler is not None:
                banned_rows = await media_repo.find_banned_with_delete_at(
                    enabled_servers_only=True
                )
                for row in banned_rows:
                    schedule_account_lifecycle_jobs(scheduler, row)

            users = await media_repo.find_expired_for_ban()
            if not users:
                logger.info("没有需要封禁的媒体账号。")
                return
            target_accounts = [(int(u.id), int(u.server_id)) for u in users]
            # 释放列表扫描的只读快照，保证每个账号进入锁后都能读取到最新并发提交
            await media_repo.end_clean_read_tx()

            for user_id, server_id in target_accounts:
                await account_service.ban_expired_account(user_id, server_id)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("封禁过期媒体账号时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


# 主路径已由每账号单次 add_job（expiry_ban）精准驱动，
# 本任务降级保留为停机/异常恢复后的每日兜底自动续期巡检（遵循 ADR-0003）
@scheduled_job(
    "cron",
    hour=0,
    minute=10,
    id="auto_renew_watch_time_users",
    replace_existing=True,
)
async def auto_renew_watch_time_users() -> None:
    """每日兜底巡检：补查已到达有效期且开启自动续期的媒体账号并按服务器准许条件自动续期"""
    from main import app  # 避免循环导入
    from services.account_service import AccountService

    async with async_session() as session:
        try:
            account_service = AccountService(app, session)
            renewed_count = await account_service.process_auto_renewals()
            if renewed_count > 0:
                logger.info("本次兜底自动续期完成账号数: {}", renewed_count)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("执行兜底自动续期任务时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


@scheduled_job(
    "cron", hour=0, minute=30, id="delete_expired_banned_users", replace_existing=True
)
async def delete_expired_banned_users() -> None:
    """每日兜底巡检：删除宽限期届满仍未续期的已过期封禁媒体账号（主路径由 grace_delete 驱动）

    对到期满宽限期仍未续期的媒体账号，先删除媒体服务器账户，确认已删除或已不存在后再移除本地记录。
    """
    from main import app  # 避免循环导入
    from services.account_service import AccountService

    async with async_session() as session:
        try:
            media_repo = MediaRepository(session)
            account_service = AccountService(app, session)

            users = await media_repo.find_ban()
            if not users:
                logger.info("没有需要删除的已过期封禁媒体账号。")
                return

            target_pairs = [(int(u.id), int(u.server_id)) for u in users]
            await media_repo.end_clean_read_tx()
            for uid, sid in target_pairs:
                await account_service.delete_expired_banned_account(uid, sid)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("删除已过期封禁媒体账号时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()


@scheduled_job(
    "cron",
    hour=0,
    minute=5,
    id="reconcile_missed_traffic_resets",
    replace_existing=True,
)
async def reconcile_missed_traffic_resets() -> None:
    """每日兜底巡检：补充遗漏的月度账期流量重置、释放残余超流停用并校验生命周期调度任务"""
    from main import app  # 避免循环导入
    from services.account_service import AccountService

    async with async_session() as session:
        try:
            account_service = AccountService(app, session)
            healed = await account_service.reconcile_missed_traffic_resets()
            if healed > 0:
                logger.info("兜底巡检完成遗漏账期重置或超流释放账号数: {}", healed)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("执行兜底账期重置巡检任务时出错: {}", exc)
            await session.rollback()
        finally:
            await session.close()


@scheduled_job(
    "cron",
    hour=2,
    minute=0,
    id="cleanup_orphan_playback_sessions",
    replace_existing=True,
)
async def cleanup_orphan_playback_sessions() -> None:
    """每日兜底巡检：核对媒体服务器 ``/Sessions`` 并截断清理异常长期挂起的孤立播放会话"""
    from main import app  # 避免循环导入
    from services.watch_time_service import WatchTimeService

    async with async_session() as session:
        try:
            service = WatchTimeService(session)
            closed = await service.cleanup_orphan_active_sessions(
                media_clients=app.state.media_clients,
                scheduler=getattr(app.state, "scheduler", None),
                commit=True,
            )
            if closed > 0:
                logger.info("兜底巡检清理孤立挂起播放会话数: {}", closed)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("执行孤立播放会话清理任务时出错: {}", exc)
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
    from services.score_service import ScoreService

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
    from services.user_service import UserService

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
    from services.cache_service import CacheService

    await CacheService.cleanup_expired()


@scheduled_job(
    "cron",
    hour=3,
    minute=30,
    id="cleanup_notification_records",
    replace_existing=True,
)
async def cleanup_notification_records() -> None:
    """每日定时清理超过 30 天的站内兜底收件箱记录与运维告警日志。"""
    from services.notification_service import cleanup_expired_notification_records

    async with async_session() as session:
        try:
            inbox_deleted, ops_deleted = await cleanup_expired_notification_records(
                session
            )
            if inbox_deleted or ops_deleted:
                logger.info(
                    "已清理超过 30 天的历史通知记录: inbox={}, ops_alert={}",
                    inbox_deleted,
                    ops_deleted,
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("清理过期通知记录失败: {}", exc)
            await session.rollback()
        finally:
            await session.close()


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


async def recover_and_calibrate_account_lifecycles(app: FastAPI) -> dict[str, int]:
    """应用启动恢复校准全部媒体账号生命周期调度节点并补救停机遗漏状态。

    包括为存量老账号平滑回填锚定周期与缺省 ``delete_at``、清理残留孤立任务、
    补跑停机错过的账期重置/到期自动续期与封禁/宽限删号，并补全所有未来单次节点任务。

    Args:
        app: FastAPI 应用实例。

    Returns:
        包含各类校准计数统计的字典；发生异常时返回空字典。
    """
    from services.account_service import AccountService

    async with async_session() as session:
        try:
            account_service = AccountService(app, session)
            summary = await account_service.calibrate_account_lifecycles()
            logger.info("媒体账号生命周期调度校准完成: {}", summary)
            return summary
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("启动恢复校准媒体账号生命周期调度失败: {}", exc)
            await session.rollback()
            return {}
        finally:
            await session.close()
