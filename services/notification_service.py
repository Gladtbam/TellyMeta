"""全域统一通知路由器、异步非阻塞投递管线与站内兜底服务"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
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
from core.telegram_message import TelegramMessage
from models.events import (
    DEFAULT_GLOBAL_ROUTE_TARGETS,
    NotificationAudience,
    NotificationEvent,
    NotificationRouteCategory,
    NotificationTargetType,
    get_event_spec,
)
from repositories.notification_repo import (
    NotificationInboxRepository,
    NotificationRouteRepository,
    OpsAlertRepository,
    UserNotificationPreferenceRepository,
)
from repositories.telegram_repo import TelegramRepository
from services.notification_renderer import (
    NotificationPayload,
    NotificationRenderer,
    RenderedNotification,
    SystemTestPayload,
)

settings = get_settings()
_ORIG_DB_ASYNC_SESSION = _db_module.async_session
async_session = _db_module.async_session

# 收敛兼容期旧模板渲染产生的多余空行
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

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


def _truncate_text(value: Any, limit: int) -> str:
    """将文本安全裁剪至指定长度并附加省略号。"""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _collapse_blank_lines(text: str) -> str:
    """收敛连续三个及以上的换行符为单个空行并去除首尾空白。"""
    return _MULTI_NEWLINE_RE.sub("\n\n", text).strip()


def _render_custom_format_html(cf_info: Any) -> str:
    """将 Arr 自定义格式评分与命中名称列表安全渲染为 HTML 片段。"""
    if not cf_info:
        return ""
    lines: list[str] = []
    score = _get_field(cf_info, "customFormatScore")
    if score is not None and score != 0:
        lines.append(TelegramMessage.render_html(t"分数: <b>{score}</b>"))
    formats = _get_field(cf_info, "customFormats") or []
    if formats:
        chips = " ".join(
            TelegramMessage.render_html(t"<code>{_get_field(cf, 'name', cf)}</code>")
            for cf in formats
        )
        lines.append(f"格式: {chips}")
    return "\n".join(lines)


def _render_library_new_html(
    *,
    server_name: Any,
    series_name: Any,
    name: Any,
    year: Any,
    media_type: Any,
    overview: Any,
) -> str:
    """统一渲染 Emby / Jellyfin 媒体入库通知的 Telegram HTML。"""
    series_line = (
        TelegramMessage.render_html(t"剧集: <b>{series_name}</b>")
        if series_name
        else ""
    )
    year_part = TelegramMessage.render_html(t" ({year})") if year else ""
    title_line = (
        TelegramMessage.render_html(t"标题: <b>{name}</b>{year_part:raw}")
        if name
        else ""
    )
    type_line = TelegramMessage.render_html(t"类型: {media_type}") if media_type else ""
    overview_short = _truncate_text(overview, 120)
    overview_block = (
        TelegramMessage.render_html(t"\n描述:\n{overview_short}")
        if overview_short
        else ""
    )
    body = TelegramMessage.render_html(
        t"🆕 <b>媒体库更新</b>\n\n"
        t"{series_line:raw}\n"
        t"{title_line:raw}\n"
        t"{type_line:raw}\n"
        t"{overview_block:raw}\n\n"
        t"来源: <code>{server_name}</code>\n\n"
        t"#入库"
    )
    return _collapse_blank_lines(body)


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
                    parse_mode="html",
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

    async def _handle_terminal_failure(
        self, job: _DeliveryJob, *, error_message: str
    ) -> None:
        """统一收尾不可恢复或已耗尽重试的任务：写私聊兜底、记录运维结果并释放计数。"""
        try:
            await self._persist_inbox_fallback(job, error_message=error_message)
            await self._record_ops_outcome(job, success=False, error=error_message)
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


class NotificationService:
    """统一强类型通知入口服务（整合分层路由、偏好过滤、冷却与异步投递）。"""

    def __init__(self, app: FastAPI | Any) -> None:
        self.app = app
        self.runtime = get_or_create_notification_runtime(app)

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

    async def _resolve_route_config(
        self,
        *,
        category: NotificationRouteCategory,
        server_id: int | None,
        session: AsyncSession | None,
        fallback_factory: async_sessionmaker[AsyncSession] | None,
    ) -> _ResolvedRouteConfig:
        """按单服覆盖 -> 全局默认 -> 内置兜底三级优先级解析路由规则。"""
        route = None
        if session is not None:
            try:
                route = await NotificationRouteRepository(
                    session
                ).resolve_effective_route(category, server_id)
            except SQLAlchemyError as exc:
                logger.warning("查询通知路由失败，回退默认策略: {}", exc)
        else:
            factory = self.runtime.resolve_session_factory(fallback_factory)
            if factory is not None:
                try:
                    async with self.runtime._db_lock, factory() as db:
                        route = await NotificationRouteRepository(
                            db
                        ).resolve_effective_route(category, server_id)
                except SQLAlchemyError as exc:
                    logger.warning("查询通知路由失败，回退默认策略: {}", exc)

        if route is None:
            return _ResolvedRouteConfig(
                target_type=DEFAULT_GLOBAL_ROUTE_TARGETS[category],
                target_chat_id=None,
                target_topic_id=None,
                escalate_admin_dm=False,
                enabled_events=frozenset(),
            )
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

    async def _send_targets_sync(
        self,
        *,
        rendered: RenderedNotification,
        targets: Sequence[_DeliveryTarget],
    ) -> Any:
        """同步向已解析目标列表投递消息并返回首个成功发送的 Telethon Message。"""
        client = self.runtime.get_telethon_client()
        if client is None or not targets:
            return None

        first_sent: Any = None
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
                if first_sent is None and sent is not None:
                    first_sent = sent
            except (RPCError, ValueError, TypeError, OSError) as exc:
                logger.error(
                    "同步通知投递失败: event={}, target={}, err={}",
                    rendered.event.value,
                    target.label,
                    exc,
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
        """同步发送 ``SYSTEM_TEST`` 通路测试消息到持久化路由或指定候选目标配置。"""
        normalized_cat = NotificationRouteCategory(category)
        payload = SystemTestPayload(
            server_name=server_name,
            category=normalized_cat.value,
            target_description=target_description,
            operator_name=operator_name,
            triggered_at=self.runtime.clock(),
            message=message,
        )
        if target_type is None:
            return await self.emit_sync(
                payload,
                server_id=server_id,
                route_category_override=normalized_cat,
                session=session,
            )

        derived_factory = self._derive_session_factory(session)
        route_cfg = _ResolvedRouteConfig(
            target_type=NotificationTargetType(target_type),
            target_chat_id=target_chat_id,
            target_topic_id=target_topic_id,
            escalate_admin_dm=bool(escalate_admin_dm),
            enabled_events=frozenset(),
        )
        targets = await self._resolve_route_targets(
            route_cfg=route_cfg,
            category=normalized_cat,
            is_critical=False,
            session=session,
            fallback_factory=derived_factory,
        )
        rendered = NotificationRenderer.render(payload)
        return await self._send_targets_sync(rendered=rendered, targets=targets)

    # ------------------------------------------------------------------
    # 兼容层：供 Tickets 05~07 逐步迁移存量调用点前保持安全可用
    # ------------------------------------------------------------------

    async def _render(
        self, event_type: NotificationEvent, context: dict[str, Any]
    ) -> str | None:
        """兼容方法：将既有字典上下文渲染为安全转义的 Telegram HTML。"""
        server_name = context.get("server_name", "")

        if event_type == NotificationEvent.REQUEST_SUBMIT:
            user_id = context.get("user_id", "")
            user_name = context.get("user_name", "")
            media_title = context.get("media_title", "")
            media_year = context.get("media_year", "")
            overview_short = _truncate_text(context.get("overview", ""), 100)
            prefix = context.get("prefix", "")
            tmdb_id = context.get("tmdb_id", "")
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"🎬 <b>收到新的求片请求</b>\n\n"
                    t'👤 <b>申请人</b>: <code>(<a href="tg://user?id={user_id}">{user_name}</a>)</code>\n'
                    t"🎬 <b>标题</b>: {media_title} ({media_year})\n"
                    t"🖥️ <b>服务器</b>: {server_name}\n"
                    t"📝 <b>简介</b>: {overview_short}\n\n"
                    t"{prefix}: {tmdb_id}\n\n"
                    t"#求片 #待审核"
                )
            )

        if event_type == NotificationEvent.REQUEST_APPROVED:
            media_title = context.get("media_title", "")
            media_year = context.get("media_year", "")
            return TelegramMessage.render_html(
                t"✅ <b>求片已批准</b>\n\n"
                t"您请求的 <b>{media_title}</b> ({media_year}) 已通过审核。\n"
                t"服务器: <code>{server_name}</code>\n\n"
                t"系统已自动将其加入下载队列，请耐心等待下载完成。"
            )

        if event_type == NotificationEvent.REQUEST_REJECTED:
            media_title = context.get("media_title", "")
            media_year = context.get("media_year", "")
            reason = context.get("reason", "")
            return TelegramMessage.render_html(
                t"❌ <b>求片被拒绝</b>\n\n"
                t"很抱歉，您请求的 <b>{media_title}</b> ({media_year}) 未能通过审核。\n"
                t"服务器: <code>{server_name}</code>\n\n"
                t"<b>拒绝原因:</b>\n"
                t"{reason}\n\n"
                t"如有疑问，请联系管理员。"
            )

        if event_type == NotificationEvent.REQUEST_COMPLETED:
            media_title = context.get("media_title", "")
            media_year = context.get("media_year", "")
            return TelegramMessage.render_html(
                t"🎉 <b>影片已入库！</b>\n\n"
                t"<b>{media_title}</b> ({media_year}) 现已就绪。\n"
                t"服务器: <code>{server_name}</code>\n\n"
                t"祝您观影愉快！"
            )

        if event_type == NotificationEvent.SONARR_DOWNLOAD:
            series = context.get("series")
            series_title = _get_field(series, "title", "")
            episodes = context.get("episodes") or []
            ep_lines = "\n".join(
                TelegramMessage.render_html(
                    t"单集: S{_get_field(ep, 'seasonNumber', '')}E{_get_field(ep, 'episodeNumber', '')} - {_get_field(ep, 'title', '')}"
                )
                for ep in episodes
            )
            quality = _get_field(context.get("episodeFile"), "quality", "")
            cf_block = _render_custom_format_html(context.get("customFormatInfo"))
            upgrade_line = "✨ <b>画质洗版升级</b>" if context.get("isUpgrade") else ""
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"📥 <b>剧集下载完成</b>\n\n"
                    t"剧集: <b>{series_title}</b>\n"
                    t"{ep_lines:raw}\n"
                    t"质量: {quality}\n\n"
                    t"{cf_block:raw}\n\n"
                    t"{upgrade_line:raw}\n"
                    t"来源: <code>{server_name}</code>\n\n"
                    t"#Sonarr #下载完成"
                )
            )

        if event_type == NotificationEvent.SONARR_SERIES_ADD:
            series = context.get("series")
            title = _get_field(series, "title", "")
            year = _get_field(series, "year", "")
            genres = _get_field(series, "genres") or []
            genres_line = (
                TelegramMessage.render_html(
                    t"类型: {', '.join(str(g) for g in genres)}"
                )
                if genres
                else ""
            )
            series_path = _get_field(series, "path", "")
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"📺 <b>新增订阅剧集</b>\n\n"
                    t"标题: <b>{title}</b> ({year})\n"
                    t"{genres_line:raw}\n"
                    t"路径: <code>{series_path}</code>\n"
                    t"来源: <code>{server_name}</code>\n\n"
                    t"#Sonarr #新订阅"
                )
            )

        if event_type == NotificationEvent.RADARR_DOWNLOAD:
            movie = context.get("movie")
            title = _get_field(movie, "title", "")
            year = _get_field(movie, "year", "")
            quality = _get_field(context.get("movieFile"), "quality", "")
            cf_block = _render_custom_format_html(context.get("customFormatInfo"))
            upgrade_line = "✨ <b>画质洗版升级</b>" if context.get("isUpgrade") else ""
            overview_short = _truncate_text(_get_field(movie, "overview"), 80)
            overview_line = (
                TelegramMessage.render_html(t"简介: {overview_short}")
                if overview_short
                else ""
            )
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"📥 <b>电影下载完成</b>\n\n"
                    t"标题: <b>{title}</b> ({year})\n"
                    t"质量: {quality}\n\n"
                    t"{cf_block:raw}\n\n"
                    t"{upgrade_line:raw}\n"
                    t"{overview_line:raw}\n"
                    t"来源: <code>{server_name}</code>\n\n"
                    t"#Radarr #下载完成"
                )
            )

        if event_type == NotificationEvent.RADARR_MOVIE_ADD:
            movie = context.get("movie")
            title = _get_field(movie, "title", "")
            year = _get_field(movie, "year", "")
            genres = _get_field(movie, "genres") or []
            genres_line = (
                TelegramMessage.render_html(
                    t"类型: {', '.join(str(g) for g in genres)}"
                )
                if genres
                else ""
            )
            overview_short = _truncate_text(_get_field(movie, "overview"), 100)
            overview_line = (
                TelegramMessage.render_html(t"简介: {overview_short}")
                if overview_short
                else ""
            )
            tmdb_id = _get_field(movie, "tmdbId", "")
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"🎬 <b>新增订阅电影</b>\n\n"
                    t"标题: <b>{title}</b> ({year})\n"
                    t"{genres_line:raw}\n"
                    t"{overview_line:raw}\n"
                    t"TMDB ID: <code>{tmdb_id}</code>\n"
                    t"来源: <code>{server_name}</code>\n\n"
                    t"#Radarr #新增订阅"
                )
            )

        if event_type == NotificationEvent.EMBY_LIBRARY_NEW:
            item = context.get("item")
            return _render_library_new_html(
                server_name=server_name,
                series_name=_get_field(item, "series_name"),
                name=_get_field(item, "name", ""),
                year=_get_field(item, "production_year"),
                media_type=_get_field(item, "type", ""),
                overview=_get_field(item, "overview"),
            )

        if event_type == NotificationEvent.JELLYFIN_LIBRARY_NEW:
            item = context.get("item")
            fallback_name = item if isinstance(item, str) else ""
            return _render_library_new_html(
                server_name=server_name,
                series_name=_get_field(item, "series_name"),
                name=_get_field(item, "name", fallback_name),
                year=_get_field(item, "year"),
                media_type=_get_field(item, "item_type", ""),
                overview=_get_field(item, "overview"),
            )

        if event_type == NotificationEvent.SYSTEM_TEST:
            return TelegramMessage.render_html(
                t"🔔 <b>系统通知测试</b>\n\n来源: <code>{server_name}</code>"
            )

        return None

    async def send_to_topic(
        self,
        topic_id: int,
        event_type: NotificationEvent,
        image: Any = None,
        buttons: list | None = None,
        **kwargs: Any,
    ) -> Any:
        """兼容方法：直接发送通知到指定的 Topic 或 Channel。"""
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
            sent_msg = await client.send_message(
                chat_id=target_chat,
                message=message_text,
                file=image,
                buttons=buttons,
                reply_to=target_reply,
                parse_mode="html",
                link_preview=False,
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
        """兼容方法：发送模板通知给特定用户 (私聊)，返回是否发送成功。"""
        client = self.runtime.get_telethon_client()
        if client is None:
            return False

        message_text = await self._render(event_type, kwargs)
        if not message_text:
            logger.warning(
                "用户私聊通知模板渲染为空: user_id={}, event={}",
                user_id,
                event_type.value,
            )
            return False

        try:
            await client.send_message(
                chat_id=user_id, message=message_text, file=image, parse_mode="html"
            )
            logger.debug("发送的用户通知：{} -> 用户 {}", event_type.value, user_id)
            return True
        except (RPCError, ValueError, TypeError, OSError) as e:
            logger.error(
                "无法向 {} 发送用户通知 ({})：{}", user_id, event_type.value, e
            )
            return False

    async def send_text_to_user(
        self, user_id: int, message_text: str, image: Any = None
    ) -> bool:
        """兼容方法：发送自定义文本通知给特定用户 (私聊)，返回是否发送成功。"""
        client = self.runtime.get_telethon_client()
        if not message_text or client is None:
            return False

        try:
            await client.send_message(
                chat_id=user_id, message=message_text, file=image, parse_mode="html"
            )
            logger.debug("发送的用户文本通知 -> 用户 {}", user_id)
            return True
        except (RPCError, ValueError, TypeError, OSError) as e:
            logger.error("无法向 {} 发送用户文本通知：{}", user_id, e)
            return False


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
