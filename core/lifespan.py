import asyncio
import os
import time
from contextlib import asynccontextmanager

import httpx
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from loguru import logger

from clients.ai_client import AIClientWarper
from clients.emby_client import EmbyClient
from clients.jellyfin_client import JellyfinClient
from clients.qb_client import QbittorrentClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.database import DATABASE_URL, Base, async_engine, async_session
from core.initialization import check_sqlite_version, initialize_admin
from core.scheduler_jobs import (ban_expired_users,
                                 delete_expired_banned_users, settle_scores)
from core.telegram_manager import TelethonClientWarper
from models.orm import ServerType
from repositories.config_repo import ConfigRepository
from repositories.server_repo import ServerRepository
from services.score_service import MessageTrackingState
from workers.mkv_worker import mkv_merge_task

settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 设置时区
    os.environ['TZ'] = settings.timezone
    time.tzset()
    logger.info("时区已设置为 {}", settings.timezone)

    check_sqlite_version()

    logger.info("启动应用程序生命周期上下文")

    app.state.task_queue = asyncio.Queue()
    app.state.mkv_worker = asyncio.create_task(mkv_merge_task(app.state.task_queue))
    app.state.message_tracker = MessageTrackingState()

    app.state.ai_client = AIClientWarper(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model
    )

    app.state.qb_client = QbittorrentClient(
        client=httpx.AsyncClient(base_url=settings.qbittorrent_base_url),
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password
    )

    app.state.tmdb_client = TmdbClient(
        client=httpx.AsyncClient(base_url='https://api.themoviedb.org/3'),
        api_key=settings.tmdb_api_key
    )

    if settings.tvdb_api_key:
        app.state.tvdb_client = TvdbClient(
            client=httpx.AsyncClient(base_url='https://api4.thetvdb.com/v4'),
            api_key=settings.tvdb_api_key
        )
    else:
        app.state.tvdb_client = None

    app.state.db_engine = async_engine
    app.state.telethon_client = TelethonClientWarper(app)

    async def create_db_tables():
        async with app.state.db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    await asyncio.gather(
        create_db_tables(),
        app.state.telethon_client.connect()
    )
    app.state.telethon_worker = asyncio.create_task(app.state.telethon_client.run_until_disconnected())

    app.state.sonarr_clients = {} # dict[int, SonarrClient]
    app.state.radarr_clients = {} # dict[int, RadarrClient]
    app.state.media_clients = {} # dict[int, MediaService]

    # 初始化管理员用户
    async with async_session() as session:
        try:
            admin_ids = await initialize_admin(session, app.state.telethon_client)
            app.state.admin_ids = set(admin_ids)
            await ConfigRepository.load_all_to_cache(session)

            servers = await ServerRepository(session).get_all_enabled()
            for server in servers:
                logger.info("正在加载服务器：[{}] {}", server.server_type, server.name)
                match server.server_type:
                    case ServerType.SONARR:
                        app.state.sonarr_clients[server.id] = SonarrClient(
                            client=httpx.AsyncClient(base_url=server.url),
                            api_key=server.api_key
                        )
                    case ServerType.RADARR:
                        app.state.radarr_clients[server.id] = RadarrClient(
                            client=httpx.AsyncClient(base_url=server.url),
                            api_key=server.api_key
                        )
                    case ServerType.JELLYFIN:
                        app.state.media_clients[server.id] = JellyfinClient(
                            client=httpx.AsyncClient(base_url=f'{server.url}'),
                            api_key=server.api_key
                        )
                    case ServerType.EMBY:
                        app.state.media_clients[server.id] = EmbyClient(
                            client=httpx.AsyncClient(base_url=f'{server.url}/emby'),
                            api_key=server.api_key
                        )
        finally:
            await session.close()

    app.state.scheduler = AsyncIOScheduler(
        jobstores={'default': SQLAlchemyJobStore(url=DATABASE_URL.replace("+aiosqlite", ""))},
        timezone=settings.timezone
        )

    app.state.scheduler.add_job(
        ban_expired_users,
        'cron',
        hour=0, minute=15,
        id='ban_expired_users',
        replace_existing=True
    )

    app.state.scheduler.add_job(
        delete_expired_banned_users,
        'cron',
        hour=0, minute=30,
        id='delete_expired_banned_users',
        replace_existing=True
    )

    app.state.scheduler.add_job(
        settle_scores,
        'cron',
        hour='8, 20',
        id='settle_scores',
        replace_existing=True
    )

    app.state.scheduler.start()

    yield

    logger.info("关闭应用程序生命周期上下文")
    app.state.mkv_worker.cancel()
    try:
        await app.state.mkv_worker
    except asyncio.CancelledError:
        logger.info("MKV 工作线程已取消")

    if app.state.scheduler.running:
        app.state.scheduler.shutdown(wait=True)
        logger.info("任务计划程序已关闭")

    await app.state.qb_client.close()
    if app.state.tvdb_client:
        await app.state.tvdb_client.close()

    for client in app.state.sonarr_clients.values():
        await client.close()
    for client in app.state.radarr_clients.values():
        await client.close()
    for client in app.state.media_clients.values():
        await client.close()

    if await app.state.telethon_client.is_connected():
        await app.state.telethon_client.disconnect()
    app.state.telethon_worker.cancel()
    try:
        await app.state.telethon_worker
    except asyncio.CancelledError:
        logger.info("Telethon 工作线程已取消")

    if app.state.db_engine:
        await app.state.db_engine.dispose()
    logger.info("应用程序生命周期上下文已关闭")
