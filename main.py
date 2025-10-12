import asyncio
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, Request, Response
from loguru import logger

import bot.handlers
from clients.ai_client import AIClientWarper
from clients.qb_client import QbittorrentClient
from clients.tmdb_client import TmdbService
from core.config import setup_logging
from core.dependencies import (get_ai_client, get_media_client, get_qb_client,
                               get_scheduler, get_task_queue, get_tmdb_client)
from core.lifespan import lifespan
from models.emby import EmbyPayload
from models.sonarr import SonarrPayload
from services.media_service import MediaService
from workers.nfo_worker import create_episode_nfo, create_series_nfo
from workers.translator_worker import translate_emby_item

setup_logging()

app = FastAPI(lifespan=lifespan)

@app.post("/webhooks/sonarr", status_code=202)
async def sonarr_webhook(
    payload: SonarrPayload,
    tmdb_client: TmdbService = Depends(get_tmdb_client),
    task_queue: asyncio.Queue = Depends(get_task_queue),
    qb_client: QbittorrentClient = Depends(get_qb_client)
) -> Response:
    """处理来自 Sonarr 的 Webhook"""

    logger.info("收到 Sonarr 的 Webhook：%s", payload)

    if payload.eventType == 'Test':
        logger.info("收到 Sonarr 测试事件")
    elif payload.eventType == 'SeriesAdd':
        asyncio.create_task(create_series_nfo(payload, tmdb_client))
    elif payload.eventType == 'Download':
        if payload.episodeFile and payload.episodeFile.path and 'VCB-Studio' not in payload.episodeFile.path:
            asyncio.create_task(create_episode_nfo(payload, tmdb_client))
            await task_queue.put(Path(payload.episodeFile.path)) # type: ignore
        if payload.downloadId:
            asyncio.create_task(
                qb_client.torrents_set_share_limits(torrent_hash=payload.downloadId, seeding_time_limit=1440) # type: ignore
            )
    elif payload.eventType in ["Grab", "Rename", "SeriesDelete", "EpisodeFileDelete", "Health", "ApplicationUpdate", "HealthRestored", "ManualInteractionRequired"]:
        logger.info("已收到 Sonarr %s 事件", payload.eventType)
    else:
        logger.warning("未处理的 Sonarr 事件类型：%s", payload.eventType)

    return Response(content="Webhook received", status_code=200)

@app.post("/webhooks/emby", status_code=202)
async def emby_webhook(payload: EmbyPayload) -> Response:
    """处理来自 Emby 的 Webhook"""
    logger.info("收到 Emby 的 Webhook：%s", payload)

    if payload.Event == 'library.new' and payload.Item:
        asyncio.create_task(
            translate_emby_item(payload.Item.Id)
            )

    return Response(content="Webhook received", status_code=200)

@app.get("/health", status_code=200)
async def health_check() -> dict[str, str]:
    """健康检查端点"""
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5080, log_level="info", reload=False)
