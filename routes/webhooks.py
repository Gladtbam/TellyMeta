import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from loguru import logger

from clients.qb_client import QbittorrentClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.dependencies import (get_qb_client, get_task_queue, get_tmdb_client,
                               get_tvdb_client)
from models.emby import EmbyPayload
from models.jellyfin_webhook import JellyfinWebhookPayload
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
    logger.info("收到 Sonarr 的 {} 事件: {}", payload.eventType, payload)

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

@router.post("/webhook/emby", status_code=202)
async def emby_webhook(payload: EmbyPayload) -> Response:
    """处理来自 Emby 的 Webhook"""
    logger.info("收到 Emby 的 {} 事件: {}", payload.Event, payload)

    if payload.Event == 'library.new' and payload.Item:
        asyncio.create_task(
            translate_emby_item(payload.Item.Id)
            )
    if payload.Event == 'library.delete' and payload.Item:
        asyncio.create_task(
            cancel_translate_emby_item(payload.Item.Id)
            )

    return Response(content="Webhook received", status_code=200)

@router.get("/health", status_code=200)
async def health_check() -> dict[str, str]:
    """健康检查端点"""
    return {"status": "ok"}
