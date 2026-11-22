"""媒体播放时长去重状态机与 Emby 按需主动会话心跳服务"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.economy_policy import is_watch_time_renewal_open
from core.media_utils import (
    clean_str as _clean_str,
)
from core.media_utils import (
    expand_media_id_candidates,
    get_account_lock,
    media_ids_match,
)
from models.emby_webhook import (
    EmbyPayload,
    PlaybackPauseEvent,
    PlaybackProgressEvent,
    PlaybackStartEvent,
    PlaybackStopEvent,
    PlaybackUnpauseEvent,
)
from models.jellyfin_webhook import JellyfinPayload, NotificationType, PlaybackEvent
from models.orm import (
    MediaPlaybackSession,
    MediaUser,
    MediaWatchDaily,
    MediaWatchInterval,
    ServerInstance,
    ServerType,
)
from repositories.config_repo import ConfigRepository

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from fastapi import FastAPI

    from services.media_service import ActiveSession, MediaService

TICKS_PER_SECOND = 10_000_000
EMBY_HEARTBEAT_JOB_PREFIX = "emby_watch_heartbeat_"

# 位置校验与异常防刷阈值（秒）
_SEEK_BACKWARD_TOLERANCE_SECONDS = -3.0
_SEEK_FORWARD_SPEED_MULTIPLIER = 2.5
_SEEK_FORWARD_SLACK_SECONDS = 5.0
_STALLED_ZERO_PROGRESS_SECONDS = 3.0
_STALLED_SAME_POS_SECONDS = 15.0
_INTERVAL_RETENTION_DAYS = 2

# 进程内服务器心跳互斥锁表：加锁顺序必须严格遵守 get_account_lock -> _SERVER_HEARTBEAT_LOCKS，严禁逆序嵌套
_SERVER_HEARTBEAT_LOCKS: dict[int, asyncio.Lock] = {}


class PlaybackAction(StrEnum):
    """标准化播放状态变更动作"""

    START = "start"
    PROGRESS = "progress"
    PAUSE = "pause"
    UNPAUSE = "unpause"
    STOP = "stop"


class StepValidity(StrEnum):
    """单步播放推进合法性判定结果"""

    VALID = "valid"
    WAIT_BUFFER = "wait_buffer"
    INVALID = "invalid"


@dataclass(slots=True, frozen=True)
class WatchTimePolicy:
    """播放时长统计与 Emby 主动心跳运行时策略快照"""

    daily_cap_seconds: int
    grace_seconds: int
    heartbeat_interval_seconds: int


@dataclass(slots=True, frozen=True)
class WatchTimeRenewalStatus:
    """媒体账号当前周期时长保活续期资格评估快照"""

    is_open: bool
    is_eligible: bool
    threshold_seconds: int
    current_seconds: int
    status_text: str
    progress_percent: float | None = None


def calculate_watch_progress_percent(
    current_seconds: int, threshold_seconds: int
) -> float:
    """按保活门槛计算当前周期观看度百分比（范围 ``0.0``–``100.0``，未达标时封顶 ``99.9``）。

    Args:
        current_seconds: 当前周期累计有效观看秒数。
        threshold_seconds: 服务器设定的保活门槛秒数（须大于 0）。

    Returns:
        保留一位小数的向下取整百分比数值，仅当真正达到门槛时返回 ``100.0``。
    """
    if threshold_seconds <= 0 or current_seconds <= 0:
        return 0.0
    if current_seconds >= threshold_seconds:
        return 100.0
    return min(99.9, math.floor(current_seconds * 1000 / threshold_seconds) / 10)


def _format_watch_progress_percent(progress_percent: float) -> str:
    """将观看度百分比数值格式化为紧凑百分比字符串（如 ``50%`` 或 ``99.9%``）。

    Args:
        progress_percent: ``0.0`` 至 ``100.0`` 的观看度数值。

    Returns:
        无多余末尾零的百分比字符串。
    """
    if progress_percent == int(progress_percent):
        return f"{int(progress_percent)}%"
    return f"{progress_percent:.1f}%"


def format_watch_duration_cn(seconds: int) -> str:
    """将有效播放时长秒数格式化为可读中文时长字符串

    Args:
        seconds: 时长秒数

    Returns:
        形如 ``10 小时``、``1 小时 30 分钟`` 或 ``45 秒`` 的中文描述
    """
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours} 小时 {minutes} 分钟" if minutes > 0 else f"{hours} 小时"
    if minutes > 0:
        return f"{minutes} 分钟"
    return f"{secs} 秒"


def evaluate_watch_time_renewal(
    server: ServerInstance | None,
    media_user: MediaUser,
    *,
    global_watch_time_enabled: bool | None = None,
) -> WatchTimeRenewalStatus:
    """统一评估指定媒体账号当前周期的时长保活续期开放状态与达标资格

    依次检验全局 ``watch_time:enable``、服务器实例及其单服时长采集 ``enable_watch_time``
    与保活开关 ``enable_watch_time_renewal``（及门槛 > 0），任一上级关闭均否决保活资格。

    Args:
        server: 所属服务器实例（不存在时按未开放保活处理）。
        media_user: 目标媒体账号实例。
        global_watch_time_enabled: 全局播放时长开关状态；缺省时读取 ``ConfigRepository`` 缓存。

    Returns:
        包含开放状态、达标标志、门槛/当前秒数及展示文案的 ``WatchTimeRenewalStatus``。
    """
    global_on = (
        ConfigRepository.is_watch_time_enabled_from_cache()
        if global_watch_time_enabled is None
        else global_watch_time_enabled
    )
    threshold_seconds = (
        max(0, int(server.watch_time_renewal_threshold_seconds or 0))
        if server is not None
        else 0
    )
    current_seconds = max(0, int(media_user.cycle_watch_time_seconds or 0))
    is_open = is_watch_time_renewal_open(global_on, server)
    if not is_open:
        return WatchTimeRenewalStatus(
            is_open=False,
            is_eligible=False,
            threshold_seconds=threshold_seconds,
            current_seconds=current_seconds,
            status_text="当前服务器未开放保活续期",
            progress_percent=None,
        )

    is_eligible = current_seconds >= threshold_seconds
    progress_percent = calculate_watch_progress_percent(
        current_seconds, threshold_seconds
    )
    pct_label = _format_watch_progress_percent(progress_percent)
    if is_eligible:
        status_text = f"观看度 {pct_label}（已达标，可免费保活续期）"
    else:
        status_text = f"观看度 {pct_label}（未达标）"

    return WatchTimeRenewalStatus(
        is_open=True,
        is_eligible=is_eligible,
        threshold_seconds=threshold_seconds,
        current_seconds=current_seconds,
        status_text=status_text,
        progress_percent=progress_percent,
    )


@dataclass(slots=True, frozen=True)
class NormalizedPlaybackEvent:
    """跨媒体服务器平台统一抽象后的播放事件"""

    action: PlaybackAction
    media_user_id: str
    item_id: str
    session_key: str
    play_session_id: str | None = None
    position_ticks: int | None = None
    is_paused: bool = False


def emby_heartbeat_job_id(server_id: int) -> str:
    """生成指定 Emby 服务器单例心跳任务的调度器 ID

    Args:
        server_id: Emby 服务器实例 ID。

    Returns:
        形如 ``emby_watch_heartbeat_{server_id}`` 的唯一任务 ID。
    """
    return f"{EMBY_HEARTBEAT_JOB_PREFIX}{server_id}"


def _get_server_heartbeat_lock(server_id: int) -> asyncio.Lock:
    """获取指定服务器心跳任务启停同步锁

    Note:
        调用方若同时需要账号锁与心跳锁，必须先获取账号锁再获取心跳锁。

    Args:
        server_id: 媒体服务器实例 ID。

    Returns:
        对应服务器维度的 ``asyncio.Lock``。
    """
    lock = _SERVER_HEARTBEAT_LOCKS.get(server_id)
    if lock is None:
        lock = asyncio.Lock()
        _SERVER_HEARTBEAT_LOCKS[server_id] = lock
    return lock


def _normalize_dt(dt: datetime | None) -> datetime:
    """将时间统一转换为与数据库一致的配置时区朴素时间

    Args:
        dt: 原始 ``datetime``；若为 ``None`` 则取当前配置时区时间。

    Returns:
        去除 ``tzinfo`` 后的本地时间，避免朴素与带时区时间相减异常。
    """
    tz_name = get_settings().timezone
    tz = ZoneInfo(tz_name)
    if dt is None:
        return datetime.now(tz).replace(tzinfo=None)
    if dt.tzinfo is not None:
        return dt.astimezone(tz).replace(tzinfo=None)
    return dt


def normalize_emby_playback_event(
    payload: EmbyPayload,
) -> NormalizedPlaybackEvent | None:
    """将 Emby Webhook 播放类事件映射为标准播放事件

    Args:
        payload: 已通过 ``EmbyPayload`` 校验的事件模型。

    Returns:
        播放事件对应的 ``NormalizedPlaybackEvent``；非播放事件返回 ``None``。
    """
    if isinstance(payload, PlaybackStartEvent):
        action = PlaybackAction.START
    elif isinstance(payload, PlaybackProgressEvent):
        action = PlaybackAction.PROGRESS
    elif isinstance(payload, PlaybackPauseEvent):
        action = PlaybackAction.PAUSE
    elif isinstance(payload, PlaybackUnpauseEvent):
        action = PlaybackAction.UNPAUSE
    elif isinstance(payload, PlaybackStopEvent):
        action = PlaybackAction.STOP
    else:
        return None

    media_user_id = _clean_str(payload.user.id)
    item_id = _clean_str(payload.item.id)
    if not media_user_id or not item_id:
        return None

    pb_info = payload.playback_info
    play_session_id = _clean_str(pb_info.play_session_id) if pb_info else None
    # Emby 同一次客户端播放中切换转码流会更换 PlaySessionId，优先使用稳定的 Session.Id 作为会话键
    session_key = (
        _clean_str(payload.session.id)
        or play_session_id
        or _clean_str(payload.session.device_id)
        or f"{media_user_id}:{item_id}"
    )

    return NormalizedPlaybackEvent(
        action=action,
        media_user_id=media_user_id,
        item_id=item_id,
        session_key=session_key,
        play_session_id=play_session_id,
        position_ticks=pb_info.position_ticks if pb_info else None,
        is_paused=(action == PlaybackAction.PAUSE),
    )


def normalize_jellyfin_playback_event(
    payload: JellyfinPayload,
) -> NormalizedPlaybackEvent | None:
    """将 Jellyfin Webhook 播放类事件映射为标准播放事件

    Args:
        payload: 已通过 ``JellyfinPayload`` 校验的事件模型。

    Returns:
        播放事件对应的 ``NormalizedPlaybackEvent``；非播放事件返回 ``None``。
    """
    if not isinstance(payload, PlaybackEvent):
        return None

    if payload.notification_type == NotificationType.PLAYBACK_START:
        action = PlaybackAction.START
    elif payload.notification_type == NotificationType.PLAYBACK_PROGRESS:
        action = PlaybackAction.PROGRESS
    elif payload.notification_type == NotificationType.PLAYBACK_STOP:
        action = PlaybackAction.STOP
    else:
        return None

    media_user_id = _clean_str(payload.user_id)
    item_id = _clean_str(payload.item_id)
    if not media_user_id or not item_id:
        return None

    session_key = (
        _clean_str(payload.session_id)
        or _clean_str(payload.device_id)
        or f"{media_user_id}:{item_id}"
    )

    return NormalizedPlaybackEvent(
        action=action,
        media_user_id=media_user_id,
        item_id=item_id,
        session_key=session_key,
        play_session_id=None,
        position_ticks=payload.playback_position_ticks,
        is_paused=payload.is_paused,
    )


def _subtract_intervals(
    candidate: tuple[datetime, datetime],
    existing: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """计算候选时间段减去既有已计入区间后的未重复子区间列表

    Args:
        candidate: 本次拟新增的闭开/闭时间范围 ``(start, end)``。
        existing: 账号已持久化过的有效时间段列表。

    Returns:
        尚未计入过的新增时间片段列表，保证严格满足 ``start < end`` 且互不重叠。
    """
    cand_start, cand_end = candidate
    if cand_start >= cand_end:
        return []

    uncovered: list[tuple[datetime, datetime]] = []
    cursor = cand_start
    for exist_start, exist_end in sorted(existing, key=lambda pair: pair[0]):
        if exist_end <= cursor:
            continue
        if exist_start >= cand_end:
            break
        if exist_start > cursor:
            uncovered.append((cursor, min(exist_start, cand_end)))
        cursor = max(cursor, exist_end)
        if cursor >= cand_end:
            break

    if cursor < cand_end:
        uncovered.append((cursor, cand_end))
    return uncovered


def _split_by_calendar_day(
    start_at: datetime,
    end_at: datetime,
) -> list[tuple[date, datetime, datetime]]:
    """按自然日 00:00:00 边界切分时间段，保证单日上限准确计量

    Args:
        start_at: 待切分时间段起点。
        end_at: 待切分时间段终点。

    Returns:
        按自然日拆解后的 ``(日期, 当天起点, 当天终点)`` 元组列表。
    """
    if start_at >= end_at:
        return []

    segments: list[tuple[date, datetime, datetime]] = []
    cursor = start_at
    while cursor < end_at:
        next_midnight = datetime.combine(cursor.date() + timedelta(days=1), time.min)
        seg_end = min(end_at, next_midnight)
        segments.append((cursor.date(), cursor, seg_end))
        cursor = seg_end
    return segments


def _classify_step_validity(
    last_ticks: int | None,
    curr_ticks: int | None,
    wall_dt: float,
    *,
    is_stop: bool,
) -> StepValidity:
    """校验单步推进的真实播放合法性

    Args:
        last_ticks: 上一有效锚点处的媒体播放进度（100ns tick）。
        curr_ticks: 当前观测到的媒体播放进度（100ns tick）。
        wall_dt: 距上一有效锚点的墙钟流逝秒数。
        is_stop: 当前动作是否为停播结算。

    Returns:
        ``StepValidity`` 判定枚举：
        - ``VALID``：位置与墙钟推进匹配，允许计入时长；
        - ``WAIT_BUFFER``：短暂缓冲或进度暂未推进但未超阈值，保留锚点等待下一帧；
        - ``INVALID``：Seek 跳变、零位置停播或长期卡顿，不得产出时长且应重置锚点。
    """
    if wall_dt <= 0:
        return StepValidity.INVALID
    if is_stop and curr_ticks == 0:
        return StepValidity.INVALID
    if last_ticks is None or curr_ticks is None:
        return StepValidity.VALID

    pos_dt = (curr_ticks - last_ticks) / TICKS_PER_SECOND
    if pos_dt < _SEEK_BACKWARD_TOLERANCE_SECONDS:
        return StepValidity.INVALID
    max_allowed = wall_dt * _SEEK_FORWARD_SPEED_MULTIPLIER + _SEEK_FORWARD_SLACK_SECONDS
    if pos_dt > max_allowed:
        return StepValidity.INVALID
    if curr_ticks == 0 and last_ticks == 0:
        if not is_stop and wall_dt <= _STALLED_ZERO_PROGRESS_SECONDS:
            return StepValidity.WAIT_BUFFER
        return StepValidity.INVALID
    if pos_dt <= 0:
        # 无论是否小于 15s，只要播放刻度未实质前进就不立刻记账，交由原锚点累积 wall_dt 等待后续确认或判定卡顿
        if not is_stop and wall_dt < _STALLED_SAME_POS_SECONDS:
            return StepValidity.WAIT_BUFFER
        return StepValidity.INVALID
    return StepValidity.VALID


def ensure_emby_heartbeat_job(
    scheduler: AsyncIOScheduler | None,
    app: FastAPI,
    server_id: int,
    interval_seconds: int,
) -> None:
    """为指定 Emby 服务器注册内存 JobStore 的按需单例会话心跳任务

    Args:
        scheduler: 应用持有的 ``AsyncIOScheduler``；为 ``None`` 时跳过。
        app: FastAPI 应用实例，用于心跳回调访问数据库与媒体客户端。
        server_id: 目标 Emby 服务器 ID。
        interval_seconds: 心跳轮询间隔秒数（最小限制为 5 秒）。
    """
    if scheduler is None:
        return
    job_id = emby_heartbeat_job_id(server_id)
    existing = scheduler.get_job(job_id)
    if existing is not None:
        return

    jobstore = (
        "memory" if "memory" in getattr(scheduler, "_jobstores", {}) else "default"
    )
    scheduler.add_job(
        run_emby_heartbeat_tick,
        "interval",
        seconds=max(5, interval_seconds),
        args=[app, server_id],
        id=job_id,
        jobstore=jobstore,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


def remove_emby_heartbeat_job(
    scheduler: AsyncIOScheduler | None,
    server_id: int,
) -> None:
    """当 Emby 服务器关闭采集或活跃播放归零时，移除其单例心跳任务

    Args:
        scheduler: 应用调度器实例；为 ``None`` 时直接跳过。
        server_id: 目标 Emby 服务器 ID。
    """
    if scheduler is None:
        return
    job_id = emby_heartbeat_job_id(server_id)
    if scheduler.get_job(job_id) is None:
        return
    with contextlib.suppress(Exception):
        scheduler.remove_job(job_id)


def clear_all_emby_heartbeat_jobs(scheduler: AsyncIOScheduler | None) -> int:
    """移除调度器中全部按服务器注册的 Emby 播放心跳任务

    Args:
        scheduler: 应用调度器实例；为 ``None`` 时返回 ``0``。

    Returns:
        成功尝试移除的 Emby 心跳任务数量。
    """
    if scheduler is None:
        return 0
    removed = 0
    for job in list(scheduler.get_jobs()):
        job_id = getattr(job, "id", "")
        if isinstance(job_id, str) and job_id.startswith(EMBY_HEARTBEAT_JOB_PREFIX):
            with contextlib.suppress(Exception):
                scheduler.remove_job(job_id)
                removed += 1
    return removed


def reschedule_emby_heartbeat_jobs(
    scheduler: AsyncIOScheduler | None, interval_seconds: int
) -> int:
    """热更新调度器中所有运行中 Emby 心跳任务的触发周期

    Args:
        scheduler: 应用调度器实例；为 ``None`` 时返回 ``0``。
        interval_seconds: 新的轮询间隔秒数（低于 5 秒自动兜底为 5 秒）。

    Returns:
        成功重调度的 Emby 心跳任务数量。
    """
    if scheduler is None:
        return 0
    safe_seconds = max(5, interval_seconds)
    updated = 0
    for job in list(scheduler.get_jobs()):
        job_id = getattr(job, "id", "")
        if isinstance(job_id, str) and job_id.startswith(EMBY_HEARTBEAT_JOB_PREFIX):
            with contextlib.suppress(Exception):
                scheduler.reschedule_job(
                    job_id,
                    trigger="interval",
                    seconds=safe_seconds,
                )
                updated += 1
    return updated


async def run_emby_heartbeat_tick(app: FastAPI, server_id: int) -> None:
    """Emby 单例心跳定时器回调函数，在独立数据库会话中完成一次全量对账

    Args:
        app: FastAPI 应用实例，用于读取 ``media_clients`` 与 ``scheduler``。
        server_id: 目标 Emby 服务器 ID。
    """
    from core.database import async_session

    async with async_session() as session:
        try:
            service = WatchTimeService(session, app=app)
            media_clients: dict[int, MediaService] = getattr(
                app.state, "media_clients", {}
            )
            scheduler: AsyncIOScheduler | None = getattr(app.state, "scheduler", None)
            await service.reconcile_emby_server_heartbeat(
                server_id,
                media_client=media_clients.get(server_id),
                scheduler=scheduler,
                app=app,
                commit=True,
            )
            await session.commit()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("Emby 服务器 {} 心跳对账失败: {}", server_id, exc)
            await session.rollback()
        finally:
            await session.close()


async def recover_active_emby_heartbeats(app: FastAPI) -> None:
    """应用启动时清理孤儿活跃会话，并恢复符合条件的 Emby 服务器按需心跳

    副作用：
    - 若全局播放时长已关闭，清理遗留的全部活跃会话并清空心跳任务（不改动历史统计）；
    - 否则先停用已禁用、关闭统计或非 Emby 服务器的遗留活跃会话，再为仍有在播会话的 Emby 服务器重新挂载心跳；
    - 结束前提交数据库会话以持久化启动清理结果。

    Args:
        app: FastAPI 应用实例。
    """
    from core.database import async_session

    async with async_session() as session:
        try:
            cfg = ConfigRepository(session)
            service = WatchTimeService(session)
            scheduler: AsyncIOScheduler | None = getattr(app.state, "scheduler", None)
            if not await cfg.is_watch_time_enabled():
                await service.deactivate_server_sessions(None, commit=True)
                clear_all_emby_heartbeat_jobs(scheduler)
                return

            orphan_stmt = (
                select(MediaPlaybackSession.server_id)
                .outerjoin(
                    ServerInstance,
                    ServerInstance.id == MediaPlaybackSession.server_id,
                )
                .where(
                    MediaPlaybackSession.is_active.is_(True),
                    or_(
                        ServerInstance.id.is_(None),
                        ServerInstance.is_enabled.is_(False),
                        ServerInstance.enable_watch_time.is_(False),
                        ServerInstance.server_type != ServerType.EMBY,
                    ),
                )
                .distinct()
            )
            orphan_server_ids = (await session.execute(orphan_stmt)).scalars().all()
            for orphan_server_id in orphan_server_ids:
                await service.deactivate_server_sessions(orphan_server_id, commit=True)

            wt_cfg = await cfg.get_watch_time_config()
            stmt = (
                select(ServerInstance)
                .join(
                    MediaPlaybackSession,
                    and_(
                        MediaPlaybackSession.server_id == ServerInstance.id,
                        MediaPlaybackSession.is_active.is_(True),
                    ),
                )
                .where(
                    ServerInstance.server_type == ServerType.EMBY,
                    ServerInstance.is_enabled.is_(True),
                    ServerInstance.enable_watch_time.is_(True),
                )
                .distinct()
            )
            servers = (await session.execute(stmt)).scalars().all()
            await session.commit()

            for srv in servers:
                async with _get_server_heartbeat_lock(srv.id):
                    ensure_emby_heartbeat_job(
                        scheduler,
                        app,
                        srv.id,
                        wt_cfg["watch_time_heartbeat_interval_seconds"],
                    )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("启动恢复 Emby 播放心跳失败: {}", exc)
            await session.rollback()
        finally:
            await session.close()


class WatchTimeService:
    """播放时长状态机与 Emby 心跳对账服务"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
        app: Any | None = None,
    ) -> None:
        """初始化服务实例

        Args:
            session: 异步 SQLAlchemy 会话。
            clock: 可选时间注入函数，供单元测试控制确定性时间流逝。
            app: 可选 FastAPI 应用实例，用于在突破保活门槛时发射统一通知。
        """
        self.session = session
        self._clock_fn = clock or datetime.now
        self.app = app
        self.config_repo = ConfigRepository(session)

    def _build_milestone_if_crossed(
        self,
        server: ServerInstance | None,
        media_user: MediaUser | None,
        prev_cycle_seconds: int,
        *,
        global_watch_on: bool,
    ) -> Any | None:
        """在持锁事务提交前检测是否同周期首次突破保活门槛并置位幂等标记。

        Args:
            server: 所属服务器 ORM 实例。
            media_user: 更新后的媒体账号 ORM 实例。
            prev_cycle_seconds: 本次结算前账号当期累计播放秒数。
            global_watch_on: 全局播放时长开关状态。

        Returns:
            首次突破门槛时置位 ``watch_time_milestone_notified=True`` 并返回
            ``WatchTimeRenewalEligiblePayload``；否则返回 ``None``。
        """
        if (
            server is None
            or media_user is None
            or media_user.is_banned
            or media_user.watch_time_milestone_notified
            or not is_watch_time_renewal_open(global_watch_on, server)
        ):
            return None

        threshold = max(0, int(server.watch_time_renewal_threshold_seconds or 0))
        curr = max(0, int(media_user.cycle_watch_time_seconds or 0))
        if threshold <= 0 or not (prev_cycle_seconds < threshold <= curr):
            return None

        from services.notification_renderer import (
            EffectiveEconomyContext,
            WatchTimeRenewalEligiblePayload,
        )

        media_user.watch_time_milestone_notified = True
        return WatchTimeRenewalEligiblePayload(
            server_name=server.name,
            media_name=media_user.media_name,
            watched_seconds=curr,
            threshold_seconds=threshold,
            expires_at=media_user.expires_at,
            auto_renew_enabled=bool(media_user.auto_renew),
            economy=EffectiveEconomyContext.from_cache(server),
        )

    async def _emit_milestone_notifications(
        self,
        app: Any | None,
        pending: Sequence[tuple[int, Any]],
    ) -> None:
        """在事务提交并离开账号锁后发射时长保活首次达标祝贺通知。"""
        if app is None or not pending:
            return
        from services.notification_service import NotificationService

        notifier = NotificationService(app)
        for user_id, payload in pending:
            await notifier.emit(
                user_id=user_id,
                payload=payload,
                session=self.session,
            )

    def _now(self) -> datetime:
        """读取归一化后的当前配置时区时间"""
        return _normalize_dt(self._clock_fn())

    async def _load_policy(self) -> WatchTimePolicy:
        """加载并封装当前播放时长采集与 Emby 心跳策略配置

        Returns:
            包含单日上限、断线宽限与 Emby 心跳周期的 ``WatchTimePolicy`` 对象。
        """
        wt_cfg = await self.config_repo.get_watch_time_config()
        return WatchTimePolicy(
            daily_cap_seconds=wt_cfg["watch_time_daily_cap_seconds"],
            grace_seconds=wt_cfg["watch_time_disconnect_grace_seconds"],
            heartbeat_interval_seconds=wt_cfg["watch_time_heartbeat_interval_seconds"],
        )

    async def _reload_server(self, server_id: int) -> ServerInstance | None:
        """按主键值强制从数据库刷新服务器实例，规避锁等待期间产生的陈旧快照"""
        stmt = (
            select(ServerInstance)
            .where(ServerInstance.id == server_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _reload_media_user(
        self, user_id: int, server_id: int
    ) -> MediaUser | None:
        """按复合主键从数据库强制刷新媒体账号记录

        Args:
            user_id: Telegram 用户 ID。
            server_id: 媒体服务器 ID。

        Returns:
            最新的 ``MediaUser`` 实例；若账号已被删除则返回 ``None``。
        """
        stmt = (
            select(MediaUser)
            .where(MediaUser.id == user_id, MediaUser.server_id == server_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _find_media_user(
        self, server_id: int, media_user_id: str
    ) -> MediaUser | None:
        """按服务器 ID 与媒体侧用户标识查找已注册的 MediaUser

        Args:
            server_id: 媒体服务器 ID。
            media_user_id: Webhook 上报的媒体侧用户 UUID 或标识。

        Returns:
            匹配的 ``MediaUser``；未绑定则返回 ``None``。
        """
        candidates = expand_media_id_candidates(media_user_id)
        stmt = (
            select(MediaUser)
            .where(
                MediaUser.server_id == server_id,
                MediaUser.media_id.in_(candidates),
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _find_session_row(
        self, server_id: int, session_key: str
    ) -> MediaPlaybackSession | None:
        """强制刷新查询同一服务器与会话键对应的会话快照行

        Args:
            server_id: 媒体服务器 ID。
            session_key: 稳定的客户端会话主键。

        Returns:
            对应的 ``MediaPlaybackSession``；不存在时返回 ``None``。
        """
        stmt = (
            select(MediaPlaybackSession)
            .where(
                MediaPlaybackSession.server_id == server_id,
                MediaPlaybackSession.session_key == session_key,
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _list_account_intervals(
        self, user_id: int, server_id: int
    ) -> list[MediaWatchInterval]:
        """读取媒体账号当前保留窗口内已计入的时间区间并按起始时间排序

        Args:
            user_id: Telegram 用户 ID。
            server_id: 媒体服务器 ID。

        Returns:
            按 ``start_at`` 升序排列的区间记录列表。
        """
        stmt = (
            select(MediaWatchInterval)
            .where(
                MediaWatchInterval.user_id == user_id,
                MediaWatchInterval.server_id == server_id,
            )
            .order_by(MediaWatchInterval.start_at.asc())
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def _merge_and_prune_intervals(
        self,
        user_id: int,
        server_id: int,
        existing_rows: list[MediaWatchInterval],
        new_span: tuple[datetime, datetime],
        now: datetime,
    ) -> None:
        """合并新增有效时间段至既有区间集合，并清理保留天数之前的历史记录

        Args:
            user_id: Telegram 用户 ID。
            server_id: 媒体服务器 ID。
            existing_rows: 当前数据库中的区间记录列表。
            new_span: 新产生待合并的时间段 ``(start, end)``。
            now: 归一化后的当前时间。
        """
        new_start, new_end = new_span
        cutoff = now - timedelta(days=_INTERVAL_RETENTION_DAYS)

        overlapping: list[MediaWatchInterval] = []
        merged_start, merged_end = new_start, new_end
        for row in existing_rows:
            if row.end_at < cutoff and row.end_at < new_start:
                await self.session.delete(row)
                continue
            if row.start_at <= merged_end and row.end_at >= merged_start:
                overlapping.append(row)
                merged_start = min(merged_start, row.start_at)
                merged_end = max(merged_end, row.end_at)

        if overlapping:
            keeper = overlapping[0]
            keeper.start_at = merged_start
            keeper.end_at = merged_end
            for duplicate in overlapping[1:]:
                await self.session.delete(duplicate)
        else:
            self.session.add(
                MediaWatchInterval(
                    user_id=user_id,
                    server_id=server_id,
                    start_at=merged_start,
                    end_at=merged_end,
                )
            )

    async def _record_effective_interval(
        self,
        media_user: MediaUser,
        raw_start: datetime,
        raw_end: datetime,
        daily_cap_seconds: int,
        now: datetime,
    ) -> int:
        """将有效播放时段按时间轴并集去重、跨零点拆日、受日上限封顶并持久化入账

        副作用：更新 ``media_user`` 周期/总播放秒数与亚秒余数，写入 ``MediaWatchInterval``
        与 ``MediaWatchDaily``；严格遵守 ADR-0001，绝不改动用户积分。

        Args:
            media_user: 目标媒体账号 ORM 实例（调用方需已持有账号互斥锁）。
            raw_start: 原始播放步长起点。
            raw_end: 原始播放步长终点。
            daily_cap_seconds: 每日允许计入的最高秒数（0 表示不设上限）。
            now: 归一化后的当前时间。

        Returns:
            本次成功新增并计入账号的整数秒数。
        """
        if raw_start >= raw_end:
            return 0

        existing_rows = await self._list_account_intervals(
            media_user.id, media_user.server_id
        )
        uncovered = _subtract_intervals(
            (raw_start, raw_end),
            [(r.start_at, r.end_at) for r in existing_rows],
        )
        if not uncovered:
            return 0

        await self._merge_and_prune_intervals(
            media_user.id,
            media_user.server_id,
            existing_rows,
            (raw_start, raw_end),
            now,
        )

        # 逐日加载/更新日统计缓存，避免同一事务内重复查询同一日期行
        daily_cache: dict[date, MediaWatchDaily] = {}
        added_seconds = 0

        for piece_start, piece_end in uncovered:
            for watch_day, seg_start, seg_end in _split_by_calendar_day(
                piece_start, piece_end
            ):
                seg_ms = max(0, round((seg_end - seg_start).total_seconds() * 1000))
                if seg_ms <= 0:
                    continue

                daily_row = daily_cache.get(watch_day)
                if daily_row is None:
                    stmt = (
                        select(MediaWatchDaily)
                        .where(
                            MediaWatchDaily.user_id == media_user.id,
                            MediaWatchDaily.server_id == media_user.server_id,
                            MediaWatchDaily.watch_date == watch_day,
                        )
                        .execution_options(populate_existing=True)
                    )
                    daily_row = (await self.session.execute(stmt)).scalar_one_or_none()
                    if daily_row is None:
                        daily_row = MediaWatchDaily(
                            user_id=media_user.id,
                            server_id=media_user.server_id,
                            watch_date=watch_day,
                            watch_seconds=0,
                            updated_at=now,
                        )
                        self.session.add(daily_row)
                    daily_cache[watch_day] = daily_row

                total_ms = media_user.watch_time_remainder_ms + seg_ms
                whole_secs = total_ms // 1000
                rem_ms = total_ms % 1000

                if daily_cap_seconds > 0:
                    remaining_cap = max(0, daily_cap_seconds - daily_row.watch_seconds)
                    if whole_secs >= remaining_cap:
                        grant_secs = remaining_cap
                        # 当日配额耗尽后丢弃超出部分亚秒余数，杜绝跨天溢出绕过上限
                        rem_ms = 0
                    else:
                        grant_secs = whole_secs
                else:
                    grant_secs = whole_secs

                media_user.watch_time_remainder_ms = rem_ms
                if grant_secs > 0:
                    daily_row.watch_seconds += grant_secs
                    daily_row.updated_at = now
                    media_user.cycle_watch_time_seconds += grant_secs
                    media_user.total_watch_time_seconds += grant_secs
                    added_seconds += grant_secs

        return added_seconds

    def _init_or_reactivate_session(
        self,
        *,
        server_id: int,
        user_id: int,
        sess: MediaPlaybackSession | None,
        session_key: str,
        play_session_id: str | None,
        item_id: str,
        is_paused: bool,
        position_ticks: int | None,
        now: datetime,
    ) -> MediaPlaybackSession:
        """新建或重新激活播放会话快照并重置有效时长锚点

        Args:
            server_id: 媒体服务器 ID。
            user_id: Telegram 用户 ID。
            sess: 已有历史会话行（可能已停用或为 ``None``）。
            session_key: 客户端稳定会话键。
            play_session_id: 本次关联的单次流 ID。
            item_id: 当前播放媒体条目 ID。
            is_paused: 初始是否处于暂停态。
            position_ticks: 当前播放进度（100ns tick）。
            now: 归一化后的当前时间。

        Returns:
            已加入会话追踪或就地更新后的 ``MediaPlaybackSession`` 实例。
        """
        if sess is None:
            sess = MediaPlaybackSession(
                user_id=user_id,
                server_id=server_id,
                session_key=session_key,
                play_session_id=play_session_id,
                item_id=item_id,
                is_active=True,
                is_paused=is_paused,
                last_position_ticks=position_ticks,
                last_event_at=now,
                last_effective_at=now,
                ended_at=None,
            )
            self.session.add(sess)
            return sess

        sess.user_id = user_id
        sess.play_session_id = play_session_id or sess.play_session_id
        sess.item_id = item_id
        sess.is_active = True
        sess.is_paused = is_paused
        sess.last_position_ticks = position_ticks
        sess.last_event_at = now
        sess.last_effective_at = now
        sess.ended_at = None
        return sess

    async def _rollover_session_item(
        self,
        *,
        server_id: int,
        media_user: MediaUser,
        sess: MediaPlaybackSession | None,
        session_key: str,
        play_session_id: str | None,
        item_id: str,
        is_paused: bool,
        position_ticks: int | None,
        grace_seconds: int,
        daily_cap_seconds: int,
        now: datetime,
    ) -> int:
        """同一会话切换媒体条目时按宽限结算旧条目尾段并重建新条目锚点

        Args:
            server_id: 媒体服务器 ID。
            media_user: 当前会话所属媒体账号实例。
            sess: 原活跃或待新建会话快照。
            session_key: 客户端稳定会话键。
            play_session_id: 新条目关联流 ID。
            item_id: 新播放媒体条目 ID。
            is_paused: 新条目初始暂停状态。
            position_ticks: 新条目当前位置（100ns tick）。
            grace_seconds: 未发 Stop 直接切集时的尾段封顶宽限秒数。
            daily_cap_seconds: 单日有效播放时长上限秒数。
            now: 归一化后的当前时间。

        Returns:
            旧条目尾段按宽限封顶实际补记的秒数。
        """
        added = 0
        if (
            sess is not None
            and sess.is_active
            and sess.item_id != item_id
            and not sess.is_paused
            and sess.user_id == media_user.id
        ):
            capped_end = min(
                now, sess.last_effective_at + timedelta(seconds=grace_seconds)
            )
            added = await self._record_effective_interval(
                media_user,
                sess.last_effective_at,
                capped_end,
                daily_cap_seconds,
                now,
            )

        self._init_or_reactivate_session(
            server_id=server_id,
            user_id=media_user.id,
            sess=sess,
            session_key=session_key,
            play_session_id=play_session_id,
            item_id=item_id,
            is_paused=is_paused,
            position_ticks=position_ticks,
            now=now,
        )
        return added

    async def _advance_and_record_step(
        self,
        *,
        media_user: MediaUser,
        sess: MediaPlaybackSession,
        curr_ticks: int | None,
        step_cap_seconds: int,
        daily_cap_seconds: int,
        now: datetime,
        is_stop: bool = False,
    ) -> tuple[StepValidity, int]:
        """校验单步播放推进合法性，并在有效时入账、异常时重置锚点

        Note:
            当判定为 ``StepValidity.WAIT_BUFFER`` 时，有意保留 ``sess.last_effective_at``
            与 ``sess.last_position_ticks`` 不变，使后续正常恢复或持续卡顿能够依据原始锚点累计判断。

        Args:
            media_user: 当前会话所属媒体账号实例。
            sess: 待推进的活跃会话快照。
            curr_ticks: 本次观测到的播放位置（100ns tick）。
            step_cap_seconds: 单步推进最大封顶秒数。
            daily_cap_seconds: 每日播放上限秒数。
            now: 归一化后的当前时间。
            is_stop: 是否为停播动作触发的最终步长校验。

        Returns:
            二元组 ``(步长判定枚举, 新增有效入账秒数)``。
        """
        wall_dt = (now - sess.last_effective_at).total_seconds()
        validity = _classify_step_validity(
            sess.last_position_ticks,
            curr_ticks,
            wall_dt,
            is_stop=is_stop,
        )
        added = 0
        if validity == StepValidity.VALID:
            step_end = min(
                now, sess.last_effective_at + timedelta(seconds=step_cap_seconds)
            )
            added = await self._record_effective_interval(
                media_user,
                sess.last_effective_at,
                step_end,
                daily_cap_seconds,
                now,
            )
            sess.last_effective_at = now
            if curr_ticks is not None:
                sess.last_position_ticks = curr_ticks
        elif validity == StepValidity.INVALID:
            sess.last_effective_at = now
            if curr_ticks is not None:
                sess.last_position_ticks = curr_ticks
        return validity, added

    async def _close_and_settle_grace_capped(
        self,
        *,
        media_user: MediaUser | None,
        sess: MediaPlaybackSession,
        grace_seconds: int,
        daily_cap_seconds: int,
        now: datetime,
    ) -> int:
        """按断线宽限上限结算异常失联或超时的活跃会话并关闭快照

        Args:
            media_user: 对应的媒体账号实例；若为空则仅关闭快照不入账。
            sess: 待清理关闭的播放会话快照。
            grace_seconds: 允许从最后有效锚点往后补记的最大宽限秒数。
            daily_cap_seconds: 单日有效时长上限秒数。
            now: 归一化后的当前时间。

        Returns:
            本次按宽限封顶实际补记入账的秒数。
        """
        added = 0
        if media_user is not None and sess.is_active and not sess.is_paused:
            capped_end = min(
                now, sess.last_effective_at + timedelta(seconds=grace_seconds)
            )
            added = await self._record_effective_interval(
                media_user,
                sess.last_effective_at,
                capped_end,
                daily_cap_seconds,
                now,
            )
        sess.is_active = False
        sess.is_paused = False
        sess.ended_at = now
        sess.last_event_at = now
        sess.last_effective_at = now
        return added

    async def _settle_and_close_session_candidates(
        self,
        *,
        server_id: int,
        candidates: Sequence[tuple[int, int]],
        cutoff: datetime,
        policy: WatchTimePolicy,
        now: datetime,
        commit: bool,
        exclude_session_key: str | None = None,
        skip_paused: bool = False,
        live_by_key: dict[str, ActiveSession] | None = None,
        app: Any | None = None,
    ) -> int:
        """逐账号加锁刷新候选活跃会话，跳过未超时或同账号在线会话并按宽限封顶结算关闭

        Args:
            server_id: 媒体服务器实例 ID。
            candidates: 预查询的 ``(session_row_id, user_id)`` 候选列表。
            cutoff: 陈旧会话判定截止时间戳。
            policy: 当前播放时长策略配置。
            now: 归一化后的当前时间。
            commit: 是否按账号提交数据库事务。
            exclude_session_key: 可选排除的当前会话键，防止误杀正在处理事件的会话。
            skip_paused: 为 ``True`` 时跳过暂停态会话。
            live_by_key: 可选远端 ``/Sessions`` 活跃会话索引；命中同账号仍在播会话时保留活跃。
            app: 可选 FastAPI 应用实例，用于在提交后发射保活首次达标通知。

        Returns:
            本次实际截断并关闭的会话快照数量。
        """
        closed_count = 0
        pending_emits: list[tuple[int, Any]] = []
        target_app = app if app is not None else self.app
        for sess_id, user_id in candidates:
            lock = get_account_lock(user_id, server_id)
            async with lock:
                db_sess = (
                    await self.session.execute(
                        select(MediaPlaybackSession)
                        .where(MediaPlaybackSession.id == sess_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if (
                    db_sess is None
                    or not db_sess.is_active
                    or db_sess.last_event_at >= cutoff
                    or (skip_paused and db_sess.is_paused)
                    or (
                        exclude_session_key is not None
                        and db_sess.session_key == exclude_session_key
                    )
                ):
                    if commit and self.session.in_transaction():
                        await self.session.commit()
                    continue

                mu = await self._reload_media_user(db_sess.user_id, server_id)
                if live_by_key is not None:
                    live = self._match_live_session(db_sess, live_by_key)
                    if (
                        live is not None
                        and mu is not None
                        and (
                            not live.user_id
                            or media_ids_match(mu.media_id, live.user_id)
                        )
                    ):
                        if commit and self.session.in_transaction():
                            await self.session.commit()
                        continue

                prev_cycle_seconds = (
                    int(mu.cycle_watch_time_seconds or 0) if mu is not None else 0
                )
                added = await self._close_and_settle_grace_capped(
                    media_user=mu,
                    sess=db_sess,
                    grace_seconds=policy.grace_seconds,
                    daily_cap_seconds=policy.daily_cap_seconds,
                    now=now,
                )
                if added > 0 and mu is not None and commit and target_app is not None:
                    srv = await self._reload_server(server_id)
                    global_on = await self.config_repo.is_watch_time_enabled()
                    milestone_payload = self._build_milestone_if_crossed(
                        srv,
                        mu,
                        prev_cycle_seconds,
                        global_watch_on=global_on,
                    )
                    if milestone_payload is not None:
                        pending_emits.append((mu.id, milestone_payload))
                closed_count += 1
                await self.session.flush()
                if commit:
                    await self.session.commit()

        if commit and pending_emits:
            await self._emit_milestone_notifications(target_app, pending_emits)
        return closed_count

    async def _cleanup_stale_jellyfin_sessions(
        self,
        server_id: int,
        exclude_session_key: str,
        policy: WatchTimePolicy,
        now: datetime,
        *,
        commit: bool = False,
        app: Any | None = None,
    ) -> None:
        """在 Jellyfin Webhook 到来时逐账号加锁清理其他已超时失联的在播会话

        Note:
            必须在未持有任何账号锁时调用；每条候选会话会按自身 ``(user_id, server_id)``
            获取独立账号锁并强制刷新状态后再结算，从而避免跨账号锁冲突或重复入账。

        Args:
            server_id: Jellyfin 服务器实例 ID。
            exclude_session_key: 当前正在处理事件的会话键，排除以免误杀。
            policy: 当前播放时长策略配置。
            now: 归一化后的当前时间。
            commit: 是否在每条陈旧会话清理完毕后立即提交事务以缩小锁边界。
            app: 可选 FastAPI 应用实例，用于发射保活首次达标通知。
        """
        cutoff = now - timedelta(seconds=policy.grace_seconds)
        # 仅清理其他处于播放中却长期失去心跳的会话；排除当前上报会话且不误杀短时暂停会话
        stmt = (
            select(MediaPlaybackSession.id, MediaPlaybackSession.user_id)
            .where(
                MediaPlaybackSession.server_id == server_id,
                MediaPlaybackSession.is_active.is_(True),
                MediaPlaybackSession.is_paused.is_(False),
                MediaPlaybackSession.session_key != exclude_session_key,
                MediaPlaybackSession.last_event_at < cutoff,
            )
            .order_by(
                MediaPlaybackSession.user_id.asc(),
                MediaPlaybackSession.id.asc(),
            )
        )
        candidates = [
            (int(row_id), int(row_uid))
            for row_id, row_uid in (await self.session.execute(stmt)).all()
        ]
        if commit and self.session.in_transaction():
            await self.session.commit()

        await self._settle_and_close_session_candidates(
            server_id=server_id,
            candidates=candidates,
            cutoff=cutoff,
            policy=policy,
            now=now,
            commit=commit,
            exclude_session_key=exclude_session_key,
            skip_paused=True,
            app=app,
        )

    async def process_playback_event(
        self,
        server: ServerInstance,
        event: NormalizedPlaybackEvent,
        *,
        app: FastAPI | None = None,
        scheduler: AsyncIOScheduler | None = None,
        commit: bool = False,
    ) -> int:
        """处理单个标准化的播放事件并返回本次新增入账秒数

        Args:
            server: 触发 Webhook 的服务器 ORM 实例。
            event: 标准化的播放事件。
            app: 可选 FastAPI 实例，用于按需注册 Emby 心跳。
            scheduler: 可选调度器实例；缺省时尝试从 ``app.state.scheduler`` 获取。
            commit: 是否在账号互斥锁作用域内提交数据库事务（生产路由应设为 ``True`` 以防并发快照滞后）。

        Returns:
            本次事件有效推进并入账的秒数；未采集或被判定无效则返回 ``0``。
        """
        server_id = server.id
        if not server.is_enabled or not server.enable_watch_time:
            return 0
        if not await self.config_repo.is_watch_time_enabled():
            return 0

        media_user = await self._find_media_user(server_id, event.media_user_id)
        if media_user is None:
            return 0
        user_id = media_user.id

        now = self._now()
        policy = await self._load_policy()
        if commit:
            if self.session.in_transaction():
                await self.session.commit()
        else:
            await self.session.flush()

        target_app = app if app is not None else self.app
        if server.server_type == ServerType.JELLYFIN:
            await self._cleanup_stale_jellyfin_sessions(
                server_id,
                event.session_key,
                policy,
                now,
                commit=commit,
                app=target_app,
            )

        pending_milestone: tuple[int, Any] | None = None
        lock = get_account_lock(user_id, server_id)
        async with lock:
            fresh_server = await self._reload_server(server_id)
            if (
                fresh_server is None
                or not fresh_server.is_enabled
                or not fresh_server.enable_watch_time
                or not await self.config_repo.is_watch_time_enabled()
            ):
                if commit and self.session.in_transaction():
                    await self.session.commit()
                return 0

            fresh_mu = await self._reload_media_user(user_id, server_id)
            if fresh_mu is None:
                if commit and self.session.in_transaction():
                    await self.session.commit()
                return 0

            prev_cycle_seconds = int(fresh_mu.cycle_watch_time_seconds or 0)
            sess = await self._find_session_row(server_id, event.session_key)
            added_seconds = await self._apply_event_to_session(
                server_id=server_id,
                media_user=fresh_mu,
                sess=sess,
                event=event,
                policy=policy,
                now=now,
            )
            if added_seconds > 0 and commit and target_app is not None:
                milestone_payload = self._build_milestone_if_crossed(
                    fresh_server,
                    fresh_mu,
                    prev_cycle_seconds,
                    global_watch_on=True,
                )
                if milestone_payload is not None:
                    pending_milestone = (user_id, milestone_payload)
            await self.session.flush()
            if commit:
                await self.session.commit()

            # Emby 服务器按需启停单例心跳：务必在数据落盘后加心跳锁再检测并同步任务
            if fresh_server.server_type == ServerType.EMBY:
                async with _get_server_heartbeat_lock(server_id):
                    await self._sync_emby_heartbeat(
                        server_id,
                        app=target_app,
                        scheduler=scheduler,
                        heartbeat_interval=policy.heartbeat_interval_seconds,
                        allow_add=True,
                    )

        if commit and pending_milestone is not None:
            await self._emit_milestone_notifications(target_app, [pending_milestone])
        return added_seconds

    async def _server_has_active_sessions(self, server_id: int) -> bool:
        """检查指定服务器是否仍存在活跃（含暂时暂停但未停播）会话"""
        stmt = (
            select(MediaPlaybackSession.id)
            .where(
                MediaPlaybackSession.server_id == server_id,
                MediaPlaybackSession.is_active.is_(True),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def _sync_emby_heartbeat(
        self,
        server_id: int,
        *,
        app: FastAPI | None,
        scheduler: AsyncIOScheduler | None,
        heartbeat_interval: int,
        allow_add: bool,
    ) -> None:
        """在服务器心跳锁内依据当前活跃会话数按需挂载或移除 Emby 单例心跳任务

        Args:
            server_id: Emby 服务器实例 ID。
            app: FastAPI 应用实例，新增心跳时用于回调访问状态。
            scheduler: 可选显式传入的调度器，缺省时从 ``app.state.scheduler`` 获取。
            heartbeat_interval: 当前生效的 Emby 心跳轮询周期秒数。
            allow_add: 是否允许在仍有活跃会话时挂载新任务（对账结束仅作摘除判断时传 ``False``）。
        """
        target_scheduler = scheduler or (
            getattr(app.state, "scheduler", None) if app is not None else None
        )
        if target_scheduler is None:
            return
        has_active = await self._server_has_active_sessions(server_id)
        if has_active and allow_add and app is not None:
            ensure_emby_heartbeat_job(
                target_scheduler, app, server_id, heartbeat_interval
            )
        elif not has_active:
            remove_emby_heartbeat_job(target_scheduler, server_id)

    async def _handle_start_event(
        self,
        *,
        server_id: int,
        media_user: MediaUser,
        sess: MediaPlaybackSession | None,
        event: NormalizedPlaybackEvent,
        policy: WatchTimePolicy,
        now: datetime,
    ) -> int:
        """处理 ``START`` 事件，同一客户端未发 Stop 直接切集时先按宽限结算旧条目尾段"""
        return await self._rollover_session_item(
            server_id=server_id,
            media_user=media_user,
            sess=sess,
            session_key=event.session_key,
            play_session_id=event.play_session_id,
            item_id=event.item_id,
            is_paused=event.is_paused,
            position_ticks=event.position_ticks,
            grace_seconds=policy.grace_seconds,
            daily_cap_seconds=policy.daily_cap_seconds,
            now=now,
        )

    async def _handle_pause_event(
        self,
        *,
        media_user: MediaUser,
        sess: MediaPlaybackSession | None,
        event: NormalizedPlaybackEvent,
        policy: WatchTimePolicy,
        now: datetime,
    ) -> int:
        """处理 ``PAUSE`` 事件，结算暂停前有效片段并将快照切换为暂停态"""
        if (
            sess is None
            or not sess.is_active
            or sess.user_id != media_user.id
            or sess.item_id != event.item_id
        ):
            return 0
        added = 0
        if not sess.is_paused:
            _, added = await self._advance_and_record_step(
                media_user=media_user,
                sess=sess,
                curr_ticks=event.position_ticks,
                step_cap_seconds=policy.grace_seconds,
                daily_cap_seconds=policy.daily_cap_seconds,
                now=now,
                is_stop=False,
            )
        sess.is_paused = True
        sess.play_session_id = event.play_session_id or sess.play_session_id
        if event.position_ticks is not None:
            sess.last_position_ticks = event.position_ticks
        sess.last_event_at = now
        sess.last_effective_at = now
        return added

    async def _handle_unpause_event(
        self,
        *,
        server_id: int,
        media_user: MediaUser,
        sess: MediaPlaybackSession | None,
        event: NormalizedPlaybackEvent,
        policy: WatchTimePolicy,
        now: datetime,
    ) -> int:
        """处理 ``UNPAUSE`` 事件，支持重启/漏收 Start 场景重建、切集结算与抖动过滤"""
        if sess is None or not sess.is_active or sess.user_id != media_user.id:
            # 若服务重启、漏收 Start 或同设备切换账号后首次收到 Unpause，重建当前账号活跃快照避免跨账号串账
            self._init_or_reactivate_session(
                server_id=server_id,
                user_id=media_user.id,
                sess=sess,
                session_key=event.session_key,
                play_session_id=event.play_session_id,
                item_id=event.item_id,
                is_paused=False,
                position_ticks=event.position_ticks,
                now=now,
            )
            return 0
        if sess.item_id != event.item_id:
            return await self._rollover_session_item(
                server_id=server_id,
                media_user=media_user,
                sess=sess,
                session_key=event.session_key,
                play_session_id=event.play_session_id,
                item_id=event.item_id,
                is_paused=False,
                position_ticks=event.position_ticks,
                grace_seconds=policy.grace_seconds,
                daily_cap_seconds=policy.daily_cap_seconds,
                now=now,
            )
        if sess.is_paused:
            # 从暂停恢复播放：暂停区间绝对不计入有效时长
            sess.is_paused = False
            sess.play_session_id = event.play_session_id or sess.play_session_id
            if event.position_ticks is not None:
                sess.last_position_ticks = event.position_ticks
            sess.last_event_at = now
            sess.last_effective_at = now
            return 0

        # 已在播放态时收到冗余 unpause（常见于 Android 客户端拖动进度条 Seek 或事件抖动）
        wall_dt = (now - sess.last_effective_at).total_seconds()
        validity = _classify_step_validity(
            sess.last_position_ticks,
            event.position_ticks,
            wall_dt,
            is_stop=False,
        )
        sess.last_event_at = now
        sess.play_session_id = event.play_session_id or sess.play_session_id
        if validity == StepValidity.INVALID:
            # 若属于 Seek 跳变，重置锚点避免后续合并成虚高正常时长
            if event.position_ticks is not None:
                sess.last_position_ticks = event.position_ticks
            sess.last_effective_at = now
        return 0

    async def _handle_progress_event(
        self,
        *,
        server_id: int,
        media_user: MediaUser,
        sess: MediaPlaybackSession | None,
        event: NormalizedPlaybackEvent,
        policy: WatchTimePolicy,
        now: datetime,
    ) -> int:
        """处理 ``PROGRESS`` 事件，兼顾漏 Start 重建、同会话切集结算与卡顿等待"""
        if sess is None or not sess.is_active or sess.user_id != media_user.id:
            # 若共享设备切换为其他账号直接上报 Progress，必须重新绑定为新账号起点，禁止继承旧账号锚点
            self._init_or_reactivate_session(
                server_id=server_id,
                user_id=media_user.id,
                sess=sess,
                session_key=event.session_key,
                play_session_id=event.play_session_id,
                item_id=event.item_id,
                is_paused=event.is_paused,
                position_ticks=event.position_ticks,
                now=now,
            )
            return 0
        if sess.item_id != event.item_id:
            return await self._rollover_session_item(
                server_id=server_id,
                media_user=media_user,
                sess=sess,
                session_key=event.session_key,
                play_session_id=event.play_session_id,
                item_id=event.item_id,
                is_paused=event.is_paused,
                position_ticks=event.position_ticks,
                grace_seconds=policy.grace_seconds,
                daily_cap_seconds=policy.daily_cap_seconds,
                now=now,
            )
        if sess.is_paused:
            if not event.is_paused:
                sess.is_paused = False
                sess.last_effective_at = now
            if event.position_ticks is not None:
                sess.last_position_ticks = event.position_ticks
            sess.play_session_id = event.play_session_id or sess.play_session_id
            sess.last_event_at = now
            return 0

        _, added = await self._advance_and_record_step(
            media_user=media_user,
            sess=sess,
            curr_ticks=event.position_ticks,
            step_cap_seconds=policy.grace_seconds,
            daily_cap_seconds=policy.daily_cap_seconds,
            now=now,
            is_stop=False,
        )
        sess.last_event_at = now
        sess.play_session_id = event.play_session_id or sess.play_session_id
        if event.is_paused:
            sess.is_paused = True
        return added

    async def _handle_stop_event(
        self,
        *,
        media_user: MediaUser,
        sess: MediaPlaybackSession | None,
        event: NormalizedPlaybackEvent,
        policy: WatchTimePolicy,
        now: datetime,
    ) -> int:
        """处理 ``STOP`` 事件，结算最终有效步长并幂等关闭当前匹配条目会话"""
        if (
            sess is None
            or not sess.is_active
            or sess.user_id != media_user.id
            or sess.item_id != event.item_id
        ):
            return 0
        added = 0
        if not sess.is_paused:
            _, added = await self._advance_and_record_step(
                media_user=media_user,
                sess=sess,
                curr_ticks=event.position_ticks,
                step_cap_seconds=policy.grace_seconds,
                daily_cap_seconds=policy.daily_cap_seconds,
                now=now,
                is_stop=True,
            )
        sess.is_active = False
        sess.is_paused = False
        sess.ended_at = now
        sess.last_event_at = now
        sess.last_effective_at = now
        if event.position_ticks is not None:
            sess.last_position_ticks = event.position_ticks
        return added

    async def _apply_event_to_session(
        self,
        *,
        server_id: int,
        media_user: MediaUser,
        sess: MediaPlaybackSession | None,
        event: NormalizedPlaybackEvent,
        policy: WatchTimePolicy,
        now: datetime,
    ) -> int:
        """在持锁上下文中按标准动作类型分发并执行会话状态迁移"""
        if event.action == PlaybackAction.START:
            return await self._handle_start_event(
                server_id=server_id,
                media_user=media_user,
                sess=sess,
                event=event,
                policy=policy,
                now=now,
            )
        if event.action == PlaybackAction.PAUSE:
            return await self._handle_pause_event(
                media_user=media_user,
                sess=sess,
                event=event,
                policy=policy,
                now=now,
            )
        if event.action == PlaybackAction.UNPAUSE:
            return await self._handle_unpause_event(
                server_id=server_id,
                media_user=media_user,
                sess=sess,
                event=event,
                policy=policy,
                now=now,
            )
        if event.action == PlaybackAction.PROGRESS:
            return await self._handle_progress_event(
                server_id=server_id,
                media_user=media_user,
                sess=sess,
                event=event,
                policy=policy,
                now=now,
            )
        if event.action == PlaybackAction.STOP:
            return await self._handle_stop_event(
                media_user=media_user,
                sess=sess,
                event=event,
                policy=policy,
                now=now,
            )
        return 0

    async def _reconcile_single_emby_session(
        self,
        *,
        server_id: int,
        db_sess: MediaPlaybackSession,
        live_by_key: dict[str, ActiveSession],
        policy: WatchTimePolicy,
        live_step_cap: int,
        now: datetime,
    ) -> int:
        """在账号锁内完成单条 Emby 数据库会话与 ``/Sessions`` 快照的对账结算

        Args:
            server_id: Emby 服务器实例 ID。
            db_sess: 加锁后强制刷新的活跃数据库会话快照。
            live_by_key: 远端 ``get_active_sessions()`` 建立的查找索引。
            policy: 当前播放时长策略配置。
            live_step_cap: 正常在线心跳步长封顶秒数。
            now: 归一化后的当前时间。

        Returns:
            本条会话在本次对账中新增入账的秒数。
        """
        mu = await self._reload_media_user(db_sess.user_id, server_id)
        if mu is None:
            return await self._close_and_settle_grace_capped(
                media_user=None,
                sess=db_sess,
                grace_seconds=policy.grace_seconds,
                daily_cap_seconds=policy.daily_cap_seconds,
                now=now,
            )

        live = self._match_live_session(db_sess, live_by_key)
        if (
            live is not None
            and live.user_id
            and not media_ids_match(mu.media_id, live.user_id)
        ):
            # 共享设备切换为其他账号登录时，不得用新用户的远端会话继续推进或续命旧账号快照
            live = None
        if live is None:
            # 未发 Stop 异常掉线：若先前处于播放中，按 last_effective_at + grace 封顶截断
            return await self._close_and_settle_grace_capped(
                media_user=mu,
                sess=db_sess,
                grace_seconds=policy.grace_seconds,
                daily_cap_seconds=policy.daily_cap_seconds,
                now=now,
            )

        # 若同一客户端会话在心跳间隙切换了条目且未发 Stop，按旧条目结算并重建锚点
        if live.item_id and live.item_id != db_sess.item_id:
            return await self._rollover_session_item(
                server_id=server_id,
                media_user=mu,
                sess=db_sess,
                session_key=db_sess.session_key,
                play_session_id=live.play_session_id or db_sess.play_session_id,
                item_id=live.item_id,
                is_paused=live.is_paused,
                position_ticks=live.position_ticks,
                grace_seconds=policy.grace_seconds,
                daily_cap_seconds=policy.daily_cap_seconds,
                now=now,
            )

        if live.is_paused:
            added = 0
            if not db_sess.is_paused:
                _, added = await self._advance_and_record_step(
                    media_user=mu,
                    sess=db_sess,
                    curr_ticks=live.position_ticks,
                    step_cap_seconds=policy.grace_seconds,
                    daily_cap_seconds=policy.daily_cap_seconds,
                    now=now,
                    is_stop=False,
                )
                db_sess.is_paused = True
            if live.position_ticks is not None:
                db_sess.last_position_ticks = live.position_ticks
            db_sess.last_event_at = now
            db_sess.last_effective_at = now
            return added

        # live 处于在播未暂停态
        if db_sess.is_paused:
            # 漏收 unpause 事件时由心跳自动恢复，暂停期不累计
            db_sess.is_paused = False
            db_sess.last_effective_at = now
            db_sess.last_event_at = now
            if live.position_ticks is not None:
                db_sess.last_position_ticks = live.position_ticks
            return 0

        _, added = await self._advance_and_record_step(
            media_user=mu,
            sess=db_sess,
            curr_ticks=live.position_ticks,
            step_cap_seconds=live_step_cap,
            daily_cap_seconds=policy.daily_cap_seconds,
            now=now,
            is_stop=False,
        )
        db_sess.last_event_at = now
        if live.play_session_id:
            db_sess.play_session_id = live.play_session_id
        return added

    async def reconcile_emby_server_heartbeat(
        self,
        server_id: int,
        *,
        media_client: MediaService | None,
        scheduler: AsyncIOScheduler | None = None,
        app: Any | None = None,
        commit: bool = False,
    ) -> int:
        """执行一次 Emby 服务器主动会话心跳对账

        规则：
        1. 若全局或服务器播放时长采集已关闭、服务器被禁用或客户端缺失：清理活跃会话状态并移除定时任务（绝不清空历史累计）。
        2. 调用 ``MediaService.get_active_sessions()`` 逐账号加锁并强制刷新快照对账：
           - 仍在线且未暂停：校验合法性后推进 ``[last_effective_at, min(now, last_effective_at + step_cap)]``；
           - 仍在线但已暂停：结算暂停前有效片段后切入暂停态；
           - 会话已从 ``/Sessions`` 消失：若此前非暂停则按 ``min(now, last_effective_at + grace)`` 封顶截断并关闭会话。
        3. 对账结束后在服务器心跳锁内检查是否已无活跃会话，若归零则自动移除心跳任务。

        Args:
            server_id: 目标 Emby 服务器 ID。
            media_client: 对应的 ``MediaService`` 客户端；为 ``None`` 时视为停用。
            scheduler: 可选调度器，用于在无活跃会话或禁用时摘除心跳任务。
            app: 可选 FastAPI 应用实例，用于在提交后发射保活首次达标通知。
            commit: 是否按账号提交数据库事务以配合长耗时网络拉取后的一致性。

        Returns:
            本次心跳对账总共新增入账的秒数。
        """
        target_app = app if app is not None else self.app
        server = await self._reload_server(server_id)
        if (
            server is None
            or not server.is_enabled
            or not server.enable_watch_time
            or server.server_type != ServerType.EMBY
            or not await self.config_repo.is_watch_time_enabled()
            or media_client is None
        ):
            await self.deactivate_server_sessions(server_id, commit=commit)
            async with _get_server_heartbeat_lock(server_id):
                remove_emby_heartbeat_job(scheduler, server_id)
            return 0

        try:
            live_sessions = await media_client.get_active_sessions()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Emby 服务器 {} 心跳拉取活跃会话失败: {}", server_id, exc)
            return 0

        now = self._now()
        policy = await self._load_policy()
        # 正常心跳单步推进上限取宽限窗口与心跳周期+15s 较大值，避免配置心跳>宽限时正常步长被截断
        live_step_cap = max(
            policy.grace_seconds, policy.heartbeat_interval_seconds + 15
        )
        live_by_key = self._index_live_sessions(live_sessions)

        stmt = (
            select(MediaPlaybackSession.id, MediaPlaybackSession.user_id)
            .where(
                MediaPlaybackSession.server_id == server_id,
                MediaPlaybackSession.is_active.is_(True),
            )
            .order_by(
                MediaPlaybackSession.user_id.asc(),
                MediaPlaybackSession.id.asc(),
            )
        )
        candidates = (await self.session.execute(stmt)).all()
        if commit and self.session.in_transaction():
            await self.session.commit()

        total_added = 0
        pending_emits: list[tuple[int, Any]] = []
        for sess_id, user_id in candidates:
            lock = get_account_lock(user_id, server_id)
            async with lock:
                fresh_server = await self._reload_server(server_id)
                if (
                    fresh_server is None
                    or not fresh_server.is_enabled
                    or not fresh_server.enable_watch_time
                    or not await self.config_repo.is_watch_time_enabled()
                ):
                    if commit and self.session.in_transaction():
                        await self.session.commit()
                    break

                db_sess = (
                    await self.session.execute(
                        select(MediaPlaybackSession)
                        .where(MediaPlaybackSession.id == sess_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if db_sess is None or not db_sess.is_active:
                    if commit and self.session.in_transaction():
                        await self.session.commit()
                    continue

                pre_mu = await self._reload_media_user(db_sess.user_id, server_id)
                prev_cycle_seconds = (
                    int(pre_mu.cycle_watch_time_seconds or 0)
                    if pre_mu is not None
                    else 0
                )
                added = await self._reconcile_single_emby_session(
                    server_id=server_id,
                    db_sess=db_sess,
                    live_by_key=live_by_key,
                    policy=policy,
                    live_step_cap=live_step_cap,
                    now=now,
                )
                total_added += added
                if (
                    added > 0
                    and pre_mu is not None
                    and commit
                    and target_app is not None
                ):
                    milestone_payload = self._build_milestone_if_crossed(
                        fresh_server,
                        pre_mu,
                        prev_cycle_seconds,
                        global_watch_on=True,
                    )
                    if milestone_payload is not None:
                        pending_emits.append((pre_mu.id, milestone_payload))
                await self.session.flush()
                if commit:
                    await self.session.commit()

        if commit and pending_emits:
            await self._emit_milestone_notifications(target_app, pending_emits)

        # 若服务器此时仍存在关闭前的残余（例如对账途中全局开关被关闭），确保停用
        post_server = await self._reload_server(server_id)
        if (
            post_server is None
            or not post_server.is_enabled
            or not post_server.enable_watch_time
            or not await self.config_repo.is_watch_time_enabled()
        ):
            await self.deactivate_server_sessions(server_id, commit=commit)
            async with _get_server_heartbeat_lock(server_id):
                remove_emby_heartbeat_job(scheduler, server_id)
            return total_added

        async with _get_server_heartbeat_lock(server_id):
            await self._sync_emby_heartbeat(
                server_id,
                app=None,
                scheduler=scheduler,
                heartbeat_interval=policy.heartbeat_interval_seconds,
                allow_add=False,
            )

        return total_added

    async def deactivate_server_sessions(
        self, server_id: int | None = None, *, commit: bool = False
    ) -> int:
        """停用指定服务器或全平台的活跃会话快照（绝不修改历史累计与日统计）

        Args:
            server_id: 目标服务器 ID；传入 ``None`` 表示停用全部服务器的遗留活跃会话。
            commit: 是否按账号提交事务。

        Returns:
            本次成功关闭的活跃会话快照条数。
        """
        now = self._now()
        policy = await self._load_policy()
        stmt = (
            select(MediaPlaybackSession.user_id, MediaPlaybackSession.server_id)
            .where(MediaPlaybackSession.is_active.is_(True))
            .distinct()
            .order_by(
                MediaPlaybackSession.server_id.asc(),
                MediaPlaybackSession.user_id.asc(),
            )
        )
        if server_id is not None:
            stmt = stmt.where(MediaPlaybackSession.server_id == server_id)
        account_pairs = (await self.session.execute(stmt)).all()
        if commit and self.session.in_transaction():
            await self.session.commit()

        closed_count = 0
        for uid, sid in account_pairs:
            lock = get_account_lock(uid, sid)
            async with lock:
                fresh_mu = (
                    await self.session.execute(
                        select(MediaUser)
                        .where(MediaUser.id == uid, MediaUser.server_id == sid)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                rows_stmt = (
                    select(MediaPlaybackSession)
                    .where(
                        MediaPlaybackSession.user_id == uid,
                        MediaPlaybackSession.server_id == sid,
                        MediaPlaybackSession.is_active.is_(True),
                    )
                    .order_by(MediaPlaybackSession.last_effective_at.asc())
                    .execution_options(populate_existing=True)
                )
                rows = (await self.session.execute(rows_stmt)).scalars().all()
                for row in rows:
                    await self._close_and_settle_grace_capped(
                        media_user=fresh_mu,
                        sess=row,
                        grace_seconds=policy.grace_seconds,
                        daily_cap_seconds=policy.daily_cap_seconds,
                        now=now,
                    )
                    closed_count += 1
                    # 多端并发会话逐条刷新已合并区间，保证下一条通过 _subtract_intervals 去重时不重复叠加
                    await self.session.flush()
                if commit:
                    await self.session.commit()
        return closed_count

    async def _cleanup_server_orphan_sessions(
        self,
        *,
        server_id: int,
        media_clients: dict[int, MediaService],
        scheduler: AsyncIOScheduler | None,
        policy: WatchTimePolicy,
        cutoff: datetime,
        now: datetime,
        commit: bool,
    ) -> int:
        """巡检单台服务器的陈旧活跃会话，确认 ``/Sessions`` 已消失后按宽限截断关闭。

        副作用：若服务器已停用或关闭时长统计，则关闭其所有遗留活跃会话并摘除 Emby 心跳任务；
        否则向媒体服务器拉取活跃会话，在单账号锁内封顶结算并关闭已失联会话，再按需回收空闲心跳任务。

        Args:
            server_id: 目标媒体服务器实例 ID。
            media_clients: 运行期 ``MediaService`` 客户端映射。
            scheduler: 可选 APScheduler 实例，用于在无活跃会话时移除 Emby 心跳任务。
            policy: 当前生效的观看时长采集策略配置。
            cutoff: 陈旧会话判定截止时间戳（早于此时间无事件的活跃会话触发查核）。
            now: 当前统一基准时间。
            commit: 是否按账号/阶段提交数据库事务。

        Returns:
            本台服务器本次巡检截断关闭的活跃会话数量。
        """
        server = await self._reload_server(server_id)
        if server is None or not server.is_enabled or not server.enable_watch_time:
            closed = await self.deactivate_server_sessions(server_id, commit=commit)
            async with _get_server_heartbeat_lock(server_id):
                remove_emby_heartbeat_job(scheduler, server_id)
            return closed

        stmt = (
            select(MediaPlaybackSession.id, MediaPlaybackSession.user_id)
            .where(
                MediaPlaybackSession.server_id == server_id,
                MediaPlaybackSession.is_active.is_(True),
                MediaPlaybackSession.last_event_at < cutoff,
            )
            .order_by(
                MediaPlaybackSession.user_id.asc(),
                MediaPlaybackSession.id.asc(),
            )
        )
        candidates = (await self.session.execute(stmt)).all()
        if not candidates:
            return 0

        media_client = media_clients.get(server_id)
        if media_client is None:
            logger.warning("孤立播放会话巡检跳过，服务器 {} 客户端未连接", server_id)
            return 0

        try:
            live_sessions = await media_client.get_active_sessions()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "孤立播放会话巡检拉取服务器 {} /Sessions 失败: {}", server_id, exc
            )
            return 0

        live_by_key = self._index_live_sessions(live_sessions)
        if commit and self.session.in_transaction():
            await self.session.commit()

        candidate_pairs = [(int(sess_id), int(uid)) for sess_id, uid in candidates]
        closed_count = await self._settle_and_close_session_candidates(
            server_id=server_id,
            candidates=candidate_pairs,
            cutoff=cutoff,
            policy=policy,
            now=now,
            commit=commit,
            live_by_key=live_by_key,
        )

        if server.server_type == ServerType.EMBY:
            async with _get_server_heartbeat_lock(server_id):
                await self._sync_emby_heartbeat(
                    server_id,
                    app=None,
                    scheduler=scheduler,
                    heartbeat_interval=policy.heartbeat_interval_seconds,
                    allow_add=False,
                )
        return closed_count

    async def cleanup_orphan_active_sessions(
        self,
        *,
        media_clients: dict[int, MediaService],
        scheduler: AsyncIOScheduler | None = None,
        commit: bool = False,
    ) -> int:
        """兜底巡检并截断异常长期挂起且 ``/Sessions`` 已消失的孤立活跃播放状态

        针对 Jellyfin 末尾观众掉线或 Emby 心跳意外缺失导致的长期挂起会话，
        基于策略自动计算安全陈旧阈值 ``max(grace*2, heartbeat*2, 300)``，
        向媒体服务器核对 ``/Sessions`` 后确认已不存在时按断线宽限封顶结算并关闭快照。

        Args:
            media_clients: 运行期媒体客户端映射。
            scheduler: 可选 APScheduler 实例，用于清理空转的 Emby 心跳任务。
            commit: 是否按账号提交数据库事务。

        Returns:
            本次巡检截断关闭的孤立活跃会话数量。
        """
        if not await self.config_repo.is_watch_time_enabled():
            closed = await self.deactivate_server_sessions(None, commit=commit)
            clear_all_emby_heartbeat_jobs(scheduler)
            return closed

        now = self._now()
        policy = await self._load_policy()
        effective_stale = max(
            policy.grace_seconds * 2, policy.heartbeat_interval_seconds * 2, 300
        )
        cutoff = now - timedelta(seconds=effective_stale)

        server_ids = (
            (
                await self.session.execute(
                    select(MediaPlaybackSession.server_id)
                    .where(MediaPlaybackSession.is_active.is_(True))
                    .distinct()
                    .order_by(MediaPlaybackSession.server_id.asc())
                )
            )
            .scalars()
            .all()
        )
        if commit and self.session.in_transaction():
            await self.session.commit()

        total_closed = 0
        for sid in server_ids:
            total_closed += await self._cleanup_server_orphan_sessions(
                server_id=int(sid),
                media_clients=media_clients,
                scheduler=scheduler,
                policy=policy,
                cutoff=cutoff,
                now=now,
                commit=commit,
            )
        return total_closed

    @staticmethod
    def _index_live_sessions(
        sessions: list[ActiveSession],
    ) -> dict[str, ActiveSession]:
        """构建按 ``session_id``、``play_session_id`` 与 ``device_id`` 索引的映射"""
        indexed: dict[str, ActiveSession] = {}
        for item in sessions:
            for key in (item.session_id, item.play_session_id, item.device_id):
                cleaned = _clean_str(key)
                if cleaned and cleaned not in indexed:
                    indexed[cleaned] = item
        return indexed

    @staticmethod
    def _match_live_session(
        db_sess: MediaPlaybackSession,
        live_by_key: dict[str, ActiveSession],
    ) -> ActiveSession | None:
        """匹配数据库快照对应的远端活跃会话对象"""
        for key in (db_sess.session_key, db_sess.play_session_id):
            if key and key in live_by_key:
                return live_by_key[key]
        return None
