import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from loguru import logger

from clients.qb_client import QbittorrentClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.dependencies import (get_media_clients, get_notification_service,
                               get_qb_client, get_radarr_clients,
                               get_server_by_token, get_sonarr_clients,
                               get_task_queue, get_tmdb_client,
                               get_tvdb_client)
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
from workers.nfo_worker import create_episode_nfo, create_series_nfo
from workers.translator_worker import (cancel_translate_media_item,
                                       translate_media_item)

router = APIRouter()
settings = get_settings()

@router.post("/webhook/sonarr", status_code=202)
async def sonarr_webhook(
    payload: SonarrPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    sonarr_clients: dict[int, SonarrClient] = Depends(get_sonarr_clients),
    tmdb_client: TmdbClient = Depends(get_tmdb_client),
    tvdb_client: TvdbClient = Depends(get_tvdb_client),
    task_queue: asyncio.Queue = Depends(get_task_queue),
    qb_client: QbittorrentClient = Depends(get_qb_client),
    notify_service: NotificationService = Depends(get_notification_service)
) -> Response:
    """处理来自 Sonarr 的 Webhook"""
    client = sonarr_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    if isinstance(payload, SonarrWebhookSeriesAddPayload):
        if mapped_path := client.to_local_path(payload.series.path):
            payload.series.path = mapped_path
        asyncio.create_task(create_series_nfo(payload, tmdb_client))

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
            asyncio.create_task(create_episode_nfo(payload, tmdb_client, tvdb_client))
            await task_queue.put(Path(payload.episodeFile.path))

        if payload.downloadId:
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
    qb_client: QbittorrentClient = Depends(get_qb_client),
    notify_service: NotificationService = Depends(get_notification_service)
) -> Response:
    """处理来自 Radarr 的 Webhook"""
    client = radarr_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    if isinstance(payload, RadarrWebhookDownloadPayload):
        if payload.movieFile and payload.movieFile.path and 'VCB-Studio' not in payload.movieFile.path:
            if mapped_path := client.to_local_path(payload.movieFile.path):
                payload.movieFile.path = mapped_path
            await task_queue.put(Path(payload.movieFile.path))

        if payload.downloadId:
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
    media_clients: dict[int, MediaService] = Depends(get_media_clients),
    notify_service: NotificationService = Depends(get_notification_service)
) -> Response:
    """处理来自 Emby 的 Webhook"""
    client = media_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    if isinstance(payload, LibraryNewEvent):
        asyncio.create_task(
            translate_media_item(server_instance.id, payload.item.id)
        )

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.EMBY_LIBRARY_NEW,
                server_name=client.server_name,
                item=payload.item
            )
    elif isinstance(payload, LibraryDeletedEvent):
        asyncio.create_task(
            cancel_translate_media_item(server_instance.id, payload.item.id)
        )

    return Response(content="Webhook received", status_code=200)

@router.post("/webhook/jellyfin", status_code=202)
async def jellyfin(
    payload: JellyfinPayload,
    server_instance: ServerInstance = Depends(get_server_by_token),
    media_clients: dict[int, MediaService] = Depends(get_media_clients),
    notify_service: NotificationService = Depends(get_notification_service)
) -> Response:
    """处理来自 Jellyfin 的 Webhook"""
    client = media_clients.get(server_instance.id)
    if not client:
        return Response(content="Webhook received (Client Not Found)", status_code=200)

    if payload.notification_type == NotificationType.ITEM_ADDED:
        asyncio.create_task(
            translate_media_item(server_instance.id, payload.item_id)
        )

        if client.notify_topic_id:
            await notify_service.send_to_topic(
                topic_id=client.notify_topic_id,
                event_type=NotificationEvent.JELLYFIN_LIBRARY_NEW,
                server_name=client.server_name,
                item=payload.item_id
            )
    elif payload.notification_type == NotificationType.ITEM_DELETED:
        asyncio.create_task(
            cancel_translate_media_item(server_instance.id, payload.item_id)
        )
    return Response(content="Webhook received", status_code=200)

@router.get("/health", status_code=200)
async def health_check() -> dict[str, str]:
    """健康检查端点"""
    return {"status": "ok"}

@router.post("/webhook/test", status_code=202)
async def test_webhook(payload: dict[str, Any]) -> Response:
    logger.info("收到 test 事件： {}", payload)
    return Response(status_code=200)
