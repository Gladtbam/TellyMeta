"""全域统一通知路由器、异步非阻塞投递管线与站内兜底服务"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import FastAPI
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from telethon import errors
from telethon.errors import RPCError

import core.database as _db_module
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from models.events import (
    DEFAULT_GLOBAL_ROUTE_TARGETS,
    SERVER_OWNED_ROUTE_CATEGORIES,
    NotificationAudience,
    NotificationEvent,
    NotificationRouteCategory,
    NotificationRouteScope,
    NotificationTargetType,
    get_event_spec,
)
from models.jellyfin_webhook import ItemEvent as JellyfinItemEvent
from repositories.notification_repo import (
    NotificationInboxRepository,
    NotificationRouteRepository,
    OpsAlertRepository,
    UserNotificationPreferenceRepository,
)
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.notification_renderer import (
    EmbyLibraryNewPayload,
    EpisodeDetail,
    ExternalServerErrorPayload,
    ExternalServerRecoveredPayload,
    JellyfinLibraryNewPayload,
    NotificationPayload,
    NotificationRenderer,
    RadarrDownloadPayload,
    RadarrMovieAddPayload,
    RenderedNotification,
    RequestApprovedPayload,
    RequestCompletedPayload,
    RequestRejectedPayload,
    RequestSubmitPayload,
    SonarrDownloadPayload,
    SonarrSeriesAddPayload,
    SystemTestPayload,
    TelegramDeliveryDegradedPayload,
)

settings = get_settings()
_ORIG_DB_ASYNC_SESSION = _db_module.async_session
async_session = _db_module.async_session


def resolve_active_app() -> Any | None:
    """安全读取已加载的全局 FastAPI 应用实例，未初始化或隔离测试时返回 None。"""
    main_mod = sys.modules.get("main")
    if main_mod is None:
        return None
    app = getattr(main_mod, "app", None)
    if app is None or getattr(app, "state", None) is None:
        return None
    return app


async def resolve_tg_display_name(app_or_client: Any, user_id: int | None) -> str:
    """安全解析 Telegram 用户展示名称（兼容 app / client / 同步或异步 mock）。

    Args:
        app_or_client: FastAPI 应用实例、state 替身或 Telethon 客户端包装器。
        user_id: 目标 Telegram 用户 ID；为 ``None`` 时直接返回空字符串。

    Returns:
        去除首尾空白的展示名；无法解析时返回空字符串。
    """
    if user_id is None:
        return ""
    state = getattr(app_or_client, "state", None)
    client = (
        getattr(state, "telethon_client", None) if state is not None else app_or_client
    )
    getter = getattr(client, "get_user_name", None) if client is not None else None
    if not callable(getter):
        return ""
    try:
        res = getter(user_id)
        if inspect.isawaitable(res):
            res = await res
        if isinstance(res, str) and res.strip():
            return res.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        return ""
    return ""


# 永久不可达错误：触发后立即放弃重试，若为用户私聊则立即转入站内兜底收件箱
_PERMANENT_TELEGRAM_ERRORS: tuple[type[BaseException], ...] = (
    errors.UserIsBlockedError,
    errors.InputUserDeactivatedError,
    errors.UserIdInvalidError,
    errors.PeerIdInvalidError,
    errors.ChatWriteForbiddenError,
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    ValueError,
)

# 瞬时可恢复错误：按指数退避策略有限重试
_TRANSIENT_TELEGRAM_ERRORS: tuple[type[BaseException], ...] = (
    RPCError,
    OSError,
    TimeoutError,
    ConnectionError,
)

# 恢复事件到待清除异常冷却事件的映射关系
_RECOVERY_CLEAR_MAP: dict[NotificationEvent, NotificationEvent] = {
    NotificationEvent.EXTERNAL_SERVER_RECOVERED: NotificationEvent.EXTERNAL_SERVER_ERROR,
    NotificationEvent.ARR_HEALTH_RESTORED: NotificationEvent.ARR_HEALTH_ISSUE,
}
_ERROR_TO_RECOVERY_MAP: dict[NotificationEvent, NotificationEvent] = {
    err_event: rec_event for rec_event, err_event in _RECOVERY_CLEAR_MAP.items()
}


@dataclass(slots=True)
class _CooldownEntry:
    """内存运维冷却窗口记录条目。"""

    expires_at: datetime
    event_value: str
    source_key: str
    server_id: int | None
    server_name: str | None


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """兼容字典与模型对象安全读取字段，避免字符串或缺失属性触发 AttributeError。"""
    if obj is None or isinstance(obj, (str, bytes, int, float, bool)):
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _extract_compat_custom_format(
    cf_info: Any,
) -> tuple[int | None, tuple[str, ...]]:
    """从兼容渲染字典或模型对象中提取 Arr 自定义格式评分与命中名称元组。"""
    if not cf_info:
        return None, ()
    raw_score = _get_field(cf_info, "customFormatScore")
    score = int(raw_score) if isinstance(raw_score, int) else None
    formats = _get_field(cf_info, "customFormats") or []
    names = tuple(
        str(_get_field(cf, "name", cf)).strip()
        for cf in formats
        if str(_get_field(cf, "name", cf)).strip()
    )
    return score, names


def resolve_server_route_precedence(
    *,
    server_override: Any | None,
    global_route: Any | None,
    legacy_topic_id: int | None,
    category: NotificationRouteCategory,
) -> tuple[str, Any | None]:
    """按单服独立归属或可继承全局策略解析单台服务器的通知路由生效来源。

    规则：
        - ``media_library``、``media_request`` 与单服 ``ops_alert`` 不设全局总开关：
          仅按「单服显式配置 -> 遗留话题（仅入库/求片）-> 默认关闭 (default_disabled)」解析，绝不回退全局路由；
        - ``account_lifecycle`` 支持单服覆盖，未配置单服覆盖时继承全局默认路由。

    Args:
        server_override: 单服通知路由对象或 DTO。
        global_route: 全局默认路由对象或 DTO。
        legacy_topic_id: ``ServerInstance`` 上的遗留话题 ID。
        category: 当前通知路由类别。

    Returns:
        ``(source, chosen_route)``，其中 ``source`` 为 ``server_override``、``legacy_topic``、
        ``default_disabled`` 或 ``global``。
    """
    if server_override is not None:
        return "server_override", server_override
    if category in SERVER_OWNED_ROUTE_CATEGORIES:
        if (
            category
            in (
                NotificationRouteCategory.MEDIA_LIBRARY,
                NotificationRouteCategory.MEDIA_REQUEST,
            )
            and legacy_topic_id
        ):
            return "legacy_topic", None
        return "default_disabled", None
    return "global", global_route


def _safe_int_ids(raw: Any) -> list[int]:
    """安全提取有效的正整数 Telegram 用户 ID 列表，兼容缺失或 Mock 对象。"""
    if raw is None or isinstance(raw, (str, bytes, int, bool)):
        return []
    if not isinstance(raw, Iterable):
        return []
    result: list[int] = []
    for item in raw:
        if isinstance(item, int) and not isinstance(item, bool) and item > 0:
            result.append(item)
    return result


@dataclass(frozen=True, slots=True)
class _DeliveryTarget:
    """单次 Telegram 出站投递目标描述。"""

    chat_id: int | str
    reply_to: int | None
    label: str
    is_user_dm: bool = False


@dataclass(slots=True)
class _OpsAlertBatch:
    """汇总同一次运维告警全部路由目标的投递结果，并在终结后落库 OpsAlertLog。"""

    source_key: str
    server_id: int | None
    event_type: str
    severity: str
    title: str
    body_text: str
    created_at: datetime
    remaining: int
    fallback_session_factory: async_sessionmaker[AsyncSession] | None = None
    delivered_targets: list[str] | None = None
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.delivered_targets is None:
            self.delivered_targets = []
        if self.errors is None:
            self.errors = []

    def record(self, target_label: str, *, success: bool, error: str | None) -> bool:
        """记录单个目标终态结果，所有目标完成后返回 True。"""
        assert self.delivered_targets is not None and self.errors is not None
        if success:
            self.delivered_targets.append(target_label)
        elif error:
            self.errors.append(f"{target_label}: {error}")
        self.remaining = max(0, self.remaining - 1)
        return self.remaining == 0

    def build_log_fields(self) -> dict[str, Any]:
        """构建写入 OpsAlertRepository.create_log 的参数映射。"""
        assert self.delivered_targets is not None and self.errors is not None
        if self.delivered_targets and not self.errors:
            status = "sent"
        elif self.delivered_targets and self.errors:
            status = "partial"
        elif self.errors:
            status = "failed"
        else:
            status = "no_targets"
        return {
            "source_key": self.source_key[:128],
            "server_id": self.server_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "title": self.title[:256],
            "body_text": self.body_text,
            "delivered_targets_json": json.dumps(
                self.delivered_targets, ensure_ascii=False
            ),
            "delivery_status": status,
            "error_message": ("; ".join(self.errors)[:512] if self.errors else None),
            "now": self.created_at,
        }


@dataclass(slots=True)
class _DeliveryJob:
    """投递队列中的单个目标任务单元（``target is None`` 表示仅落库审计日志）。"""

    event: NotificationEvent
    category: NotificationRouteCategory
    severity: str
    is_mandatory: bool
    rendered: RenderedNotification
    target: _DeliveryTarget | None
    user_id: int | None
    server_id: int | None
    source_key: str
    skip_inbox_fallback: bool
    created_at: datetime
    fallback_session_factory: async_sessionmaker[AsyncSession] | None = None
    ops_batch: _OpsAlertBatch | None = None
    ops_log_status_override: str | None = None
    flood_attempts: int = 0
    transient_attempts: int = 0


@dataclass(frozen=True, slots=True)
class _ResolvedRouteConfig:
    """归一化后的生效通知路由规则快照。"""

    target_type: NotificationTargetType
    target_chat_id: int | None
    target_topic_id: int | None
    escalate_admin_dm: bool
    enabled_events: frozenset[str]


class NotificationRuntime:
    """挂载在 ``app.state`` 上的共享通知投递运行时（队列、限流、重试与冷却状态）。"""

    def __init__(
        self,
        app: FastAPI | Any,
        *,
        max_concurrency: int = 4,
        per_chat_min_interval: float = 0.05,
        retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0),
        max_flood_retries: int = 3,
        cooldown_window: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] = datetime.now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.app = app
        self.max_concurrency = max(1, int(max_concurrency))
        self.per_chat_min_interval = max(0.0, float(per_chat_min_interval))
        self.retry_delays = tuple(float(d) for d in retry_delays)
        self.max_flood_retries = max(0, int(max_flood_retries))
        self.cooldown_window = cooldown_window
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self.sleep_fn = sleep_fn
        self.session_factory = session_factory

        self.queue: asyncio.Queue[_DeliveryJob | None] = asyncio.Queue()
        self.worker_task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._chat_locks: dict[int | str, asyncio.Lock] = {}
        self._chat_last_sent_at: dict[int | str, float] = {}
        self._in_flight_tasks: set[asyncio.Task[None]] = set()
        self._delayed_tasks: set[asyncio.Task[None]] = set()
        self._db_lock = asyncio.Lock()
        self._cooldown_entries: dict[str, _CooldownEntry] = {}
        self._cooldown_cleared_at: dict[str, datetime] = {}
        self._active_jobs: int = 0
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._stopping = False
        self._persistent_worker = False

    def get_telethon_client(self) -> TelethonClientWarper | Any | None:
        """动态读取当前应用上的 Telethon 客户端实例（缺失或为 None 时返回 None）。"""
        state = getattr(self.app, "state", None)
        if state is None:
            return None
        return getattr(state, "telethon_client", None)

    def resolve_session_factory(
        self, fallback_factory: async_sessionmaker[AsyncSession] | None = None
    ) -> async_sessionmaker[AsyncSession] | None:
        """按优先级解析异步数据库会话工厂；非 FastAPI 测试替身且无显式配置时返回 None。"""
        if self.session_factory is not None:
            return self.session_factory
        state = getattr(self.app, "state", None)
        state_factory = (
            getattr(state, "notification_session_factory", None)
            if state is not None
            else None
        )
        if state_factory is not None:
            return state_factory
        if fallback_factory is not None:
            return fallback_factory
        if async_session is not _ORIG_DB_ASYNC_SESSION:
            return async_session
        if _db_module.async_session is not _ORIG_DB_ASYNC_SESSION:
            return _db_module.async_session
        if isinstance(self.app, FastAPI):
            return _db_module.async_session
        return None

    def start(self, *, persistent: bool = True) -> None:
        """启动后台异步消费 Worker（幂等）。"""
        self._stopping = False
        if persistent:
            self._persistent_worker = True
        if self.worker_task is not None and not self.worker_task.done():
            return
        self.worker_task = asyncio.create_task(self._worker_loop())

    def enqueue(self, job: _DeliveryJob) -> None:
        """将单个投递任务放入异步队列并按需拉起消费 Worker。"""
        self._active_jobs += 1
        self._idle_event.clear()
        self.queue.put_nowait(job)
        if not self._stopping:
            self.start(persistent=self._persistent_worker)

    def _finish_job(self) -> None:
        """扣减活跃任务计数并在全部任务终结时触发排空事件。"""
        self._active_jobs = max(0, self._active_jobs - 1)
        if self._active_jobs == 0:
            self._idle_event.set()
            # 非显式常驻模式（如未调用 lifespan.start() 的测试）排空后自动唤醒退出 Worker，避免悬挂任务
            if (
                not self._persistent_worker
                and not self._stopping
                and self.worker_task is not None
                and not self.worker_task.done()
            ):
                self.queue.put_nowait(None)

    async def drain(self, drain_timeout: float | None = None) -> None:
        """等待当前所有已入队与延迟重试任务全部消费完毕（主要供测试与停机排空使用）。"""
        if self._active_jobs > 0:
            if self.worker_task is None or self.worker_task.done():
                self.start(persistent=self._persistent_worker)
            if drain_timeout is None:
                await self._idle_event.wait()
            else:
                await asyncio.wait_for(self._idle_event.wait(), timeout=drain_timeout)

        # 非显式常驻模式在队列排空后等待自动退出，避免测试残留挂起协程
        if (
            not self._stopping
            and not self._persistent_worker
            and self.worker_task is not None
            and not self.worker_task.done()
        ):
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self.worker_task, timeout=1.0)

    async def stop(self, drain_timeout: float | None = 10.0) -> None:
        """优雅排空在途通知任务并停止后台 Worker。"""
        self._stopping = True
        self._persistent_worker = False
        if self._active_jobs > 0 and (
            self.worker_task is not None and not self.worker_task.done()
        ):
            try:
                await self.drain(drain_timeout=drain_timeout)
            except TimeoutError:
                logger.warning("关闭通知队列等待排空超时，将取消未完成投递任务")

        for task in list(self._delayed_tasks):
            task.cancel()
        for task in list(self._in_flight_tasks):
            task.cancel()
        if self._delayed_tasks or self._in_flight_tasks:
            await asyncio.gather(
                *self._delayed_tasks, *self._in_flight_tasks, return_exceptions=True
            )
        self._delayed_tasks.clear()
        self._in_flight_tasks.clear()

        if self.worker_task is not None and not self.worker_task.done():
            self.queue.put_nowait(None)
            try:
                await asyncio.wait_for(self.worker_task, timeout=2.0)
            except TimeoutError, asyncio.CancelledError:
                self.worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.worker_task
        self.worker_task = None
        self._active_jobs = 0
        self._idle_event.set()

    async def _worker_loop(self) -> None:
        """消费队列并将单个投递任务派发至受全局并发信号量约束的协程中执行。"""
        try:
            while True:
                job = await self.queue.get()
                if job is None:
                    self.queue.task_done()
                    # 若在退出信号期间恰好有新任务入队，继续消费而不提前退出
                    if self._active_jobs > 0 and not self._stopping:
                        continue
                    break
                await self._semaphore.acquire()
                task = asyncio.create_task(self._execute_job_wrapper(job))
                self._in_flight_tasks.add(task)
                task.add_done_callback(self._in_flight_tasks.discard)
        finally:
            if self.worker_task is asyncio.current_task():
                self.worker_task = None

    async def _execute_job_wrapper(self, job: _DeliveryJob) -> None:
        """执行单个投递任务，确保并发信号量与队列计数始终对称释放。"""
        try:
            await self._process_job(job)
        finally:
            self._semaphore.release()
            self.queue.task_done()

    async def _persist_ops_log(
        self,
        *,
        fallback_factory: async_sessionmaker[AsyncSession] | None,
        log_fields: dict[str, Any],
    ) -> None:
        """将运维告警日志写入数据库（忽略非数据库测试替身或表未初始化环境）。"""
        factory = self.resolve_session_factory(fallback_factory)
        if factory is None:
            return
        try:
            async with self._db_lock, factory() as db:
                await OpsAlertRepository(db).create_log(**log_fields)
        except SQLAlchemyError as exc:
            logger.warning("写入 OpsAlertLog 失败: {}", exc)

    async def _record_ops_outcome(
        self,
        job: _DeliveryJob,
        *,
        success: bool,
        error: str | None,
    ) -> None:
        """汇总多目标运维告警结果并在全部目标终结后落库。"""
        if job.ops_batch is None:
            return
        target_label = job.target.label if job.target is not None else "none"
        is_last = job.ops_batch.record(target_label, success=success, error=error)
        if not is_last:
            return
        log_fields = job.ops_batch.build_log_fields()
        if job.ops_log_status_override is not None:
            log_fields["delivery_status"] = job.ops_log_status_override
        await self._persist_ops_log(
            fallback_factory=job.ops_batch.fallback_session_factory,
            log_fields=log_fields,
        )

    async def send_with_chat_pacing(
        self,
        client: TelethonClientWarper | Any,
        *,
        chat_id: int | str,
        message: str,
        file: Any = None,
        buttons: Sequence[Any] | None = None,
        reply_to: int | None = None,
        parse_mode: str | None = "html",
    ) -> Any:
        """按目标 chat_id 加锁并执行最小发送间隔节流后调用 Telethon 发送消息。"""
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            if self.per_chat_min_interval > 0:
                last_sent = self._chat_last_sent_at.get(chat_id)
                if last_sent is not None:
                    elapsed = self.monotonic_clock() - last_sent
                    wait_for = self.per_chat_min_interval - elapsed
                    if wait_for > 0:
                        await self.sleep_fn(wait_for)
            try:
                return await client.send_message(
                    chat_id=chat_id,
                    message=message,
                    file=file,
                    buttons=buttons,
                    reply_to=reply_to,
                    parse_mode=parse_mode,
                    link_preview=False,
                )
            finally:
                self._chat_last_sent_at[chat_id] = self.monotonic_clock()

    async def _persist_inbox_fallback(
        self, job: _DeliveryJob, *, error_message: str
    ) -> None:
        """私聊最终失败且未设置跳过兜底标记时，写入仅存脱敏摘要的站内收件箱。"""
        if (
            job.target is None
            or not job.target.is_user_dm
            or job.user_id is None
            or job.skip_inbox_fallback
        ):
            return
        factory = self.resolve_session_factory(job.fallback_session_factory)
        if factory is None:
            return
        try:
            async with self._db_lock, factory() as db:
                await NotificationInboxRepository(db).create_item(
                    user_id=job.user_id,
                    server_id=job.server_id,
                    event_type=job.event.value,
                    category=job.category.value,
                    severity=job.severity,
                    title=job.rendered.title[:256],
                    body_text=job.rendered.plain_summary,
                    action_url=(
                        job.rendered.action_url[:512]
                        if job.rendered.action_url
                        else None
                    ),
                    is_mandatory=job.is_mandatory,
                    delivery_error=error_message[:512],
                    now=self.clock(),
                )
        except SQLAlchemyError as exc:
            logger.warning(
                "写入站内兜底收件箱失败: user_id={}, event={}, err={}",
                job.user_id,
                job.event.value,
                exc,
            )

    async def _emit_telegram_delivery_degraded(
        self, job: _DeliveryJob, *, error_message: str
    ) -> None:
        """非个人私聊通道在终态投递失败时，向 ops_alert 发射带冷却的通知投递降级告警。

        说明：
            - ``user_dm`` 终态失败由站内收件箱兜底承接，不作为系统降级事件刷屏；
            - ``ops_alert`` 自身的失败由当前批次 ``_record_ops_outcome`` 直接记录，避免自激二次告警；
            - 若当前失败事件本身已是 ``TELEGRAM_DELIVERY_DEGRADED``，直接跳过以防递归自激。
        """
        if (
            job.target is None
            or job.target.is_user_dm
            or job.category == NotificationRouteCategory.OPS_ALERT
            or job.event == NotificationEvent.TELEGRAM_DELIVERY_DEGRADED
        ):
            return
        failure_count = 1 + job.flood_attempts + job.transient_attempts
        payload = TelegramDeliveryDegradedPayload(
            target_label=job.target.label,
            event_type=job.event.value,
            failure_count=failure_count,
            error_message=error_message,
        )
        source_key = f"tg_degraded:{job.target.label}"
        try:
            # 投递降级属于机器人全局通道故障，统一路由至系统全局 ops_alert（不依赖单服告警开关）
            factory = self.resolve_session_factory(job.fallback_session_factory)
            if factory is not None:
                async with factory() as diag_session:
                    await NotificationService(self.app).emit(
                        payload,
                        server_id=None,
                        source_key=source_key,
                        session=diag_session,
                    )
            else:
                await NotificationService(self.app).emit(
                    payload,
                    server_id=None,
                    source_key=source_key,
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "发射 TELEGRAM_DELIVERY_DEGRADED 告警失败: target={}, err={}",
                job.target.label,
                exc,
            )

    async def _handle_terminal_failure(
        self, job: _DeliveryJob, *, error_message: str
    ) -> None:
        """统一收尾不可恢复或已耗尽重试的任务：写私聊兜底、记录运维结果，并在非私聊通道失败时上报降级。"""
        try:
            await self._persist_inbox_fallback(job, error_message=error_message)
            await self._record_ops_outcome(job, success=False, error=error_message)
            await self._emit_telegram_delivery_degraded(
                job, error_message=error_message
            )
        finally:
            self._finish_job()

    def _schedule_retry(self, job: _DeliveryJob, delay: float) -> None:
        """将待重试任务交由独立延迟协程休眠并重新入队，避免阻塞并发信号量槽位。"""
        if self._stopping:
            self._finish_job()
            return
        task = asyncio.create_task(self._requeue_after_delay(job, delay))
        self._delayed_tasks.add(task)
        task.add_done_callback(self._delayed_tasks.discard)

    async def _requeue_after_delay(self, job: _DeliveryJob, delay: float) -> None:
        """等待退避时长后重新入队；若在休眠期间停机或取消则安全扣减活跃计数。"""
        requeued = False
        try:
            if delay > 0:
                await self.sleep_fn(delay)
            if not self._stopping:
                self.queue.put_nowait(job)
                requeued = True
                if not self._stopping and (
                    self.worker_task is None or self.worker_task.done()
                ):
                    self.start(persistent=self._persistent_worker)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("通知延迟重排入队失败: event={}, err={}", job.event.value, exc)
        finally:
            if not requeued:
                self._finish_job()

    async def _process_job(self, job: _DeliveryJob) -> None:
        """执行单次通知出站投递或仅落库运维审计日志；按异常类型执行限流重排、重试或兜底。"""
        if job.target is None:
            try:
                if job.ops_batch is not None:
                    log_fields = job.ops_batch.build_log_fields()
                    if job.ops_log_status_override is not None:
                        log_fields["delivery_status"] = job.ops_log_status_override
                    await self._persist_ops_log(
                        fallback_factory=job.ops_batch.fallback_session_factory,
                        log_fields=log_fields,
                    )
            finally:
                self._finish_job()
            return

        client = self.get_telethon_client()
        if client is None:
            logger.warning(
                "跳过通知投递（Telethon 客户端不可用）: event={}, target={}",
                job.event.value,
                job.target.label,
            )
            await self._handle_terminal_failure(
                job, error_message="Telethon client unavailable"
            )
            return

        try:
            await self.send_with_chat_pacing(
                client,
                chat_id=job.target.chat_id,
                message=job.rendered.telegram_html,
                file=job.rendered.image,
                buttons=job.rendered.buttons,
                reply_to=job.target.reply_to,
            )
            await self._record_ops_outcome(job, success=True, error=None)
            self._finish_job()
        except errors.FloodWaitError as exc:
            wait_seconds = float(max(0, int(getattr(exc, "seconds", 0) or 0)) + 1)
            if job.flood_attempts < self.max_flood_retries:
                job.flood_attempts += 1
                logger.warning(
                    "Telegram 触发限流，将于 {}s 后重排投递 ({}/{}): event={}, target={}",
                    wait_seconds,
                    job.flood_attempts,
                    self.max_flood_retries,
                    job.event.value,
                    job.target.label,
                )
                self._schedule_retry(job, wait_seconds)
                return
            logger.error(
                "Telegram FloodWait 重试耗尽: event={}, target={}, err={}",
                job.event.value,
                job.target.label,
                exc,
            )
            await self._handle_terminal_failure(job, error_message=str(exc))
        except _PERMANENT_TELEGRAM_ERRORS as exc:
            logger.warning(
                "Telegram 永久不可达错误，立即停止重试: event={}, target={}, err={}",
                job.event.value,
                job.target.label,
                exc,
            )
            await self._handle_terminal_failure(job, error_message=str(exc))
        except _TRANSIENT_TELEGRAM_ERRORS as exc:
            if job.transient_attempts < len(self.retry_delays):
                delay = self.retry_delays[job.transient_attempts]
                job.transient_attempts += 1
                logger.warning(
                    "Telegram 瞬时投递错误，将于 {}s 后重试 ({}/{}): event={}, target={}, err={}",
                    delay,
                    job.transient_attempts,
                    len(self.retry_delays),
                    job.event.value,
                    job.target.label,
                    exc,
                )
                self._schedule_retry(job, delay)
                return
            logger.error(
                "Telegram 瞬时重试耗尽: event={}, target={}, err={}",
                job.event.value,
                job.target.label,
                exc,
            )
            await self._handle_terminal_failure(job, error_message=str(exc))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error(
                "通知投递未知异常: event={}, target={}, err={}",
                job.event.value,
                job.target.label,
                exc,
            )
            await self._handle_terminal_failure(job, error_message=str(exc))


def get_or_create_notification_runtime(app: FastAPI | Any) -> NotificationRuntime:
    """获取或初始化挂载在 ``app.state.notification_runtime`` 上的共享通知运行时。

    Args:
        app: FastAPI 实例或携带 ``state`` 属性的应用替身对象。

    Returns:
        单例 ``NotificationRuntime`` 对象。
    """
    state = getattr(app, "state", None)
    if state is not None:
        existing: NotificationRuntime | None = getattr(
            state, "notification_runtime", None
        )
        if existing is not None:
            return existing
    runtime = NotificationRuntime(app)
    if state is not None:
        with contextlib.suppress(AttributeError, TypeError):
            state.notification_runtime = runtime
    return runtime


@dataclass(slots=True)
class NotificationTestReport:
    """单次 ``SYSTEM_TEST`` 通路实测结果详情。"""

    delivered_targets: list[str]
    errors: list[str]
    disabled: bool = False


class NotificationService:
    """统一强类型通知入口服务（整合分层路由、偏好过滤、冷却与异步投递）。"""

    def __init__(self, app: FastAPI | Any) -> None:
        self.app = app
        self.runtime = get_or_create_notification_runtime(app)
        self.last_test_report: NotificationTestReport | None = None

    @property
    def client(self) -> TelethonClientWarper:
        """动态读取关联的 Telethon 客户端（保持既有非空类型签名以兼容过渡期调用点）。"""
        return cast(TelethonClientWarper, self.runtime.get_telethon_client())

    async def drain(self, drain_timeout: float | None = None) -> None:
        """等待异步通知队列排空完成。"""
        await self.runtime.drain(drain_timeout=drain_timeout)

    def _derive_session_factory(
        self, session: AsyncSession | None
    ) -> async_sessionmaker[AsyncSession] | None:
        """从调用方传入会话派生同引擎会话工厂，供无全局工厂的测试环境使用。"""
        if session is None:
            return None
        bind = getattr(session, "bind", None)
        if isinstance(bind, AsyncEngine):
            return async_sessionmaker(bind, class_=AsyncSession, expire_on_commit=False)
        return None

    @staticmethod
    def _route_to_resolved_config(route: Any) -> _ResolvedRouteConfig:
        """将 ORM 通知路由对象转换为归一化生效路由快照。"""
        return _ResolvedRouteConfig(
            target_type=NotificationTargetType(route.target_type),
            target_chat_id=route.target_chat_id,
            target_topic_id=route.target_topic_id,
            escalate_admin_dm=bool(route.escalate_admin_dm),
            enabled_events=frozenset(
                NotificationRouteRepository.parse_enabled_events(
                    route.enabled_events_json
                )
            ),
        )

    @staticmethod
    def _synthesize_legacy_topic_route(topic_id: int) -> _ResolvedRouteConfig:
        """将存量 ServerInstance 的正/负话题字段合成为等效通知路由配置。"""
        if topic_id > 0:
            return _ResolvedRouteConfig(
                target_type=NotificationTargetType.FORUM_TOPIC,
                target_chat_id=None,
                target_topic_id=int(topic_id),
                escalate_admin_dm=False,
                enabled_events=frozenset(),
            )
        return _ResolvedRouteConfig(
            target_type=NotificationTargetType.CHAT_OR_CHANNEL,
            target_chat_id=int(topic_id),
            target_topic_id=None,
            escalate_admin_dm=False,
            enabled_events=frozenset(),
        )

    async def _query_route_config(
        self,
        db: AsyncSession,
        *,
        category: NotificationRouteCategory,
        server_id: int | None,
    ) -> _ResolvedRouteConfig | None:
        """按作用域解析通知路由配置：单服独立类别不回退全局，未配置时默认关闭。"""
        route_repo = NotificationRouteRepository(db)

        if server_id is None:
            # 媒体入库与求片审核不设全局总开关，无 server_id 时默认关闭
            if (
                category in SERVER_OWNED_ROUTE_CATEGORIES
                and category != NotificationRouteCategory.OPS_ALERT
            ):
                return _ResolvedRouteConfig(
                    target_type=NotificationTargetType.DISABLED,
                    target_chat_id=None,
                    target_topic_id=None,
                    escalate_admin_dm=False,
                    enabled_events=frozenset(),
                )
            global_route = await route_repo.get_route(
                scope_type=NotificationRouteScope.GLOBAL,
                category=category,
                server_id=None,
                fresh=True,
            )
            if global_route is not None:
                return self._route_to_resolved_config(global_route)
            return None

        server_route = await route_repo.get_route(
            scope_type=NotificationRouteScope.SERVER,
            category=category,
            server_id=server_id,
            fresh=True,
        )

        # 单服独立类别（入库、求片、服务器运维告警）严禁回退全局路由
        global_route = None
        if category not in SERVER_OWNED_ROUTE_CATEGORIES:
            global_route = await route_repo.get_route(
                scope_type=NotificationRouteScope.GLOBAL,
                category=category,
                server_id=None,
                fresh=True,
            )

        legacy_topic: int | None = None
        if server_route is None and category in (
            NotificationRouteCategory.MEDIA_LIBRARY,
            NotificationRouteCategory.MEDIA_REQUEST,
        ):
            server = await ServerRepository(db).get_by_id(server_id)
            if server is not None:
                legacy_topic = (
                    server.notify_topic_id
                    if category == NotificationRouteCategory.MEDIA_LIBRARY
                    else server.request_notify_topic_id
                )

        source, chosen = resolve_server_route_precedence(
            server_override=server_route,
            global_route=global_route,
            legacy_topic_id=legacy_topic,
            category=category,
        )
        if source == "default_disabled":
            return _ResolvedRouteConfig(
                target_type=NotificationTargetType.DISABLED,
                target_chat_id=None,
                target_topic_id=None,
                escalate_admin_dm=False,
                enabled_events=frozenset(),
            )
        if source == "legacy_topic" and legacy_topic:
            return self._synthesize_legacy_topic_route(int(legacy_topic))
        if chosen is not None:
            return self._route_to_resolved_config(chosen)
        return None

    async def _resolve_route_config(
        self,
        *,
        category: NotificationRouteCategory,
        server_id: int | None,
        session: AsyncSession | None,
        fallback_factory: async_sessionmaker[AsyncSession] | None,
    ) -> _ResolvedRouteConfig:
        """解析生效通知路由规则；单服独立类别缺省为禁用，其余全局类别回退内置默认值。"""
        resolved: _ResolvedRouteConfig | None = None
        if session is not None:
            try:
                resolved = await self._query_route_config(
                    session, category=category, server_id=server_id
                )
            except SQLAlchemyError as exc:
                logger.warning("查询通知路由失败，回退默认策略: {}", exc)
        else:
            factory = self.runtime.resolve_session_factory(fallback_factory)
            if factory is not None:
                try:
                    async with self.runtime._db_lock, factory() as db:
                        resolved = await self._query_route_config(
                            db, category=category, server_id=server_id
                        )
                except SQLAlchemyError as exc:
                    logger.warning("查询通知路由失败，回退默认策略: {}", exc)

        if resolved is None:
            if server_id is not None and category in SERVER_OWNED_ROUTE_CATEGORIES:
                default_target = NotificationTargetType.DISABLED
            else:
                default_target = DEFAULT_GLOBAL_ROUTE_TARGETS.get(
                    category, NotificationTargetType.DISABLED
                )
            return _ResolvedRouteConfig(
                target_type=default_target,
                target_chat_id=None,
                target_topic_id=None,
                escalate_admin_dm=False,
                enabled_events=frozenset(),
            )
        return resolved

    async def _resolve_admin_ids(
        self,
        *,
        session: AsyncSession | None,
        fallback_factory: async_sessionmaker[AsyncSession] | None,
    ) -> list[int]:
        """汇总数据库管理员与内存 ``app.state.admin_ids``，返回去重排序后的管理员 ID 列表。"""
        state = getattr(self.app, "state", None)
        admin_set: set[int] = set(
            _safe_int_ids(getattr(state, "admin_ids", None) if state else None)
        )
        if session is not None:
            try:
                admin_set.update(
                    _safe_int_ids(await TelegramRepository(session).get_admins())
                )
            except SQLAlchemyError as exc:
                logger.warning("查询管理员 ID 失败: {}", exc)
        else:
            factory = self.runtime.resolve_session_factory(fallback_factory)
            if factory is not None:
                try:
                    async with self.runtime._db_lock, factory() as db:
                        admin_set.update(
                            _safe_int_ids(await TelegramRepository(db).get_admins())
                        )
                except SQLAlchemyError as exc:
                    logger.warning("查询管理员 ID 失败: {}", exc)
        return sorted(admin_set)

    async def _resolve_route_targets(
        self,
        *,
        route_cfg: _ResolvedRouteConfig,
        category: NotificationRouteCategory,
        is_critical: bool,
        session: AsyncSession | None,
        fallback_factory: async_sessionmaker[AsyncSession] | None,
    ) -> list[_DeliveryTarget]:
        """将抽象路由目标规则解析为去重后的具体 Telegram 发送目标列表。"""
        if route_cfg.target_type == NotificationTargetType.DISABLED:
            return []

        targets: list[_DeliveryTarget] = []
        if route_cfg.target_type == NotificationTargetType.MAIN_CHAT:
            if settings.telegram_chat_id:
                targets.append(
                    _DeliveryTarget(
                        chat_id=settings.telegram_chat_id,
                        reply_to=None,
                        label=f"main_chat:{settings.telegram_chat_id}",
                    )
                )
        elif route_cfg.target_type == NotificationTargetType.FORUM_TOPIC:
            if route_cfg.target_topic_id:
                chat_id = route_cfg.target_chat_id or settings.telegram_chat_id
                if chat_id:
                    targets.append(
                        _DeliveryTarget(
                            chat_id=chat_id,
                            reply_to=route_cfg.target_topic_id,
                            label=f"forum_topic:{chat_id}:{route_cfg.target_topic_id}",
                        )
                    )
        elif route_cfg.target_type == NotificationTargetType.CHAT_OR_CHANNEL:
            if route_cfg.target_chat_id:
                targets.append(
                    _DeliveryTarget(
                        chat_id=route_cfg.target_chat_id,
                        reply_to=None,
                        label=f"chat:{route_cfg.target_chat_id}",
                    )
                )
        elif route_cfg.target_type == NotificationTargetType.ADMIN_DM_GROUP:
            for admin_id in await self._resolve_admin_ids(
                session=session, fallback_factory=fallback_factory
            ):
                targets.append(
                    _DeliveryTarget(
                        chat_id=admin_id,
                        reply_to=None,
                        label=f"admin_dm:{admin_id}",
                    )
                )

        if (
            category == NotificationRouteCategory.OPS_ALERT
            and is_critical
            and route_cfg.escalate_admin_dm
            and route_cfg.target_type != NotificationTargetType.ADMIN_DM_GROUP
        ):
            for admin_id in await self._resolve_admin_ids(
                session=session, fallback_factory=fallback_factory
            ):
                targets.append(
                    _DeliveryTarget(
                        chat_id=admin_id,
                        reply_to=None,
                        label=f"admin_dm:{admin_id}",
                    )
                )

        # 按 (chat_id, reply_to) 去重，防止管理员私聊与主目标重复发送
        deduped: list[_DeliveryTarget] = []
        seen: set[tuple[int | str, int | None]] = set()
        for target in targets:
            key = (target.chat_id, target.reply_to)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(target)
        return deduped

    async def emit(
        self,
        payload: NotificationPayload,
        *,
        user_id: int | None = None,
        server_id: int | None = None,
        source_key: str | None = None,
        skip_inbox_fallback: bool = False,
        route_category_override: NotificationRouteCategory | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """统一异步非阻塞发射强类型通知事件。

        Args:
            payload: 强类型通知载荷对象。
            user_id: 目标 Telegram 用户 ID（私聊与双发事件使用）。
            server_id: 关联媒体/Arr 服务器实例 ID，用于解析单服覆盖路由。
            source_key: 可选同源冷却去重键。
            skip_inbox_fallback: 为 ``True`` 时私聊失败后不写入站内兜底收件箱。
            route_category_override: 可选路由类别覆盖（如 ``SYSTEM_TEST`` 定向实测）。
            session: 可选异步数据库只读会话（仅在入队前同步检查阶段读取，不跨协程复用）。

        Returns:
            成功接收入队至少一个投递任务返回 ``True``；被偏好/路由/冷却抑制或无目标返回 ``False``。
        """
        spec = get_event_spec(payload.event)
        category = route_category_override or spec.category
        rendered = NotificationRenderer.render(payload)
        derived_factory = self._derive_session_factory(session)
        now = self.runtime.clock()
        effective_source = (
            source_key.strip()
            if isinstance(source_key, str) and source_key.strip()
            else (str(server_id) if server_id is not None else "global")
        )
        payload_server_name = getattr(payload, "server_name", None)

        if spec.event in _RECOVERY_CLEAR_MAP:
            self._clear_recovery_cooldown(
                recovery_event=spec.event,
                effective_source=effective_source,
                server_id=server_id,
                server_name=payload_server_name,
                now=now,
            )

        if spec.supports_cooldown:
            in_cooldown = await self._is_ops_in_cooldown(
                event=spec.event,
                effective_source=effective_source,
                now=now,
                session=session,
                fallback_factory=derived_factory,
            )
            if in_cooldown:
                return False
            self._mark_ops_cooldown(
                event=spec.event,
                effective_source=effective_source,
                server_id=server_id,
                server_name=payload_server_name,
                now=now,
            )

        enqueued = False
        if spec.audience in (
            NotificationAudience.TOPIC,
            NotificationAudience.OPS,
            NotificationAudience.BOTH,
        ):
            route_cfg = await self._resolve_route_config(
                category=category,
                server_id=server_id,
                session=session,
                fallback_factory=derived_factory,
            )
            if spec.default_enabled or spec.event.value in route_cfg.enabled_events:
                targets = await self._resolve_route_targets(
                    route_cfg=route_cfg,
                    category=category,
                    is_critical=(spec.severity.value == "critical"),
                    session=session,
                    fallback_factory=derived_factory,
                )
                is_ops_log = category == NotificationRouteCategory.OPS_ALERT
                if targets:
                    ops_batch = (
                        _OpsAlertBatch(
                            source_key=effective_source,
                            server_id=server_id,
                            event_type=spec.event.value,
                            severity=spec.severity.value,
                            title=rendered.title,
                            body_text=rendered.plain_summary,
                            created_at=now,
                            remaining=len(targets),
                            fallback_session_factory=derived_factory,
                        )
                        if is_ops_log
                        else None
                    )
                    for target in targets:
                        self.runtime.enqueue(
                            _DeliveryJob(
                                event=spec.event,
                                category=category,
                                severity=spec.severity.value,
                                is_mandatory=spec.is_mandatory,
                                rendered=rendered,
                                target=target,
                                user_id=None,
                                server_id=server_id,
                                source_key=effective_source,
                                skip_inbox_fallback=True,
                                created_at=now,
                                fallback_session_factory=derived_factory,
                                ops_batch=ops_batch,
                            )
                        )
                        enqueued = True
                elif is_ops_log:
                    # 路由禁用或无可用目标时仍异步记录运维告警，保证管理审计可查
                    status_override = (
                        "suppressed_disabled"
                        if route_cfg.target_type == NotificationTargetType.DISABLED
                        else "no_targets"
                    )
                    ops_batch = _OpsAlertBatch(
                        source_key=effective_source,
                        server_id=server_id,
                        event_type=spec.event.value,
                        severity=spec.severity.value,
                        title=rendered.title,
                        body_text=rendered.plain_summary,
                        created_at=now,
                        remaining=0,
                        fallback_session_factory=derived_factory,
                    )
                    self.runtime.enqueue(
                        _DeliveryJob(
                            event=spec.event,
                            category=category,
                            severity=spec.severity.value,
                            is_mandatory=spec.is_mandatory,
                            rendered=rendered,
                            target=None,
                            user_id=None,
                            server_id=server_id,
                            source_key=effective_source,
                            skip_inbox_fallback=True,
                            created_at=now,
                            fallback_session_factory=derived_factory,
                            ops_batch=ops_batch,
                            ops_log_status_override=status_override,
                        )
                    )

        if spec.audience in (NotificationAudience.USER_DM, NotificationAudience.BOTH):
            effective_uid = (
                user_id
                if user_id is not None
                else getattr(payload, "target_user_id", None)
            )
            if isinstance(effective_uid, int) and effective_uid > 0:
                dm_allowed = await self._is_user_dm_allowed(
                    user_id=effective_uid,
                    is_mandatory=spec.is_mandatory,
                    preference_key=spec.preference_key,
                    default_enabled=spec.default_enabled,
                    session=session,
                    fallback_factory=derived_factory,
                )
                if dm_allowed:
                    self.runtime.enqueue(
                        _DeliveryJob(
                            event=spec.event,
                            category=category,
                            severity=spec.severity.value,
                            is_mandatory=spec.is_mandatory,
                            rendered=rendered,
                            target=_DeliveryTarget(
                                chat_id=effective_uid,
                                reply_to=None,
                                label=f"user_dm:{effective_uid}",
                                is_user_dm=True,
                            ),
                            user_id=effective_uid,
                            server_id=server_id,
                            source_key=effective_source,
                            skip_inbox_fallback=skip_inbox_fallback,
                            created_at=now,
                            fallback_session_factory=derived_factory,
                        )
                    )
                    enqueued = True

        return enqueued

    async def _is_user_dm_allowed(
        self,
        *,
        user_id: int,
        is_mandatory: bool,
        preference_key: str | None,
        default_enabled: bool,
        session: AsyncSession | None,
        fallback_factory: async_sessionmaker[AsyncSession] | None,
    ) -> bool:
        """校验非必达私聊事件在目标用户的通知偏好中是否开启；必达事件始终放行。"""
        if is_mandatory or preference_key is None:
            return True
        if session is not None:
            try:
                return await UserNotificationPreferenceRepository(
                    session
                ).is_event_enabled(
                    user_id, preference_key, default_enabled=default_enabled
                )
            except (SQLAlchemyError, ValueError) as exc:
                logger.warning("查询用户通知偏好失败，回退默认开关: {}", exc)
                return default_enabled

        factory = self.runtime.resolve_session_factory(fallback_factory)
        if factory is None:
            return default_enabled
        try:
            async with self.runtime._db_lock, factory() as db:
                return await UserNotificationPreferenceRepository(db).is_event_enabled(
                    user_id, preference_key, default_enabled=default_enabled
                )
        except (SQLAlchemyError, ValueError) as exc:
            logger.warning("查询用户通知偏好失败，回退默认开关: {}", exc)
            return default_enabled

    def _clear_recovery_cooldown(
        self,
        *,
        recovery_event: NotificationEvent,
        effective_source: str,
        server_id: int | None,
        server_name: str | None,
        now: datetime,
    ) -> None:
        """恢复类通知到达时清除匹配的异常告警冷却窗口，使新故障能立即触达。"""
        target_error_event = _RECOVERY_CLEAR_MAP[recovery_event]
        target_value = target_error_event.value

        matched_keys: list[str] = []
        for key, entry in list(self.runtime._cooldown_entries.items()):
            if entry.event_value != target_value:
                continue
            if (
                entry.source_key == effective_source
                or (server_id is not None and entry.server_id == server_id)
                or (server_name and entry.server_name == server_name)
            ):
                matched_keys.append(key)

        for key in matched_keys:
            self.runtime._cooldown_entries.pop(key, None)
            self.runtime._cooldown_cleared_at[key] = now

        exact_key = f"{target_value}:{effective_source}"
        self.runtime._cooldown_entries.pop(exact_key, None)
        self.runtime._cooldown_cleared_at[exact_key] = now
        if server_id is not None:
            server_key = f"{target_value}:{server_id}"
            self.runtime._cooldown_entries.pop(server_key, None)
            self.runtime._cooldown_cleared_at[server_key] = now

    async def _is_ops_in_cooldown(
        self,
        *,
        event: NotificationEvent,
        effective_source: str,
        now: datetime,
        session: AsyncSession | None,
        fallback_factory: async_sessionmaker[AsyncSession] | None,
    ) -> bool:
        """结合内存缓存与 OpsAlertLog 持久化记录判定同源告警是否处于 30 分钟冷却期。"""
        cooldown_key = f"{event.value}:{effective_source}"
        cached = self.runtime._cooldown_entries.get(cooldown_key)
        if cached is not None:
            if now <= cached.expires_at:
                return True
            self.runtime._cooldown_entries.pop(cooldown_key, None)

        cleared_at = self.runtime._cooldown_cleared_at.get(cooldown_key)
        cooldown_since = now - self.runtime.cooldown_window

        async def _query_db(db: AsyncSession) -> bool:
            repo = OpsAlertRepository(db)
            latest = await repo.get_latest_by_source(event.value, effective_source)
            if latest is None or latest.created_at < cooldown_since:
                return False
            if cleared_at is not None and latest.created_at <= cleared_at:
                return False
            paired_recovery = _ERROR_TO_RECOVERY_MAP.get(event)
            if paired_recovery is not None:
                latest_rec = await repo.get_latest_by_source(
                    paired_recovery.value, effective_source
                )
                if (
                    latest_rec is not None
                    and latest_rec.created_at >= latest.created_at
                ):
                    return False
            # 回填内存缓存，避免后续并发事件重复查库
            self.runtime._cooldown_entries[cooldown_key] = _CooldownEntry(
                expires_at=latest.created_at + self.runtime.cooldown_window,
                event_value=event.value,
                source_key=effective_source,
                server_id=latest.server_id,
                server_name=None,
            )
            return True

        if session is not None:
            try:
                return await _query_db(session)
            except SQLAlchemyError as exc:
                logger.warning("校验运维告警冷却窗口失败: {}", exc)
                return False

        factory = self.runtime.resolve_session_factory(fallback_factory)
        if factory is None:
            return False
        try:
            async with self.runtime._db_lock, factory() as db:
                return await _query_db(db)
        except SQLAlchemyError as exc:
            logger.warning("校验运维告警冷却窗口失败: {}", exc)
            return False

    def _mark_ops_cooldown(
        self,
        *,
        event: NotificationEvent,
        effective_source: str,
        server_id: int | None,
        server_name: str | None,
        now: datetime,
    ) -> None:
        """登记内存运维冷却条目并顺便清理过期旧条目。"""
        cooldown_key = f"{event.value}:{effective_source}"
        self.runtime._cooldown_entries[cooldown_key] = _CooldownEntry(
            expires_at=now + self.runtime.cooldown_window,
            event_value=event.value,
            source_key=effective_source,
            server_id=server_id,
            server_name=server_name,
        )
        # 防止长期运行内存无限增长：移除已过期的冷却键
        if len(self.runtime._cooldown_entries) > 256:
            expired = [
                k
                for k, v in self.runtime._cooldown_entries.items()
                if now > v.expires_at
            ]
            for k in expired:
                self.runtime._cooldown_entries.pop(k, None)

    async def _send_targets_sync_detailed(
        self,
        *,
        rendered: RenderedNotification,
        targets: Sequence[_DeliveryTarget],
    ) -> tuple[Any, list[str], list[str]]:
        """同步向已解析目标列表投递消息，并返回首个消息、成功目标列表与错误信息列表。"""
        client = self.runtime.get_telethon_client()
        if client is None:
            return None, [], ["Telethon 客户端不可用"]
        if not targets:
            return None, [], []

        first_sent: Any = None
        delivered_targets: list[str] = []
        send_errors: list[str] = []
        for target in targets:
            try:
                sent = await self.runtime.send_with_chat_pacing(
                    client,
                    chat_id=target.chat_id,
                    message=rendered.telegram_html,
                    file=rendered.image,
                    buttons=rendered.buttons,
                    reply_to=target.reply_to,
                )
                delivered_targets.append(target.label)
                if first_sent is None and sent is not None:
                    first_sent = sent
            except (RPCError, ValueError, TypeError, OSError) as exc:
                logger.error(
                    "同步通知投递失败: event={}, target={}, err={}",
                    rendered.event.value,
                    target.label,
                    exc,
                )
                send_errors.append(f"{target.label}: {exc}")
        return first_sent, delivered_targets, send_errors

    async def _send_targets_sync(
        self,
        *,
        rendered: RenderedNotification,
        targets: Sequence[_DeliveryTarget],
    ) -> Any:
        """同步向已解析目标列表投递消息并返回首个成功发送的 Telethon Message。"""
        first_sent, _, _ = await self._send_targets_sync_detailed(
            rendered=rendered, targets=targets
        )
        return first_sent

    async def emit_sync(
        self,
        payload: NotificationPayload,
        *,
        server_id: int | None = None,
        route_category_override: NotificationRouteCategory | None = None,
        session: AsyncSession | None = None,
    ) -> Any:
        """同步直投强类型通知并返回首个成功发送的 Telethon ``Message``（供 ``REQUEST_SUBMIT`` 等同步取回消息 ID）。

        Args:
            payload: 强类型通知载荷对象。
            server_id: 关联服务器实例 ID。
            route_category_override: 可选路由类别覆盖（如 ``SYSTEM_TEST``）。
            session: 可选异步数据库会话。

        Returns:
            成功投递时返回 Telethon ``Message``，被抑制、无客户端或发送失败返回 ``None``。
        """
        spec = get_event_spec(payload.event)
        category = route_category_override or spec.category
        rendered = NotificationRenderer.render(payload)
        derived_factory = self._derive_session_factory(session)
        route_cfg = await self._resolve_route_config(
            category=category,
            server_id=server_id,
            session=session,
            fallback_factory=derived_factory,
        )
        if (
            not spec.default_enabled
            and spec.event.value not in route_cfg.enabled_events
        ):
            return None
        targets = await self._resolve_route_targets(
            route_cfg=route_cfg,
            category=category,
            is_critical=(spec.severity.value == "critical"),
            session=session,
            fallback_factory=derived_factory,
        )
        return await self._send_targets_sync(rendered=rendered, targets=targets)

    async def has_route_targets(
        self,
        event: NotificationEvent,
        *,
        server_id: int | None = None,
        route_category_override: NotificationRouteCategory | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """判断指定事件与服务器在当前路由策略下是否存在至少一个有效出站目标。"""
        spec = get_event_spec(event)
        category = route_category_override or spec.category
        derived_factory = self._derive_session_factory(session)
        route_cfg = await self._resolve_route_config(
            category=category,
            server_id=server_id,
            session=session,
            fallback_factory=derived_factory,
        )
        if (
            not spec.default_enabled
            and spec.event.value not in route_cfg.enabled_events
        ):
            return False
        targets = await self._resolve_route_targets(
            route_cfg=route_cfg,
            category=category,
            is_critical=(spec.severity.value == "critical"),
            session=session,
            fallback_factory=derived_factory,
        )
        return bool(targets)

    async def resolve_primary_target_chat_id(
        self,
        *,
        category: NotificationRouteCategory,
        server_id: int | None = None,
        session: AsyncSession | None = None,
    ) -> int | str | None:
        """解析指定路由类别与服务器的主投递目标 ``chat_id``，用于同步编辑既有消息卡片。"""
        derived_factory = self._derive_session_factory(session)
        route_cfg = await self._resolve_route_config(
            category=category,
            server_id=server_id,
            session=session,
            fallback_factory=derived_factory,
        )
        targets = await self._resolve_route_targets(
            route_cfg=route_cfg,
            category=category,
            is_critical=False,
            session=session,
            fallback_factory=derived_factory,
        )
        return targets[0].chat_id if targets else None

    async def edit_route_message(
        self,
        *,
        category: NotificationRouteCategory,
        message_id: int,
        new_content: str,
        server_id: int | None = None,
        clear_buttons: bool = True,
        parse_mode: str | None = "html",
        session: AsyncSession | None = None,
    ) -> bool:
        """在对应路由主目标会话中非致命编辑既有通知消息（如同步更新求片审核卡片状态）。

        Args:
            category: 通知路由类别。
            message_id: 原 Telegram 消息 ID。
            new_content: 更新后的 HTML 或文本内容。
            server_id: 关联服务器实例 ID。
            clear_buttons: 是否清空原消息内联按钮，默认 ``True``。
            parse_mode: Telegram 解析模式，默认 ``"html"``。
            session: 可选异步数据库会话。

        Returns:
            编辑成功返回 ``True``；无客户端、无目标或 Telegram RPC 异常时记录日志并返回 ``False``。
        """
        if not message_id:
            return False
        client = self.runtime.get_telethon_client()
        if client is None:
            return False
        edit_fn = getattr(client, "edit_message", None)
        if not callable(edit_fn):
            return False
        chat_id = await self.resolve_primary_target_chat_id(
            category=category,
            server_id=server_id,
            session=session,
        )
        if chat_id is None:
            return False
        try:
            res = edit_fn(
                chat_id,
                message_id,
                new_content,
                clear_buttons=clear_buttons,
                parse_mode=parse_mode,
            )
            if inspect.isawaitable(res):
                await res
            return True
        except (RPCError, ValueError, TypeError, OSError, AttributeError) as exc:
            logger.warning(
                "编辑通知卡片失败（忽略以保护主事务）: category={}, server_id={}, msg_id={}, err={}",
                category.value,
                server_id,
                message_id,
                exc,
            )
            return False

    async def send_test_to_route(
        self,
        *,
        category: NotificationRouteCategory | str,
        server_id: int | None = None,
        server_name: str = "系统全局",
        target_type: NotificationTargetType | str | None = None,
        target_chat_id: int | None = None,
        target_topic_id: int | None = None,
        escalate_admin_dm: bool = False,
        operator_name: str = "",
        target_description: str = "",
        message: str = "这是一条系统通知通路测试消息。",
        session: AsyncSession | None = None,
    ) -> Any:
        """同步发送 ``SYSTEM_TEST`` 通路测试消息到持久化路由或指定候选目标配置，并记录投递报告。"""
        normalized_cat = NotificationRouteCategory(category)
        derived_factory = self._derive_session_factory(session)
        if target_type is None:
            route_cfg = await self._resolve_route_config(
                category=normalized_cat,
                server_id=server_id,
                session=session,
                fallback_factory=derived_factory,
            )
        else:
            route_cfg = _ResolvedRouteConfig(
                target_type=NotificationTargetType(target_type),
                target_chat_id=target_chat_id,
                target_topic_id=target_topic_id,
                escalate_admin_dm=bool(escalate_admin_dm),
                enabled_events=frozenset(),
            )

        effective_desc = target_description or route_cfg.target_type.value
        payload = SystemTestPayload(
            server_name=server_name,
            category=normalized_cat.value,
            target_description=effective_desc,
            operator_name=operator_name,
            triggered_at=self.runtime.clock(),
            message=message,
        )
        if route_cfg.target_type == NotificationTargetType.DISABLED:
            self.last_test_report = NotificationTestReport(
                delivered_targets=[],
                errors=["当前路由目标处于禁用状态 (disabled)"],
                disabled=True,
            )
            return None

        targets = await self._resolve_route_targets(
            route_cfg=route_cfg,
            category=normalized_cat,
            is_critical=bool(route_cfg.escalate_admin_dm),
            session=session,
            fallback_factory=derived_factory,
        )
        if not targets:
            self.last_test_report = NotificationTestReport(
                delivered_targets=[],
                errors=[
                    "未解析到可用的 Telegram 投递目标（请检查主群 ID、话题或管理员配置）"
                ],
                disabled=False,
            )
            return None

        rendered = NotificationRenderer.render(payload)
        first_sent, delivered, send_errors = await self._send_targets_sync_detailed(
            rendered=rendered, targets=targets
        )
        self.last_test_report = NotificationTestReport(
            delivered_targets=delivered,
            errors=send_errors,
            disabled=False,
        )
        return first_sent

    async def deliver_direct_dm(
        self,
        *,
        user_id: int,
        message: str,
        parse_mode: str | None = "html",
        buttons: Sequence[Any] | None = None,
        file: Any = None,
        client: TelethonClientWarper | Any | None = None,
        skip_inbox_fallback: bool = False,
        fallback_payload: NotificationPayload | None = None,
        server_id: int | None = None,
        session: AsyncSession | None = None,
    ) -> tuple[bool, str | None]:
        """同步向指定用户发送私聊消息，统一经由共享节流管线投递并按需执行脱敏站内兜底。

        Args:
            user_id: 目标 Telegram 用户 ID。
            message: 已渲染完成的待发送消息正文。
            parse_mode: Telegram 解析模式（默认 ``"html"``，敏感凭据或旧式交互卡片可传 ``"markdown"`` / ``"md"``）。
            buttons: 可选内联按钮列表。
            file: 可选附件或海报媒体。
            client: 可选显式传入的 Telethon 客户端；为空时回退 ``runtime.get_telethon_client()``。
            skip_inbox_fallback: 为 ``True`` 时私聊失败后禁止写入站内兜底收件箱（如同步弹窗已当场返回凭据）。
            fallback_payload: 允许站内兜底时用于生成脱敏收件箱摘要的强类型载荷。
            server_id: 关联服务器实例 ID。
            session: 可选异步数据库会话，用于派生兜底落库连接工厂。

        Returns:
            ``(delivered, error_message)`` 二元组；投递成功时 ``error_message`` 为 ``None``。
        """
        active_client = (
            client if client is not None else self.runtime.get_telethon_client()
        )
        if active_client is None:
            err_msg = "Telethon client unavailable"
            await self._maybe_persist_direct_dm_fallback(
                user_id=user_id,
                server_id=server_id,
                skip_inbox_fallback=skip_inbox_fallback,
                fallback_payload=fallback_payload,
                session=session,
                error_message=err_msg,
            )
            return False, err_msg

        try:
            await self.runtime.send_with_chat_pacing(
                active_client,
                chat_id=user_id,
                message=message,
                file=file,
                buttons=buttons,
                parse_mode=parse_mode,
            )
            return True, None
        except (RPCError, ValueError, TypeError, OSError) as exc:
            err_msg = str(exc) or type(exc).__name__
            await self._maybe_persist_direct_dm_fallback(
                user_id=user_id,
                server_id=server_id,
                skip_inbox_fallback=skip_inbox_fallback,
                fallback_payload=fallback_payload,
                session=session,
                error_message=err_msg,
            )
            return False, err_msg

    async def _maybe_persist_direct_dm_fallback(
        self,
        *,
        user_id: int,
        server_id: int | None,
        skip_inbox_fallback: bool,
        fallback_payload: NotificationPayload | None,
        session: AsyncSession | None,
        error_message: str,
    ) -> None:
        """当同步私聊失败且未跳过站内兜底时，将脱敏载荷写入站内收件箱。"""
        if skip_inbox_fallback or fallback_payload is None:
            return
        spec = get_event_spec(fallback_payload.event)
        rendered = NotificationRenderer.render(fallback_payload)
        job = _DeliveryJob(
            event=spec.event,
            category=spec.category,
            severity=spec.severity.value,
            is_mandatory=spec.is_mandatory,
            rendered=rendered,
            target=_DeliveryTarget(
                chat_id=user_id,
                reply_to=None,
                label=f"user_dm:{user_id}",
                is_user_dm=True,
            ),
            user_id=user_id,
            server_id=server_id,
            source_key=f"direct_dm:{user_id}",
            skip_inbox_fallback=False,
            created_at=self.runtime.clock(),
            fallback_session_factory=self._derive_session_factory(session),
        )
        await self.runtime._persist_inbox_fallback(job, error_message=error_message)

    # ------------------------------------------------------------------
    # 兼容层：将旧字典上下文委托给强类型渲染器并经统一限流管线发送
    # ------------------------------------------------------------------

    def _build_compat_payload(
        self, event_type: NotificationEvent, context: dict[str, Any]
    ) -> NotificationPayload | None:
        """将旧版字典上下文映射为强类型载荷对象，供兼容渲染路径复用统一渲染器。"""
        server_name = str(context.get("server_name", "") or "")

        if event_type == NotificationEvent.REQUEST_SUBMIT:
            prefix = str(context.get("prefix", "") or "TMDB")
            return RequestSubmitPayload(
                server_name=server_name,
                user_id=int(context.get("user_id") or 0),
                user_name=str(context.get("user_name", "") or ""),
                media_title=str(context.get("media_title", "") or ""),
                media_year=context.get("media_year"),
                media_type=prefix,
                media_id=context.get("tmdb_id", ""),
                overview=context.get("overview"),
            )

        if event_type == NotificationEvent.REQUEST_APPROVED:
            return RequestApprovedPayload(
                server_name=server_name,
                media_title=str(context.get("media_title", "") or ""),
                media_year=context.get("media_year"),
            )

        if event_type == NotificationEvent.REQUEST_REJECTED:
            return RequestRejectedPayload(
                server_name=server_name,
                media_title=str(context.get("media_title", "") or ""),
                media_year=context.get("media_year"),
                reason=str(context.get("reason", "") or ""),
            )

        if event_type == NotificationEvent.REQUEST_COMPLETED:
            return RequestCompletedPayload(
                server_name=server_name,
                media_title=str(context.get("media_title", "") or ""),
                media_year=context.get("media_year"),
            )

        if event_type == NotificationEvent.SONARR_DOWNLOAD:
            series = context.get("series")
            episodes = [
                EpisodeDetail(
                    season_number=int(
                        _get_field(
                            ep, "seasonNumber", _get_field(ep, "season_number", 0)
                        )
                        or 0
                    ),
                    episode_number=int(
                        _get_field(
                            ep, "episodeNumber", _get_field(ep, "episode_number", 0)
                        )
                        or 0
                    ),
                    title=str(_get_field(ep, "title", "") or ""),
                )
                for ep in (context.get("episodes") or [])
            ]
            cf_score, cf_names = _extract_compat_custom_format(
                context.get("customFormatInfo")
            )
            return SonarrDownloadPayload(
                server_name=server_name,
                series_title=str(_get_field(series, "title", "") or ""),
                episodes=episodes,
                quality=str(
                    _get_field(context.get("episodeFile"), "quality", "") or ""
                ),
                custom_format_score=cf_score,
                custom_formats=cf_names,
                is_upgrade=bool(context.get("isUpgrade")),
            )

        if event_type == NotificationEvent.SONARR_SERIES_ADD:
            series = context.get("series")
            genres = [str(g) for g in (_get_field(series, "genres") or [])]
            return SonarrSeriesAddPayload(
                server_name=server_name,
                title=str(_get_field(series, "title", "") or ""),
                year=_get_field(series, "year"),
                genres=genres,
                path=str(_get_field(series, "path", "") or ""),
            )

        if event_type == NotificationEvent.RADARR_DOWNLOAD:
            movie = context.get("movie")
            cf_score, cf_names = _extract_compat_custom_format(
                context.get("customFormatInfo")
            )
            return RadarrDownloadPayload(
                server_name=server_name,
                title=str(_get_field(movie, "title", "") or ""),
                year=_get_field(movie, "year"),
                quality=str(_get_field(context.get("movieFile"), "quality", "") or ""),
                custom_format_score=cf_score,
                custom_formats=cf_names,
                is_upgrade=bool(context.get("isUpgrade")),
                overview=_get_field(movie, "overview"),
            )

        if event_type == NotificationEvent.RADARR_MOVIE_ADD:
            movie = context.get("movie")
            genres = [str(g) for g in (_get_field(movie, "genres") or [])]
            return RadarrMovieAddPayload(
                server_name=server_name,
                title=str(_get_field(movie, "title", "") or ""),
                year=_get_field(movie, "year"),
                genres=genres,
                overview=_get_field(movie, "overview"),
                tmdb_id=_get_field(movie, "tmdbId"),
            )

        if event_type == NotificationEvent.EMBY_LIBRARY_NEW:
            item = context.get("item")
            return EmbyLibraryNewPayload(
                server_name=server_name,
                name=str(_get_field(item, "name", "") or ""),
                series_name=_get_field(item, "series_name"),
                year=_get_field(item, "production_year"),
                item_type=str(_get_field(item, "type", "") or ""),
                overview=_get_field(item, "overview"),
            )

        if event_type == NotificationEvent.JELLYFIN_LIBRARY_NEW:
            item = context.get("item")
            fallback_name = item if isinstance(item, str) else ""
            return JellyfinLibraryNewPayload(
                server_name=server_name,
                item=item if isinstance(item, JellyfinItemEvent) else None,
                name=str(_get_field(item, "name", fallback_name) or ""),
                series_name=_get_field(item, "series_name"),
                year=_get_field(item, "year"),
                item_type=str(_get_field(item, "item_type", "") or ""),
                overview=_get_field(item, "overview"),
            )

        if event_type == NotificationEvent.SYSTEM_TEST:
            return SystemTestPayload(server_name=server_name)

        return None

    async def _render(
        self, event_type: NotificationEvent, context: dict[str, Any]
    ) -> str | None:
        """兼容方法：将既有字典上下文委托至 ``NotificationRenderer`` 生成安全转义的 Telegram HTML。"""
        payload = self._build_compat_payload(event_type, context)
        if payload is None:
            return None
        return NotificationRenderer.render(payload).telegram_html

    async def send_to_topic(
        self,
        topic_id: int,
        event_type: NotificationEvent,
        image: Any = None,
        buttons: list | None = None,
        **kwargs: Any,
    ) -> Any:
        """兼容方法：通过统一限流管线发送通知事件到指定的 Topic 或 Channel。"""
        client = self.runtime.get_telethon_client()
        if not topic_id or client is None:
            return None

        context = kwargs.copy()
        context["event_type"] = event_type.value
        message_text = await self._render(event_type, context)
        if not message_text:
            return None

        target_chat = settings.telegram_chat_id
        target_reply = None
        if topic_id < 0:
            target_chat = topic_id
        elif topic_id > 0:
            target_reply = topic_id

        try:
            sent_msg = await self.runtime.send_with_chat_pacing(
                client,
                chat_id=target_chat,
                message=message_text,
                file=image,
                buttons=buttons,
                reply_to=target_reply,
                parse_mode="html",
            )
            logger.info("已发送通知：{}（目标：{}）", event_type.value, topic_id)
            return sent_msg
        except RPCError as e:
            logger.error("Telegram API 错误: {}", e)
            return None
        except (ValueError, TypeError, OSError) as e:
            logger.error("无法发送通知 ({})：{}", event_type.value, e)
            return None

    async def send_to_user(
        self,
        user_id: int,
        event_type: NotificationEvent,
        image: Any = None,
        **kwargs: Any,
    ) -> bool:
        """兼容方法：向特定用户私聊发送通知事件，返回是否发送成功。"""
        client = self.runtime.get_telethon_client()
        if client is None:
            return False

        message_text = await self._render(event_type, kwargs)
        if not message_text:
            logger.warning(
                "用户私聊通知渲染结果为空: user_id={}, event={}",
                user_id,
                event_type.value,
            )
            return False

        ok, err = await self.deliver_direct_dm(
            user_id=user_id,
            message=message_text,
            file=image,
            parse_mode="html",
            client=client,
            skip_inbox_fallback=True,
        )
        if ok:
            logger.debug("发送的用户通知：{} -> 用户 {}", event_type.value, user_id)
        else:
            logger.error(
                "无法向 {} 发送用户通知 ({})：{}", user_id, event_type.value, err
            )
        return ok

    async def send_text_to_user(
        self, user_id: int, message_text: str, image: Any = None
    ) -> bool:
        """兼容方法：通过统一限流管线发送自定义文本通知给特定用户 (私聊)，返回是否发送成功。"""
        client = self.runtime.get_telethon_client()
        if not message_text or client is None:
            return False

        ok, err = await self.deliver_direct_dm(
            user_id=user_id,
            message=message_text,
            file=image,
            parse_mode="html",
            client=client,
            skip_inbox_fallback=True,
        )
        if ok:
            logger.debug("发送的用户文本通知 -> 用户 {}", user_id)
        else:
            logger.error("无法向 {} 发送用户文本通知：{}", user_id, err)
        return ok


def attach_external_client_hooks(
    client: Any,
    *,
    app: FastAPI | Any,
    server_id: int,
    server_name: str,
    server_type: str,
) -> Any:
    """为外部媒体/Arr/qB 客户端统一挂载异常冷却告警与恢复自愈通知钩子。

    Args:
        client: 继承自 ``BaseClient`` 的外部 HTTP 客户端实例。
        app: FastAPI 应用实例或携带 ``state`` 的替身对象。
        server_id: 数据库服务器实例 ID。
        server_name: 服务器显示名称。
        server_type: 服务器类型字符串（如 ``emby`` / ``qbittorrent``）。

    Returns:
        已绑定钩子的原始 ``client`` 对象。
    """
    set_hooks = getattr(client, "set_request_hooks", None)
    if set_hooks is None:
        return client

    source_key = f"client:{server_type}:{server_id}"

    async def _on_error(
        operation: str, status_code: int | None, error_message: str
    ) -> None:
        """外部客户端请求异常时向统一管线发射带同源冷却的告警事件。"""
        await NotificationService(app).emit(
            ExternalServerErrorPayload(
                server_name=server_name,
                server_type=server_type,
                operation=operation,
                status_code=status_code,
                error_message=error_message,
            ),
            server_id=server_id,
            source_key=source_key,
        )

    async def _on_recovered(operation: str) -> None:
        """外部客户端故障自愈后发射恢复事件并解除同源告警冷却。"""
        await NotificationService(app).emit(
            ExternalServerRecoveredPayload(
                server_name=server_name,
                server_type=server_type,
                operation=operation,
                detail="外部服务通信已恢复正常",
            ),
            server_id=server_id,
            source_key=source_key,
        )

    set_hooks(on_request_error=_on_error, on_request_recovered=_on_recovered)
    return client


async def cleanup_expired_notification_records(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    retention_days: int = 30,
) -> tuple[int, int]:
    """清理超过保留天数（默认 30 天）的站内兜底收件箱记录与运维告警日志。

    Args:
        session: 异步数据库会话。
        now: 可选当前时间基准（用于测试确定性控制）。
        retention_days: 保留天数（默认 30 天）。

    Returns:
        ``(deleted_inbox_count, deleted_ops_count)`` 清理计数元组。
    """
    cutoff = (now or datetime.now()) - timedelta(days=max(1, int(retention_days)))
    inbox_deleted = await NotificationInboxRepository(session).delete_expired_records(
        cutoff
    )
    ops_deleted = await OpsAlertRepository(session).delete_expired_records(cutoff)
    return inbox_deleted, ops_deleted
