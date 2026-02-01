import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Response
from loguru import logger

from clients.ai_client import AIClientWarper
from clients.qb_client import QbittorrentClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.dependencies import (get_ai_client, get_media_clients,
                               get_notification_service, get_qb_client,
                               get_radarr_clients, get_server_by_token,
                               get_sonarr_clients, get_task_queue,
                               get_tmdb_client, get_tvdb_client)
from models.emby_webhook import (EmbyPayload, LibraryDeletedEvent,
                                 LibraryNewEvent)
from models.events import NotificationEvent
from models.jellyfin_webhook import JellyfinPayload, NotificationType
from models.orm import ServerInstance
from models.radarr import (RadarrPayload, RadarrWebhookAddedPayload,
                           RadarrWebhookDownloadPayload)
from models.sonarr import (SonarrPayload, SonarrWebhookDownloadPayload,
                           SonarrWebhookSeriesAddPayload)
from services.media_service import MediaService
from services.notification_service import NotificationService
from workers.nfo_worker import (clean_radarr_nfo, create_episode_nfo,
                                handle_series_add_metadata)
from workers.translator_worker import (add_translate_media_item,
                                       cancel_translate_media_item)

router = APIRouter()
settings = get_settings()

@router.post("/webhook/sonarr", status_code=202)
async def sonarr_webhook(
    payload: SonarrPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    sonarr_clients: dict[int, SonarrClient] = Depends(get_sonarr_clients),
    tmdb_client: TmdbClient | None = Depends(get_tmdb_client),
    tvdb_client: TvdbClient | None = Depends(get_tvdb_client),
    task_queue: asyncio.Queue = Depends(get_task_queue),
    qb_client: QbittorrentClient | None = Depends(get_qb_client),
    notify_service: NotificationService = Depends(get_notification_service)
) -> Response:
    """处理来自 Sonarr 的 Webhook"""
    client = sonarr_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    logger.info("收到来自 {} 的 Webhook, 事件类型: {}", client.server_name, payload.eventType)

    if isinstance(payload, SonarrWebhookSeriesAddPayload):
        if mapped_path := client.to_local_path(payload.series.path):
            payload.series.path = mapped_path
        if tmdb_client:
            asyncio.create_task(handle_series_add_metadata(client, payload, tmdb_client, tvdb_client))

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.SONARR_SERIES_ADD,
                server_name=client.server_name,
                series=payload.series
            )
    elif isinstance(payload, SonarrWebhookDownloadPayload):
        if payload.episodeFile and payload.episodeFile.path and 'VCB-Studio' not in payload.episodeFile.path:
            if mapped_path := client.to_local_path(payload.episodeFile.path):
                payload.episodeFile.path = mapped_path
            if tmdb_client:
                asyncio.create_task(create_episode_nfo(payload, tmdb_client, tvdb_client))
            await task_queue.put(Path(payload.episodeFile.path))

        if payload.downloadId and qb_client:
            asyncio.create_task(qb_client.torrents_set_share_limits(
                torrent_hash=payload.downloadId,
                seeding_time_limit=settings.qbittorrent_torrent_limit
            ))

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
                customFormatInfo=payload.customFormatInfo
            )

    return Response(content="Webhook received", status_code=200)

@router.post("/webhook/radarr", status_code=202)
async def radarrarr_webhook(
    payload: RadarrPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    radarr_clients: dict[int, RadarrClient] = Depends(get_radarr_clients),
    task_queue: asyncio.Queue = Depends(get_task_queue),
    qb_client: QbittorrentClient | None = Depends(get_qb_client),
    notify_service: NotificationService = Depends(get_notification_service)
) -> Response:
    """处理来自 Radarr 的 Webhook"""
    client = radarr_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    logger.info("收到来自 {} 的 Webhook, 事件类型: {}", client.server_name, payload.eventType)

    if isinstance(payload, RadarrWebhookDownloadPayload):
        if payload.movieFile and payload.movieFile.path and 'VCB-Studio' not in payload.movieFile.path:
            if mapped_path := client.to_local_path(payload.movieFile.path):
                payload.movieFile.path = mapped_path
            await task_queue.put(Path(payload.movieFile.path))

        if local_folder_path := client.to_local_path(payload.movie.folderPath):
            asyncio.create_task(clean_radarr_nfo(local_folder_path))

        if payload.downloadId and qb_client:
            asyncio.create_task(qb_client.torrents_set_share_limits(
                torrent_hash=payload.downloadId,
                seeding_time_limit=settings.qbittorrent_torrent_limit
            ))

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.RADARR_DOWNLOAD,
                server_name=client.server_name,
                movie=payload.movie,
                movieFile=payload.movieFile,
                isUpgrade=payload.isUpgrade,
                release=payload.release,
                customFormatInfo=payload.customFormatInfo
            )
    elif isinstance(payload, RadarrWebhookAddedPayload):
        if local_folder_path := client.to_local_path(payload.movie.folderPath):
            asyncio.create_task(clean_radarr_nfo(local_folder_path))
        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.RADARR_MOVIE_ADD,
                server_name=client.server_name,
                movie=payload.movie
            )

    return Response(content="Webhook received", status_code=200)

@router.post("/webhook/emby", status_code=202)
async def emby_webhook(
    payload: EmbyPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    ai_client: AIClientWarper | None = Depends(get_ai_client),
    media_clients: dict[int, MediaService] = Depends(get_media_clients),
    notify_service: NotificationService = Depends(get_notification_service)
) -> Response:
    """处理来自 Emby 的 Webhook"""
    client = media_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    logger.info("收到来自 {} 的 Webhook, 事件类型: {}", client.server_name, payload.event)

    if isinstance(payload, LibraryNewEvent):
        if ai_client:
            await add_translate_media_item(server_instance.id, payload.item.id, days=1)

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.EMBY_LIBRARY_NEW,
                server_name=client.server_name,
                item=payload.item
            )
    elif isinstance(payload, LibraryDeletedEvent):
        if ai_client:
            await cancel_translate_media_item(server_instance.id, payload.item.id)

    return Response(content="Webhook received", status_code=200)

@router.post("/webhook/jellyfin", status_code=202)
async def jellyfin(
    payload: JellyfinPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    ai_client: AIClientWarper | None = Depends(get_ai_client),
    media_clients: dict[int, MediaService] = Depends(get_media_clients),
    notify_service: NotificationService = Depends(get_notification_service)
) -> Response:
    """处理来自 Jellyfin 的 Webhook"""
    client = media_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    logger.info("收到来自 {} 的 Webhook, 事件类型: {}", client.server_name, payload.notification_type)

    if payload.notification_type == NotificationType.ITEM_ADDED:
        if ai_client:
            await add_translate_media_item(server_instance.id, payload.item_id, days=1)

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.JELLYFIN_LIBRARY_NEW,
                server_name=client.server_name,
                item=payload.item_id
            )
    elif payload.notification_type == NotificationType.ITEM_DELETED:
        if ai_client:
            await cancel_translate_media_item(server_instance.id, payload.item_id)
    return Response(content="Webhook received", status_code=200)
