import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from loguru import logger

from clients.qb_client import QbittorrentClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.dependencies import (get_qb_client, get_task_queue, get_tmdb_client,
                               get_tvdb_client)
from models.emby import EmbyPayload
from models.sonarr import SonarrPayload
from workers.nfo_worker import create_episode_nfo, create_series_nfo
from workers.translator_worker import (cancel_translate_emby_item,
                                       translate_emby_item)

router = APIRouter()

@router.post("/webhooks/sonarr", status_code=202)
async def sonarr_webhook(
    payload: SonarrPayload,
    tmdb_client: TmdbClient = Depends(get_tmdb_client),
    tvdb_client: TvdbClient = Depends(get_tvdb_client),
    task_queue: asyncio.Queue = Depends(get_task_queue),
    qb_client: QbittorrentClient = Depends(get_qb_client)
) -> Response:
    """处理来自 Sonarr 的 Webhook"""

    logger.info("收到 Sonarr 的 Webhook：{}", payload)

    if payload.eventType == 'Test':
        logger.info("收到 Sonarr 测试事件")
    elif payload.eventType == 'SeriesAdd':
        asyncio.create_task(create_series_nfo(payload, tmdb_client))
    elif payload.eventType == 'Download':
        if payload.episodeFile and payload.episodeFile.path and 'VCB-Studio' not in payload.episodeFile.path:
            asyncio.create_task(create_episode_nfo(payload, tmdb_client, tvdb_client))
            await task_queue.put(Path(payload.episodeFile.path))
        if payload.downloadId:
            asyncio.create_task(
                qb_client.torrents_set_share_limits(torrent_hash=payload.downloadId, seeding_time_limit=1440)
            )
    elif payload.eventType in [
            "Grab", "Rename", "SeriesDelete", "EpisodeFileDelete", "Health",
            "ApplicationUpdate", "HealthRestored", "ManualInteractionRequired"]:
        logger.info("已收到 Sonarr {} 事件", payload.eventType)
    else:
        logger.warning("未处理的 Sonarr 事件类型：{}", payload.eventType)

    return Response(content="Webhook received", status_code=200)

@router.post("/webhooks/emby", status_code=202)
async def emby_webhook(payload: EmbyPayload) -> Response:
    """处理来自 Emby 的 Webhook"""
    logger.info("收到 Emby 的 Webhook：{}", payload)

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
