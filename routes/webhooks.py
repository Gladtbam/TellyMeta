import asyncio
import inspect
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from httpx2 import HTTPError
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from clients.ai_client import AIClientWarper
from clients.qb_client import QbittorrentClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.database import get_db
from core.dependencies import (
    get_ai_client,
    get_media_clients,
    get_notification_service,
    get_qb_clients,
    get_radarr_clients,
    get_server_by_token,
    get_sonarr_clients,
    get_task_queue,
    get_tmdb_client,
    get_tvdb_client,
)
from core.task_utils import create_background_task
from models.emby_webhook import (
    EmbyPayload,
    LibraryDeletedEvent,
    LibraryNewEvent,
    SystemNotificationTestEvent,
    SystemWebhookTestEvent,
    TaskFailedEvent,
    UserAuthenticatedEvent,
    UserAuthenticationFailedEvent,
    UserLockedOutEvent,
)
from models.jellyfin_webhook import (
    AuthenticationEvent,
    GenericEvent,
    ItemEvent,
    JellyfinPayload,
    NotificationType,
    TaskEvent,
    UserEvent,
)
from models.orm import MediaRequest, ServerInstance
from models.radarr_webhook import (
    RadarrPayload,
    RadarrWebhookAddedPayload,
    RadarrWebhookDownloadPayload,
    RadarrWebhookGrabPayload,
    RadarrWebhookHealthPayload,
    RadarrWebhookManualInteractionPayload,
    RadarrWebhookMovieDeletePayload,
    RadarrWebhookMovieFileDeletePayload,
    RadarrWebhookRenamePayload,
    RadarrWebhookTestPayload,
)
from models.sonarr_webhook import (
    SonarrPayload,
    SonarrWebhookDownloadPayload,
    SonarrWebhookEpisodeDeletePayload,
    SonarrWebhookGrabPayload,
    SonarrWebhookHealthPayload,
    SonarrWebhookManualInteractionPayload,
    SonarrWebhookRenamePayload,
    SonarrWebhookSeriesAddPayload,
    SonarrWebhookSeriesDeletePayload,
    SonarrWebhookTestPayload,
)
from repositories.config_repo import ConfigRepository
from repositories.request_repo import RequestRepository
from repositories.server_repo import ServerRepository
from services.media_service import MediaService
from services.notification_renderer import (
    ArrHealthIssuePayload,
    ArrHealthRestoredPayload,
    ArrManualInteractionPayload,
    ArrTestPayload,
    EffectiveEconomyContext,
    EmbyLibraryNewPayload,
    JellyfinLibraryNewPayload,
    MediaAuthRiskPayload,
    MediaLibraryDeletedPayload,
    MediaSubtitleFailedPayload,
    MediaSystemTestPayload,
    MediaTaskFailedPayload,
    MediaUserLockedOutPayload,
    MkvPathUnmappedPayload,
    NotificationPayload,
    QbShareLimitFailedPayload,
    RadarrDeletePayload,
    RadarrDownloadPayload,
    RadarrGrabPayload,
    RadarrMovieAddPayload,
    RadarrRenamePayload,
    RequestCompletedPayload,
    SonarrDeletePayload,
    SonarrDownloadPayload,
    SonarrGrabPayload,
    SonarrRenamePayload,
    SonarrSeriesAddPayload,
)
from services.notification_service import NotificationService
from services.request_service import sync_request_topic_card_status
from services.traffic_service import record_traffic_webhook_session
from services.watch_time_service import (
    NormalizedPlaybackEvent,
    WatchTimeService,
    normalize_emby_playback_event,
    normalize_jellyfin_playback_event,
)
from workers.nfo_worker import (
    create_episode_nfo,
    create_movie_nfo,
    handle_movie_add_metadata,
    handle_series_add_metadata,
)
from workers.translator_worker import (
    add_translate_media_item,
    cancel_translate_media_item,
)

router = APIRouter()
settings = get_settings()


async def _emit_notification(
    notify_service: NotificationService,
    payload: NotificationPayload,
    *,
    server_id: int | None = None,
    user_id: int | None = None,
    source_key: str | None = None,
    session: AsyncSession | None = None,
) -> bool:
    """安全调用统一通知发射入口，兼容直接传入未配置 AsyncMock 的测试替身。"""
    emit_fn = getattr(notify_service, "emit", None)
    if not callable(emit_fn):
        return False
    res = emit_fn(
        payload,
        user_id=user_id,
        server_id=server_id,
        source_key=source_key,
        session=session,
    )
    if inspect.isawaitable(res):
        return bool(await res)
    return bool(res)


def _build_radarr_download_payload(
    payload: RadarrWebhookDownloadPayload, *, server_name: str
) -> RadarrDownloadPayload:
    """构造 Radarr 下载通知载荷，兼容部分测试中使用 ``model_construct`` 传入不完整字段的场景。"""
    if (
        isinstance(payload.movie, BaseModel)
        and isinstance(payload.movieFile, BaseModel)
        and hasattr(payload, "customFormatInfo")
    ):
        return RadarrDownloadPayload.from_webhook(payload, server_name=server_name)
    return RadarrDownloadPayload(
        server_name=server_name or getattr(payload, "instanceName", None) or "Radarr",
        title=getattr(payload.movie, "title", ""),
        year=getattr(payload.movie, "year", None) or None,
        quality=getattr(payload.movieFile, "quality", "") or "",
        size_bytes=getattr(payload.movieFile, "size", None),
        is_upgrade=bool(getattr(payload, "isUpgrade", False)),
        overview=getattr(payload.movie, "overview", None),
        tmdb_id=getattr(payload.movie, "tmdbId", None) or None,
        images=getattr(payload.movie, "images", ()) or (),
    )


