import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from loguru import logger

from clients.qb_client import QbittorrentClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.dependencies import (get_qb_client, get_task_queue, get_tmdb_client,
                               get_tvdb_client)
from models.emby_webhook import (EmbyPayload, LibraryDeletedEvent,
                                 LibraryNewEvent)
from models.jellyfin_webhook import JellyfinPayload, NotificationType
from models.radarr import RadarrPayload, RadarrWebhookDownloadPayload
from models.sonarr import (SonarrPayload, SonarrWebhookDownloadPayload,
                           SonarrWebhookSeriesAddPayload)
from workers.nfo_worker import create_episode_nfo, create_series_nfo
from workers.translator_worker import (cancel_translate_emby_item,
                                       translate_emby_item)

router = APIRouter()
settings = get_settings()

@router.post("/webhook/sonarr", status_code=202)
async def sonarr_webhook(
    payload: SonarrPayload,
    tmdb_client: TmdbClient = Depends(get_tmdb_client),
    tvdb_client: TvdbClient = Depends(get_tvdb_client),
    task_queue: asyncio.Queue = Depends(get_task_queue),
    qb_client: QbittorrentClient = Depends(get_qb_client)
) -> Response:
    """处理来自 Sonarr 的 Webhook"""
    if isinstance(payload, SonarrWebhookSeriesAddPayload):
        asyncio.create_task(create_series_nfo(payload, tmdb_client))
    elif isinstance(payload, SonarrWebhookDownloadPayload):
        if payload.episodeFile and payload.episodeFile.path and 'VCB-Studio' not in payload.episodeFile.path:
            asyncio.create_task(create_episode_nfo(payload, tmdb_client, tvdb_client))
            await task_queue.put(Path(payload.episodeFile.path))

        if payload.downloadId:
            asyncio.create_task(qb_client.torrents_set_share_limits(
                torrent_hash=payload.downloadId,
                seeding_time_limit=settings.qbittorrent_torrent_limit
            ))

    return Response(content="Webhook received", status_code=200)

@router.post("/webhook/radarr", status_code=202)
async def radarrarr_webhook(
    payload: RadarrPayload,
    task_queue: asyncio.Queue = Depends(get_task_queue),
    qb_client: QbittorrentClient = Depends(get_qb_client)
) -> Response:
    """处理来自 Radarr 的 Webhook"""
    if isinstance(payload, RadarrWebhookDownloadPayload):
        if payload.movieFile and payload.movieFile.path and 'VCB-Studio' not in payload.movieFile.path:
            await task_queue.put(Path(payload.movieFile.path))

        if payload.downloadId:
            asyncio.create_task(qb_client.torrents_set_share_limits(
                torrent_hash=payload.downloadId,
                seeding_time_limit=settings.qbittorrent_torrent_limit
            ))

    return Response(content="Webhook received", status_code=200)

@router.post("/webhook/emby", status_code=202)
async def emby_webhook(
    payload: EmbyPayload,
    server_id: int = Query(..., description="TellyMeta Server ID")
) -> Response:
    """处理来自 Emby 的 Webhook"""
    if isinstance(payload, LibraryNewEvent):
        asyncio.create_task(
            translate_emby_item(server_id, payload.item.id)
        )
    elif isinstance(payload, LibraryDeletedEvent):
        asyncio.create_task(
            cancel_translate_emby_item(server_id, payload.item.id)
        )

    return Response(content="Webhook received", status_code=200)

@router.post("/webhook/jellyfin", status_code=202)
async def jellyfin(
    payload: JellyfinPayload,
    server_id: int = Query(..., description="TellyMeta Server ID")
) -> Response:
    """处理来自 Jellyfin 的 Webhook"""
    if payload.notification_type == NotificationType.ITEM_ADDED:
        asyncio.create_task(
            translate_emby_item(server_id, payload.item_id)
        )
    elif payload.notification_type == NotificationType.ITEM_DELETED:
        asyncio.create_task(
            cancel_translate_emby_item(server_id, payload.item_id)
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
