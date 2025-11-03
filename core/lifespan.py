import asyncio
from contextlib import asynccontextmanager
import os
import time

import httpx
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from loguru import logger

from clients.ai_client import AIClientWarper
from clients.emby_client import EmbyClient
from clients.jellyfin_client import JellyfinClient
from clients.qb_client import QbittorrentClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.database import DATABASE_URL, Base, async_engine, async_session
from core.initialization import (check_sqlite_version, initialize_admin,
                                 initialize_bot_configuration)
from core.scheduler_jobs import (ban_expired_users,
                                 delete_expired_banned_users, settle_scores)
from core.telegram_manager import TelethonClientWarper
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

    app.state.tvdb_client = TvdbClient(
        client=httpx.AsyncClient(base_url='https://api4.thetvdb.com/v4'),
        api_key=settings.tvdb_api_key
    )

    if settings.media_server == 'emby':
        app.state.media_client = EmbyClient(
            client=httpx.AsyncClient(base_url=f'{settings.media_server_url}/emby'),
            api_key=settings.media_api_key
        )
    elif settings.media_server == 'jellyfin':
        app.state.media_client = JellyfinClient(
            client=httpx.AsyncClient(base_url=f'{settings.media_server_url}'),
            api_key=settings.media_api_key
        )
    else:
        logger.error(f"不支持的媒体服务器类型: {settings.media_server}")

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

    # 初始化管理员用户
    async with async_session() as session:
        admin_ids = await initialize_admin(session, app.state.telethon_client)
        app.state.admin_ids = set(admin_ids)
        await initialize_bot_configuration(session)
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
    await app.state.tvdb_client.close()
    if hasattr(app.state, 'media_client'):
        await app.state.media_client.close()

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