def _parse_positive_int(raw: Any) -> int | None:
    """将外部 Provider ID 安全解析为正整数，无效值返回 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = int(str(raw).strip())
        return val if val > 0 else None
    except ValueError, TypeError:
        return None


def _extract_provider_ids_from_mapping(
    mapping: Mapping[str, Any] | None, *, jellyfin_extra: bool = False
) -> tuple[int | None, int | None]:
    """大小写不敏感提取 TMDB 与 TVDB 正整数 ID。"""
    if not mapping:
        return None, None
    tmdb_keys = {"provider_tmdb"} if jellyfin_extra else {"tmdb"}
    tvdb_keys = {"provider_tvdb"} if jellyfin_extra else {"tvdb"}
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    for key, value in mapping.items():
        lower_key = str(key).strip().lower()
        if tmdb_id is None and lower_key in tmdb_keys:
            tmdb_id = _parse_positive_int(value)
        elif tvdb_id is None and lower_key in tvdb_keys:
            tvdb_id = _parse_positive_int(value)
    return tmdb_id, tvdb_id


async def _safe_get_item_info(client: Any, item_id: str) -> Any:
    """非致命回查媒体服务器媒体项详情，兼容未配置异步方法的测试替身与网络异常。"""
    getter = getattr(client, "get_item_info", None)
    if not callable(getter) or not item_id:
        return None
    try:
        info = getter(item_id)
        if inspect.isawaitable(info):
            info = await info
    except (HTTPError, ValueError, TypeError, AttributeError, OSError) as exc:
        logger.warning("回查媒体项详情失败 (item_id={}): {}", item_id, exc)
        return None
    if (
        getattr(info, "ProviderIds", None) is None
        and getattr(info, "provider_ids", None) is None
    ):
        return None
    return info


@dataclass(frozen=True, slots=True)
class _ArrivalMediaContext:
    """入库媒体标识解析上下文，收敛媒体项字段并消除跨函数散装参数。"""

    item_id: str
    item_type: str
    series_id: str | None
    payload_providers: Mapping[str, Any] | None
    jellyfin_extra: bool
    fallback_title: str
    fallback_year: int | None


async def _resolve_arrival_media_identifiers(
    client: Any,
    ctx: _ArrivalMediaContext,
) -> tuple[int | None, int | None, str, int | None]:
    """解析入库媒体的 TMDB/TVDB ID；剧集单集与季优先回查父级 Series 防止使用单集 ID。"""
    lower_type = ctx.item_type.strip().lower()
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    title = ctx.fallback_title
    year = ctx.fallback_year

    if lower_type in ("episode", "season") and ctx.series_id:
        series_info = await _safe_get_item_info(client, str(ctx.series_id))
        if series_info is not None:
            provider_map = getattr(series_info, "ProviderIds", None) or getattr(
                series_info, "provider_ids", None
            )
            tmdb_id, tvdb_id = _extract_provider_ids_from_mapping(provider_map)
            series_title = getattr(series_info, "Name", None) or getattr(
                series_info, "name", None
            )
            series_year = getattr(series_info, "ProductionYear", None) or getattr(
                series_info, "production_year", None
            )
            if isinstance(series_title, str) and series_title.strip():
                title = series_title.strip()
            if isinstance(series_year, int) and series_year > 0:
                year = series_year

    if tmdb_id is None and tvdb_id is None:
        tmdb_id, tvdb_id = _extract_provider_ids_from_mapping(
            ctx.payload_providers, jellyfin_extra=ctx.jellyfin_extra
        )

    if tmdb_id is None and tvdb_id is None and ctx.item_id:
        item_info = await _safe_get_item_info(client, str(ctx.item_id))
        if item_info is not None:
            provider_map = getattr(item_info, "ProviderIds", None) or getattr(
                item_info, "provider_ids", None
            )
            tmdb_id, tvdb_id = _extract_provider_ids_from_mapping(provider_map)

    return tmdb_id, tvdb_id, title, year


async def _complete_matching_requests_on_arrival(
    *,
    session: AsyncSession,
    notify_service: NotificationService,
    server_instance: ServerInstance,
    client: MediaService,
    media_ctx: _ArrivalMediaContext,
) -> None:
    """在 Emby/Jellyfin 真实入库时原子认领已批准求片，单次触发 REQUEST_COMPLETED 并同步审核卡片。"""
    if not isinstance(session, AsyncSession):
        return
    repo = RequestRepository(session)
    if not await repo.has_uncompleted_approved(server_instance.id):
        return

    tmdb_id, tvdb_id, title, year = await _resolve_arrival_media_identifiers(
        client, media_ctx
    )
    lower_type = media_ctx.item_type.strip().lower()
    exact_matches: list[tuple[str, int]] = []
    non_radarr_tmdb_id: int | None = None
    if lower_type == "movie":
        if tmdb_id is not None:
            exact_matches.append(("tmdb", tmdb_id))
    elif lower_type in ("series", "season", "episode"):
        if tvdb_id is not None:
            exact_matches.append(("tvdb", tvdb_id))
        non_radarr_tmdb_id = tmdb_id
    else:
        if tvdb_id is not None:
            exact_matches.append(("tvdb", tvdb_id))
        if tmdb_id is not None:
            exact_matches.append(("tmdb", tmdb_id))

    claimed_requests: list[MediaRequest] = await repo.claim_approved_for_completion(
        media_server_id=server_instance.id,
        exact_matches=exact_matches,
        non_radarr_tmdb_id=non_radarr_tmdb_id,
    )
    if not claimed_requests:
        return

    economy = EffectiveEconomyContext.from_cache(server_instance)
    for req in claimed_requests:
        media_title = req.title or title or str(req.media_id)
        await _emit_notification(
            notify_service,
            RequestCompletedPayload(
                server_name=client.server_name,
                media_title=media_title,
                media_year=year,
                item_type=media_ctx.item_type or None,
                reserved_cost=req.reserved_cost,
                request_id=req.id,
                economy=economy,
            ),
            user_id=req.user_id,
            server_id=req.media_server_id,
            session=session,
        )
        await sync_request_topic_card_status(
            notify_service,
            req,
            stage="completed",
            server_name=client.server_name,
            media_title=media_title,
            economy=economy,
            session=session,
            approver_name=req.approver_name,
        )


_AUTH_RISK_THRESHOLD = 3
_AUTH_RISK_WINDOW_MINUTES = 5


def _get_webhook_clock_now(notify_service: NotificationService) -> datetime:
    """读取通知运行时注入时钟（便于测试确定性推进窗口），未注入时回退系统时钟。"""
    runtime = getattr(notify_service, "runtime", None)
    clock = getattr(runtime, "clock", None)
    if callable(clock):
        val = clock()
        if isinstance(val, datetime):
            return val
    return datetime.now()


def _get_extra_ci(mapping: Mapping[str, Any] | None, keys: tuple[str, ...]) -> Any:
    """从 Pydantic extra 字段映射中大小写不敏感读取首个非空值。"""
    if not mapping:
        return None
    target_set = {k.lower() for k in keys}
    for k, v in mapping.items():
        if str(k).strip().lower() in target_set and v not in (None, ""):
            return v
    return None


async def _emit_arr_common_event(
    payload: Any,
    *,
    server_instance: ServerInstance,
    server_name: str,
    notify_service: NotificationService,
    session: AsyncSession,
) -> bool:
    """统一处理 Sonarr / Radarr 共用的运维告警与测试事件（人工干预/健康检查/测试）。"""
    if isinstance(
        payload,
        (SonarrWebhookManualInteractionPayload, RadarrWebhookManualInteractionPayload),
    ):
        dl_title = payload.downloadInfo.title if payload.downloadInfo else ""
        dedupe_part = payload.downloadId or dl_title or "manual"
        await _emit_notification(
            notify_service,
            ArrManualInteractionPayload.from_webhook(payload, server_name=server_name),
            server_id=server_instance.id,
            source_key=f"arr_manual:{server_instance.id}:{dedupe_part}",
            session=session,
        )
        return True

    if isinstance(payload, (SonarrWebhookHealthPayload, RadarrWebhookHealthPayload)):
        health_source = f"arr_health:{server_instance.id}:{payload.type}"
        if payload.eventType == "Health":
            if (payload.level or "").strip().lower() != "ok":
                await _emit_notification(
                    notify_service,
                    ArrHealthIssuePayload.from_webhook(
                        payload, server_name=server_name
                    ),
                    server_id=server_instance.id,
                    source_key=health_source,
                    session=session,
                )
        elif payload.eventType == "HealthRestored":
            await _emit_notification(
                notify_service,
                ArrHealthRestoredPayload.from_webhook(payload, server_name=server_name),
                server_id=server_instance.id,
                source_key=health_source,
                session=session,
            )
        return True

    if isinstance(payload, (SonarrWebhookTestPayload, RadarrWebhookTestPayload)):
        await _emit_notification(
            notify_service,
            ArrTestPayload.from_webhook(payload, server_name=server_name),
            server_id=server_instance.id,
            session=session,
        )
        return True

    return False


async def _record_media_auth_failure_and_emit_risk(
    request: Request,
    *,
    server_instance: ServerInstance,
    server_name: str,
    username: str,
    remote_ip: str,
    client_name: str,
    device_name: str,
    notify_service: NotificationService,
    session: AsyncSession,
) -> None:
    """按 5 分钟滑动窗口聚合媒体服登录失败次数，达到 >=3 次阈值后触发风控告警。"""
    state = getattr(request.app, "state", None)
    if state is None:
        return
    cache: dict[tuple[int, str], deque[datetime]] | None = getattr(
        state, "media_auth_failures", None
    )
    if cache is None:
        cache = {}
        state.media_auth_failures = cache

    now = _get_webhook_clock_now(notify_service)
    cutoff = now - timedelta(minutes=_AUTH_RISK_WINDOW_MINUTES)

    buckets: list[tuple[int, str]] = []
    clean_user = username.strip().lower()
    clean_ip = remote_ip.strip()
    if clean_user:
        buckets.append((server_instance.id, f"user:{clean_user}"))
    if clean_ip:
        buckets.append((server_instance.id, f"ip:{clean_ip}"))
    if not buckets:
        buckets.append((server_instance.id, "unknown"))

    max_count = 0
    for key in buckets:
        window_q = cache.setdefault(key, deque())
        while window_q and window_q[0] < cutoff:
            window_q.popleft()
        window_q.append(now)
        if len(window_q) > max_count:
            max_count = len(window_q)

    # 控制内存上限：顺手清理过期空桶
    if len(cache) > 512:
        stale_keys = [k for k, dq in cache.items() if not dq or dq[-1] < cutoff]
        for k in stale_keys:
            cache.pop(k, None)

    if max_count >= _AUTH_RISK_THRESHOLD:
        identity = clean_ip or clean_user or "unknown"
        await _emit_notification(
            notify_service,
            MediaAuthRiskPayload(
                server_name=server_name,
                username=username,
                client_name=client_name,
                device_name=device_name,
                remote_ip=remote_ip,
                failure_count=max_count,
                window_minutes=_AUTH_RISK_WINDOW_MINUTES,
            ),
            server_id=server_instance.id,
            source_key=f"media_auth_risk:{server_instance.id}:{identity}",
            session=session,
        )


def _resolve_mkv_task_target(
    remote_path: str, client: SonarrClient | RadarrClient
) -> tuple[bool, Path | None, str | None]:
    """根据管理员配置与路径映射判断是否应入队 MKV 清理，返回 (是否入队, 本地路径, 诊断信息)"""
    if (
        ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_MKV_PROCESS, "true")
        != "true"
    ):
        return False, None, None

    exclude_keywords = ConfigRepository.parse_exclude_keywords(
        ConfigRepository.cache.get(ConfigRepository.KEY_MKV_EXCLUDE_KEYWORDS, "")
    )
    if any(kw in remote_path for kw in exclude_keywords):
        return False, None, None

    mapped_path = client.to_local_path(remote_path)
    if getattr(client, "path_mappings", None) and not mapped_path:
        return (
            False,
            None,
            f"MKV 路径映射失败（未匹配任何本地映射规则，已跳过破坏性处理）: {remote_path}",
        )

    local_path = mapped_path or remote_path
    if any(kw in local_path for kw in exclude_keywords):
        return False, None, None

    return True, Path(local_path), None


async def _emit_mkv_unmapped_alert(
    *,
    notify_service: NotificationService,
    server_id: int | None,
    server_name: str,
    remote_path: str,
    diagnostic: str,
    session: AsyncSession | None = None,
) -> None:
    """发射 MKV 路径未映射跳过处理运维告警（兼容测试替身）。"""
    try:
        await _emit_notification(
            notify_service,
            MkvPathUnmappedPayload(
                server_name=server_name,
                remote_path=remote_path,
                diagnostic=diagnostic,
            ),
            server_id=server_id,
            source_key=f"mkv_unmapped:{server_id}:{remote_path}",
            session=session,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("发射 MKV 路径映射告警失败: {}", exc)


async def _run_qb_share_limit(
    *,
    qb_client: QbittorrentClient,
    torrent_hash: str,
    seeding_time_limit: int,
    arr_server_id: int,
    arr_server_name: str,
    qb_server_id: int,
    qb_server_name: str,
    notify_service: NotificationService | None,
) -> None:
    """后台设置 qBittorrent 做种分享限制，失败时记录日志并向运维告警通道发射异常事件。"""
    try:
        await qb_client.torrents_set_share_limits(
            torrent_hash=torrent_hash,
            seeding_time_limit=seeding_time_limit,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "设置 qBittorrent 做种限制失败: arr={}, qb={}, hash={}, err={}",
            arr_server_name,
            qb_server_name,
            torrent_hash,
            exc,
        )
        if notify_service is None:
            return
        try:
            # 后台任务独立运行于请求会话之外，不传入已结束的 request session
            await _emit_notification(
                notify_service,
                QbShareLimitFailedPayload(
                    server_name=arr_server_name,
                    qb_server_name=qb_server_name,
                    download_id=torrent_hash,
                    error_message=str(exc) or type(exc).__name__,
                ),
                server_id=arr_server_id,
                source_key=f"qb_share_limit:{qb_server_id}",
            )
        except Exception as notify_exc:  # pylint: disable=broad-exception-caught
            logger.warning("发射 qB 做种限制失败告警异常: {}", notify_exc)


async def _finalize_arr_download_ops(
    *,
    server_instance: ServerInstance,
    server_name: str,
    download_id: str | None,
    qb_clients: dict[int, QbittorrentClient],
    notify_service: NotificationService,
    session: AsyncSession,
    unmapped_remote_path: str | None,
    unmapped_diag: str | None,
) -> None:
    """完成 Arr 下载事件的 qB 参数查询、MKV 未映射告警与只读事务收口后再拉起后台 qB 任务。"""
    qb_task_coro = None
    if download_id and server_instance.qb_server_id:
        qb_client = qb_clients.get(server_instance.qb_server_id)
        if qb_client:
            qb_server = await ServerRepository(session).get_by_id(
                server_instance.qb_server_id
            )
            seeding_limit = qb_server.qb_torrent_limit if qb_server else 43200
            qb_server_name = (
                qb_server.name if qb_server else f"qB-{server_instance.qb_server_id}"
            )
            arr_server_name = (
                getattr(server_instance, "name", "") or f"Arr-{server_instance.id}"
            )
            qb_task_coro = _run_qb_share_limit(
                qb_client=qb_client,
                torrent_hash=download_id,
                seeding_time_limit=seeding_limit,
                arr_server_id=int(server_instance.id),
                arr_server_name=arr_server_name,
                qb_server_id=int(server_instance.qb_server_id),
                qb_server_name=qb_server_name,
                notify_service=notify_service,
            )

    if unmapped_diag and unmapped_remote_path is not None:
        await _emit_mkv_unmapped_alert(
            notify_service=notify_service,
            server_id=getattr(server_instance, "id", None),
            server_name=server_name,
            remote_path=unmapped_remote_path,
            diagnostic=unmapped_diag,
            session=session,
        )

    # 在拉起后台协程与请求收尾前关闭只读事务快照，避免 session.close() 回滚与后台日志写入竞争内存连接池
    if (
        isinstance(session, AsyncSession)
        and session.in_transaction()
        and not (session.new or session.dirty or session.deleted)
    ):
        await session.rollback()

    if qb_task_coro is not None:
        create_background_task(qb_task_coro)


@router.post("/webhook/sonarr", status_code=202)
async def sonarr_webhook(
    payload: SonarrPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    sonarr_clients: dict[int, SonarrClient] = Depends(get_sonarr_clients),
    tmdb_client: TmdbClient | None = Depends(get_tmdb_client),
    tvdb_client: TvdbClient | None = Depends(get_tvdb_client),
    task_queue: asyncio.Queue | None = Depends(get_task_queue),
    qb_clients: dict[int, QbittorrentClient] = Depends(get_qb_clients),
    notify_service: NotificationService = Depends(get_notification_service),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """处理来自 Sonarr 的 Webhook"""
    client = sonarr_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    logger.info(
        "收到来自 {} 的 Webhook, 事件类型: {}", client.server_name, payload.eventType
    )

    if isinstance(payload, SonarrWebhookSeriesAddPayload):
        if mapped_path := client.to_local_path(payload.series.path):
            payload.series.path = mapped_path
        if tmdb_client and getattr(server_instance, "allow_nfo", True):
            create_background_task(
                handle_series_add_metadata(client, payload, tmdb_client, tvdb_client)
            )

        await _emit_notification(
            notify_service,
            SonarrSeriesAddPayload.from_webhook(
                payload, server_name=client.server_name
            ),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(payload, SonarrWebhookDownloadPayload):
        unmapped_remote_path: str | None = None
        unmapped_diag: str | None = None
        if payload.episodeFile and payload.episodeFile.path:
            remote_path = payload.episodeFile.path
            should_mkv, mkv_local_path, mkv_diag = _resolve_mkv_task_target(
                remote_path, client
            )
            if mapped_path := client.to_local_path(remote_path):
                payload.episodeFile.path = mapped_path
            if tmdb_client and getattr(server_instance, "allow_nfo", True):
                create_background_task(
                    create_episode_nfo(payload, tmdb_client, tvdb_client)
                )
            if task_queue and should_mkv and mkv_local_path:
                await task_queue.put(
                    (
                        mkv_local_path,
                        client.notify_topic_id,
                        server_instance.id,
                        client.server_name,
                    )
                )
            elif mkv_diag:
                logger.warning(mkv_diag)
                unmapped_remote_path = remote_path
                unmapped_diag = mkv_diag

        await _emit_notification(
            notify_service,
            SonarrDownloadPayload.from_webhook(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )

        await _finalize_arr_download_ops(
            server_instance=server_instance,
            server_name=client.server_name,
            download_id=payload.downloadId,
            qb_clients=qb_clients,
            notify_service=notify_service,
            session=session,
            unmapped_remote_path=unmapped_remote_path,
            unmapped_diag=unmapped_diag,
        )
    elif isinstance(payload, SonarrWebhookGrabPayload):
        await _emit_notification(
            notify_service,
            SonarrGrabPayload.from_webhook(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(payload, SonarrWebhookRenamePayload):
        await _emit_notification(
            notify_service,
            SonarrRenamePayload.from_webhook(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(
        payload, (SonarrWebhookEpisodeDeletePayload, SonarrWebhookSeriesDeletePayload)
    ):
        await _emit_notification(
            notify_service,
            SonarrDeletePayload.from_webhook(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )
    else:
        await _emit_arr_common_event(
            payload,
            server_instance=server_instance,
            server_name=client.server_name,
            notify_service=notify_service,
            session=session,
        )

    return Response(content="Webhook received", status_code=200)


@router.post("/webhook/radarr", status_code=202)
async def radarrarr_webhook(
    payload: RadarrPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    radarr_clients: dict[int, RadarrClient] = Depends(get_radarr_clients),
    tmdb_client: TmdbClient | None = Depends(get_tmdb_client),
    task_queue: asyncio.Queue | None = Depends(get_task_queue),
    qb_clients: dict[int, QbittorrentClient] = Depends(get_qb_clients),
    notify_service: NotificationService = Depends(get_notification_service),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """处理来自 Radarr 的 Webhook"""
    client = radarr_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    logger.info(
        "收到来自 {} 的 Webhook, 事件类型: {}", client.server_name, payload.eventType
    )

    if isinstance(payload, RadarrWebhookDownloadPayload):
        unmapped_remote_path: str | None = None
        unmapped_diag: str | None = None
        if payload.movieFile and payload.movieFile.path:
            remote_path = payload.movieFile.path
            should_mkv, mkv_local_path, mkv_diag = _resolve_mkv_task_target(
                remote_path, client
            )
            if mapped_path := client.to_local_path(remote_path):
                payload.movieFile.path = mapped_path
            if task_queue and should_mkv and mkv_local_path:
                await task_queue.put(
                    (
                        mkv_local_path,
                        client.notify_topic_id,
                        server_instance.id,
                        client.server_name,
                    )
                )
            elif mkv_diag:
                logger.warning(mkv_diag)
                unmapped_remote_path = remote_path
                unmapped_diag = mkv_diag

        if getattr(server_instance, "allow_nfo", True) and (
            local_movie_file := client.to_local_path(payload.movieFile.path)
        ):
            create_background_task(
                create_movie_nfo(
                    Path(local_movie_file).with_suffix(".nfo"),
                    payload.movie.tmdbId,
                    tmdb_client,
                )
            )

        await _emit_notification(
            notify_service,
            _build_radarr_download_payload(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )

        await _finalize_arr_download_ops(
            server_instance=server_instance,
            server_name=client.server_name,
            download_id=payload.downloadId,
            qb_clients=qb_clients,
            notify_service=notify_service,
            session=session,
            unmapped_remote_path=unmapped_remote_path,
            unmapped_diag=unmapped_diag,
        )
    elif isinstance(payload, RadarrWebhookAddedPayload):
        if local_folder_path := client.to_local_path(payload.movie.folderPath):
            payload.movie.folderPath = local_folder_path
            if getattr(server_instance, "allow_nfo", True):
                create_background_task(handle_movie_add_metadata(payload, tmdb_client))
        await _emit_notification(
            notify_service,
            RadarrMovieAddPayload.from_webhook(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(payload, RadarrWebhookGrabPayload):
        await _emit_notification(
            notify_service,
            RadarrGrabPayload.from_webhook(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(payload, RadarrWebhookRenamePayload):
        await _emit_notification(
            notify_service,
            RadarrRenamePayload.from_webhook(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(
        payload, (RadarrWebhookMovieFileDeletePayload, RadarrWebhookMovieDeletePayload)
    ):
        await _emit_notification(
            notify_service,
            RadarrDeletePayload.from_webhook(payload, server_name=client.server_name),
            server_id=server_instance.id,
            session=session,
        )
    else:
        await _emit_arr_common_event(
            payload,
            server_instance=server_instance,
            server_name=client.server_name,
            notify_service=notify_service,
            session=session,
        )

    return Response(content="Webhook received", status_code=200)


async def _process_playback_webhook(
    request: Request,
    session: AsyncSession,
    server_instance: ServerInstance,
    playback_event: NormalizedPlaybackEvent | None,
) -> None:
    """统一处理来自 Emby / Jellyfin Webhook 的标准化播放状态事件。

    Args:
        request: FastAPI 请求对象，用于向 ``WatchTimeService`` 传递 ``app`` 以按需挂载 Emby 心跳。
        session: 异步数据库会话。
        server_instance: 当前触发 Webhook 的服务器实例。
        playback_event: 标准化播放事件；非播放类 Webhook 或缺少必需标识时为 ``None``。
    """
    if playback_event is None:
        return
    # 先结束鉴权依赖 get_server_by_token 打开的只读事务，确保加锁后读取最新数据库快照
    if session.in_transaction():
        await session.commit()
    watch_time_service = WatchTimeService(session)
    await watch_time_service.process_playback_event(
        server_instance,
        playback_event,
        app=request.app,
        commit=True,
    )


def _warm_traffic_attribution_cache(
    request: Request,
    server_instance: ServerInstance,
    *,
    media_user_id: str | None,
    device_id: str | None,
    play_session_id: str | None = None,
) -> None:
    """将 Emby/Jellyfin 认证与播放 Webhook 携带的设备/会话映射预热到流量归因缓存

    仅当全局与目标服务器流量采集均启用时才预热归因映射，关闭流量体系时跳过无谓写入。

    Args:
        request: FastAPI 请求对象，用于定位 ``app.state.traffic_service``。
        server_instance: 当前触发 Webhook 的服务器实例。
        media_user_id: 媒体平台侧用户 ID。
        device_id: 客户端设备标识（可为空）。
        play_session_id: 播放会话标识（可为空）。
    """
    if not (
        ConfigRepository.is_traffic_enabled_from_cache()
        and bool(server_instance.enable_traffic)
    ):
        return
    if not media_user_id or (not device_id and not play_session_id):
        return
    record_traffic_webhook_session(
        request.app,
        server_instance.id,
        media_user_id=media_user_id,
        device_id=device_id,
        play_session_id=play_session_id,
    )


def _extract_emby_device_id(payload: EmbyPayload) -> str | None:
    """从 Emby Webhook 载荷的 Session 或 DeviceInfo 中提取设备 ID"""
    session_obj = getattr(payload, "session", None)
    device_obj = getattr(payload, "device_info", None)
    return getattr(session_obj, "device_id", None) or getattr(device_obj, "id", None)


@router.post("/webhook/emby", status_code=202)
async def emby_webhook(
    request: Request,
    payload: EmbyPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    session: AsyncSession = Depends(get_db),
    ai_client: AIClientWarper | None = Depends(get_ai_client),
    media_clients: dict[int, MediaService] = Depends(get_media_clients),
    notify_service: NotificationService = Depends(get_notification_service),
) -> Response:
    """处理来自 Emby 的 Webhook"""
    client = media_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    logger.info(
        "收到来自 {} 的 Webhook, 事件类型: {}", client.server_name, payload.event
    )

    if isinstance(payload, LibraryNewEvent):
        if ai_client:
            await add_translate_media_item(server_instance.id, payload.item.id, days=1)

        await _emit_notification(
            notify_service,
            EmbyLibraryNewPayload(
                server_name=client.server_name,
                item=payload.item,
                tmdb_id=payload.item.tmdb_id,
            ),
            server_id=server_instance.id,
            session=session,
        )
        await _complete_matching_requests_on_arrival(
            session=session,
            notify_service=notify_service,
            server_instance=server_instance,
            client=client,
            media_ctx=_ArrivalMediaContext(
                item_id=payload.item.id,
                item_type=payload.item.type,
                series_id=payload.item.series_id,
                payload_providers=payload.item.provider_ids,
                jellyfin_extra=False,
                fallback_title=payload.item.series_name or payload.item.name,
                fallback_year=payload.item.production_year,
            ),
        )
    elif isinstance(payload, LibraryDeletedEvent):
        if ai_client:
            await cancel_translate_media_item(server_instance.id, payload.item.id)
        await _emit_notification(
            notify_service,
            MediaLibraryDeletedPayload(
                server_name=client.server_name,
                name=payload.item.name,
                series_name=payload.item.series_name,
                year=payload.item.production_year,
                item_type=payload.item.type,
                item_id=payload.item.id,
            ),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(payload, UserLockedOutEvent):
        await _emit_notification(
            notify_service,
            MediaUserLockedOutPayload(
                server_name=client.server_name,
                media_user_name=payload.user.name,
                media_user_id=payload.user.id,
            ),
            server_id=server_instance.id,
            source_key=f"media_lockout:{server_instance.id}:{payload.user.id or payload.user.name}",
            session=session,
        )
    elif isinstance(payload, UserAuthenticationFailedEvent):
        dev = payload.device_info
        await _record_media_auth_failure_and_emit_risk(
            request,
            server_instance=server_instance,
            server_name=client.server_name,
            username=(dev.last_user_name if dev and dev.last_user_name else ""),
            remote_ip=(dev.ip_address if dev and dev.ip_address else ""),
            client_name=(dev.app_name if dev and dev.app_name else ""),
            device_name=(dev.name if dev and dev.name else ""),
            notify_service=notify_service,
            session=session,
        )
    elif isinstance(payload, TaskFailedEvent):
        await _emit_notification(
            notify_service,
            MediaTaskFailedPayload(
                server_name=client.server_name,
                task_name=payload.title or "计划任务",
                task_state="Failed",
                error_message=payload.description or "",
            ),
            server_id=server_instance.id,
            source_key=f"media_task_failed:{server_instance.id}:{payload.title or 'task'}",
            session=session,
        )
    elif isinstance(payload, (SystemNotificationTestEvent, SystemWebhookTestEvent)):
        await _emit_notification(
            notify_service,
            MediaSystemTestPayload(
                server_name=client.server_name,
                server_type=server_instance.server_type,
                title=payload.title or "Emby Webhook 测试通知",
                description=payload.description or "",
            ),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(payload, UserAuthenticatedEvent):
        # 登录事件提前预热设备映射，减少后续首片流请求对 /Sessions 的回查
        _warm_traffic_attribution_cache(
            request,
            server_instance,
            media_user_id=payload.user.id,
            device_id=_extract_emby_device_id(payload),
        )
    else:
        playback_event = normalize_emby_playback_event(payload)
        if playback_event is not None:
            # 播放事件同步刷新设备与 PlaySessionId 归因缓存，确保同设备换号及纯 PlaySessionId 分片准确命中
            playback_info = getattr(payload, "playback_info", None)
            _warm_traffic_attribution_cache(
                request,
                server_instance,
                media_user_id=playback_event.media_user_id,
                device_id=_extract_emby_device_id(payload),
                play_session_id=getattr(playback_info, "play_session_id", None),
            )
        await _process_playback_webhook(
            request, session, server_instance, playback_event
        )

    return Response(content="Webhook received", status_code=200)


@router.post("/webhook/jellyfin", status_code=202)
async def jellyfin(
    request: Request,
    payload: JellyfinPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    session: AsyncSession = Depends(get_db),
    ai_client: AIClientWarper | None = Depends(get_ai_client),
    media_clients: dict[int, MediaService] = Depends(get_media_clients),
    notify_service: NotificationService = Depends(get_notification_service),
) -> Response:
    """处理来自 Jellyfin 的 Webhook"""
    client = media_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    logger.info(
        "收到来自 {} 的 Webhook, 事件类型: {}",
        client.server_name,
        payload.notification_type,
    )

    if payload.notification_type == NotificationType.ITEM_ADDED and isinstance(
        payload, ItemEvent
    ):
        if ai_client:
            await add_translate_media_item(server_instance.id, payload.item_id, days=1)

        await _emit_notification(
            notify_service,
            JellyfinLibraryNewPayload(
                server_name=client.server_name,
                item=payload,
            ),
            server_id=server_instance.id,
            session=session,
        )
        await _complete_matching_requests_on_arrival(
            session=session,
            notify_service=notify_service,
            server_instance=server_instance,
            client=client,
            media_ctx=_ArrivalMediaContext(
                item_id=payload.item_id,
                item_type=payload.item_type,
                series_id=payload.series_id,
                payload_providers=payload.model_extra,
                jellyfin_extra=True,
                fallback_title=payload.series_name or payload.name,
                fallback_year=payload.year,
            ),
        )
    elif payload.notification_type == NotificationType.ITEM_DELETED and isinstance(
        payload, ItemEvent
    ):
        if ai_client:
            await cancel_translate_media_item(server_instance.id, payload.item_id)
        await _emit_notification(
            notify_service,
            MediaLibraryDeletedPayload(
                server_name=client.server_name,
                name=payload.name,
                series_name=payload.series_name,
                year=payload.year,
                item_type=payload.item_type,
                item_id=payload.item_id,
            ),
            server_id=server_instance.id,
            session=session,
        )
    elif (
        payload.notification_type == NotificationType.SUBTITLE_DOWNLOAD_FAILURE
        and isinstance(payload, ItemEvent)
    ):
        extra = payload.model_extra or {}
        language = str(
            _get_extra_ci(extra, ("language", "subtitlelanguage", "subtitle_language"))
            or ""
        )
        err_msg = str(
            _get_extra_ci(extra, ("errormessage", "error_message", "message", "error"))
            or ""
        )
        await _emit_notification(
            notify_service,
            MediaSubtitleFailedPayload(
                server_name=client.server_name,
                item_name=payload.name,
                series_name=payload.series_name,
                item_type=payload.item_type,
                language=language,
                error_message=err_msg,
            ),
            server_id=server_instance.id,
            source_key=f"media_sub_failed:{server_instance.id}:{payload.item_id}",
            session=session,
        )
    elif payload.notification_type == NotificationType.USER_LOCKED_OUT and isinstance(
        payload, UserEvent
    ):
        await _emit_notification(
            notify_service,
            MediaUserLockedOutPayload(
                server_name=client.server_name,
                media_user_name=payload.notification_username or payload.user_id,
                media_user_id=payload.user_id,
            ),
            server_id=server_instance.id,
            source_key=f"media_lockout:{server_instance.id}:{payload.user_id or payload.notification_username}",
            session=session,
        )
    elif (
        isinstance(payload, AuthenticationEvent)
        and payload.notification_type == NotificationType.AUTHENTICATION_FAILURE
    ):
        await _record_media_auth_failure_and_emit_risk(
            request,
            server_instance=server_instance,
            server_name=client.server_name,
            username=payload.username or payload.notification_username or "",
            remote_ip=payload.remote_end_point or "",
            client_name=payload.app
            or payload.client_name
            or payload.client_name_pb
            or "",
            device_name=payload.device_name or "",
            notify_service=notify_service,
            session=session,
        )
    elif (
        isinstance(payload, TaskEvent)
        and payload.notification_type == NotificationType.TASK_COMPLETED
    ):
        task_state_l = (payload.task_state or "").strip().lower()
        result_status_l = (payload.result_status or "").strip().lower()
        if "fail" in task_state_l or "fail" in result_status_l:
            await _emit_notification(
                notify_service,
                MediaTaskFailedPayload(
                    server_name=client.server_name,
                    task_name=payload.task_name,
                    task_state=payload.task_state,
                    error_message=(
                        payload.result_error_message
                        or payload.result_long_error_message
                        or ""
                    ),
                ),
                server_id=server_instance.id,
                source_key=f"media_task_failed:{server_instance.id}:{payload.task_id or payload.task_name}",
                session=session,
            )
    elif (
        isinstance(payload, GenericEvent)
        and payload.notification_type == NotificationType.GENERIC
    ):
        await _emit_notification(
            notify_service,
            MediaSystemTestPayload(
                server_name=client.server_name,
                server_type=server_instance.server_type,
                title=payload.name or "Jellyfin Webhook 测试通知",
                description=payload.description or "",
            ),
            server_id=server_instance.id,
            session=session,
        )
    elif isinstance(payload, AuthenticationEvent) and payload.notification_type in (
        NotificationType.AUTHENTICATION_SUCCESS,
        NotificationType.SESSION_START,
    ):
        # 认证或会话启动事件提前预热设备映射，减少后续首片流请求对 /Sessions 的回查
        _warm_traffic_attribution_cache(
            request,
            server_instance,
            media_user_id=payload.user_id,
            device_id=payload.device_id,
        )
    else:
        playback_event = normalize_jellyfin_playback_event(payload)
        if playback_event is not None:
            # 播放事件同步刷新设备与 PlaySessionId 归因缓存，确保同设备换号及纯 PlaySessionId 分片准确命中
            _warm_traffic_attribution_cache(
                request,
                server_instance,
                media_user_id=playback_event.media_user_id,
                device_id=getattr(payload, "device_id", None),
                play_session_id=(
                    getattr(payload, "play_session_id", None)
                    or getattr(payload, "PlaySessionId", None)
                ),
            )
        await _process_playback_webhook(
            request, session, server_instance, playback_event
        )
    return Response(content="Webhook received", status_code=200)
