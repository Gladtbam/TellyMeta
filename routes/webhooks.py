import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import errors

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
from models.emby_webhook import EmbyPayload, LibraryDeletedEvent, LibraryNewEvent
from models.events import NotificationEvent
from models.jellyfin_webhook import JellyfinPayload, NotificationType
from models.orm import ServerInstance
from models.radarr_webhook import (
    RadarrPayload,
    RadarrWebhookAddedPayload,
    RadarrWebhookDownloadPayload,
)
from models.sonarr_webhook import (
    SonarrPayload,
    SonarrWebhookDownloadPayload,
    SonarrWebhookSeriesAddPayload,
)
from repositories.config_repo import ConfigRepository
from repositories.server_repo import ServerRepository
from services.media_service import MediaService
from services.notification_service import NotificationService
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


async def _schedule_qb_share_limit(
    server_instance: ServerInstance,
    download_id: str | None,
    qb_clients: dict[int, QbittorrentClient],
    session: AsyncSession,
) -> None:
    """根据 Arr 绑定的 qBittorrent 服务器配置异步设置做种时长限制。"""
    if not download_id or not server_instance.qb_server_id:
        return
    qb_client = qb_clients.get(server_instance.qb_server_id)
    if not qb_client:
        return
    qb_server = await ServerRepository(session).get_by_id(server_instance.qb_server_id)
    seeding_limit = qb_server.qb_torrent_limit if qb_server else 43200
    create_background_task(
        qb_client.torrents_set_share_limits(
            torrent_hash=download_id,
            seeding_time_limit=seeding_limit,
        )
    )


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

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.SONARR_SERIES_ADD,
                server_name=client.server_name,
                series=payload.series,
            )
    elif isinstance(payload, SonarrWebhookDownloadPayload):
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
                await task_queue.put((mkv_local_path, client.notify_topic_id))
            elif mkv_diag:
                logger.warning(mkv_diag)
                if client.notify_topic_id:
                    try:
                        await notify_service.client.send_message(
                            chat_id=(
                                client.notify_topic_id
                                if client.notify_topic_id < 0
                                else settings.telegram_chat_id
                            ),
                            message=f"⚠️ **MKV 处理跳过**\n\n{mkv_diag}",
                            reply_to=(
                                client.notify_topic_id
                                if client.notify_topic_id > 0
                                else None
                            ),
                        )
                    except (errors.RPCError, ValueError) as e:
                        logger.warning("发送 MKV 路径映射告警失败: {}", e)

        await _schedule_qb_share_limit(
            server_instance, payload.downloadId, qb_clients, session
            )

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.SONARR_DOWNLOAD,
                server_name=client.server_name,
                series=payload.series,
                episodes=payload.episodes,
                episodeFile=payload.episodeFile,
                isUpgrade=payload.isUpgrade,
                release=payload.release,
                customFormatInfo=payload.customFormatInfo,
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
        if payload.movieFile and payload.movieFile.path:
            remote_path = payload.movieFile.path
            should_mkv, mkv_local_path, mkv_diag = _resolve_mkv_task_target(
                remote_path, client
            )
            if mapped_path := client.to_local_path(remote_path):
                payload.movieFile.path = mapped_path
            if task_queue and should_mkv and mkv_local_path:
                await task_queue.put((mkv_local_path, client.notify_topic_id))
            elif mkv_diag:
                logger.warning(mkv_diag)
                if client.notify_topic_id:
                    try:
                        await notify_service.client.send_message(
                            chat_id=(
                                client.notify_topic_id
                                if client.notify_topic_id < 0
                                else settings.telegram_chat_id
                            ),
                            message=f"⚠️ **MKV 处理跳过**\n\n{mkv_diag}",
                            reply_to=(
                                client.notify_topic_id
                                if client.notify_topic_id > 0
                                else None
                            ),
                        )
                    except (errors.RPCError, ValueError) as e:
                        logger.warning("发送 MKV 路径映射告警失败: {}", e)

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

        await _schedule_qb_share_limit(
            server_instance, payload.downloadId, qb_clients, session
            )

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.RADARR_DOWNLOAD,
                server_name=client.server_name,
                movie=payload.movie,
                movieFile=payload.movieFile,
                isUpgrade=payload.isUpgrade,
                release=payload.release,
                customFormatInfo=payload.customFormatInfo,
            )
    elif isinstance(payload, RadarrWebhookAddedPayload):
        if local_folder_path := client.to_local_path(payload.movie.folderPath):
            payload.movie.folderPath = local_folder_path
            if getattr(server_instance, "allow_nfo", True):
                create_background_task(handle_movie_add_metadata(payload, tmdb_client))
        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.RADARR_MOVIE_ADD,
                server_name=client.server_name,
                movie=payload.movie,
            )

    return Response(content="Webhook received", status_code=200)


@router.post("/webhook/emby", status_code=202)
async def emby_webhook(
    payload: EmbyPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
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

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.EMBY_LIBRARY_NEW,
                server_name=client.server_name,
                item=payload.item,
            )
    elif isinstance(payload, LibraryDeletedEvent) and ai_client:
            await cancel_translate_media_item(server_instance.id, payload.item.id)

    return Response(content="Webhook received", status_code=200)


@router.post("/webhook/jellyfin", status_code=202)
async def jellyfin(
    payload: JellyfinPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
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

    if payload.notification_type == NotificationType.ITEM_ADDED:
        if ai_client:
            await add_translate_media_item(server_instance.id, payload.item_id, days=1)

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.JELLYFIN_LIBRARY_NEW,
                server_name=client.server_name,
                item=payload.item_id,
            )
    elif payload.notification_type == NotificationType.ITEM_DELETED and ai_client:
            await cancel_translate_media_item(server_instance.id, payload.item_id)
    return Response(content="Webhook received", status_code=200)
