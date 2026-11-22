import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

import httpx2
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from loguru import logger

from clients.ai_client import AIClientWarper
from clients.cached_tmdb_client import CachedTmdbClient
from clients.cached_tvdb_client import CachedTvdbClient
from clients.emby_client import EmbyClient
from clients.jellyfin_client import JellyfinClient
from clients.qb_client import QbittorrentClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.config import get_settings
from core.database import (
    DATABASE_URL,
    IS_SQLITE,
    async_engine,
    async_session,
    get_sync_database_url,
)
from core.initialization import (
    check_mkvtoolnix,
    check_required_settings,
    check_sqlite_version,
    initialize_admin,
)
from core.scheduler_jobs import (
    SCHEDULER_JOBS_REGISTRY,
    get_ops_schedule_overrides,
    recover_and_calibrate_account_lifecycles,
)
from core.telegram_manager import TelethonClientWarper
from models.orm import ServerType
from repositories.config_repo import ConfigRepository
from repositories.server_repo import ServerRepository
from services.notification_service import (
    attach_external_client_hooks,
    get_or_create_notification_runtime,
)
from services.score_service import MessageTrackingState
from services.settings_service import SettingsServices
from services.traffic_service import sync_traffic_listener
from services.watch_time_service import recover_active_emby_heartbeats
from workers.mkv_worker import mkv_merge_task

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 设置时区
    os.environ["TZ"] = settings.timezone
    time.tzset()
    logger.info("时区已设置为 {}", settings.timezone)

    check_required_settings()
    if IS_SQLITE:
        check_sqlite_version()

    logger.info("启动应用程序生命周期上下文")

    # 启动时检测 mkvtoolnix 可用性并记录状态，供管理面板开关控制使用
    mkvtoolnix_available = check_mkvtoolnix()
    app.state.mkvtoolnix_available = mkvtoolnix_available
    if mkvtoolnix_available:
        app.state.task_queue = asyncio.Queue()
        app.state.mkv_worker = asyncio.create_task(
            mkv_merge_task(app.state.task_queue, app_state=app.state)
        )
    else:
        app.state.task_queue = None
        app.state.mkv_worker = None
    app.state.message_tracker = MessageTrackingState()

    app.state.ai_client = None
    app.state.tmdb_client = None
    app.state.tvdb_client = None
    app.state.qb_clients = {}  # dict[int, QbittorrentClient]

    app.state.db_engine = async_engine
    app.state.telethon_client = TelethonClientWarper(app)

    await app.state.telethon_client.connect()
    app.state.telethon_worker = asyncio.create_task(
        app.state.telethon_client.run_until_disconnected()
    )

    app.state.sonarr_clients = {}  # dict[int, SonarrClient]
    app.state.radarr_clients = {}  # dict[int, RadarrClient]
    app.state.media_clients = {}  # dict[int, MediaService]

    # 初始化管理员用户
    async with async_session() as session:
        try:
            admin_ids = await initialize_admin(session, app.state.telethon_client)
            app.state.admin_ids = set(admin_ids)
            await ConfigRepository.load_all_to_cache(session)
            settings_service = SettingsServices(app, session)
            await settings_service.reload_ai_client()
            await settings_service.reload_metadata_clients()

            servers = await ServerRepository(session).get_all_enabled()
            for server in servers:
                logger.info("正在加载服务器：[{}] {}", server.server_type, server.name)
                mappings = {}
                if server.path_mappings:
                    try:
                        mappings = json.loads(server.path_mappings)
                    except (json.JSONDecodeError, TypeError, ValueError) as e:
                        logger.error("解析服务器 {} 的路径映射失败: {}", server.name, e)
                match server.server_type:
                    case ServerType.SONARR:
                        sonarr_client = SonarrClient(
                            client=httpx2.AsyncClient(
                                base_url=server.url,
                                timeout=httpx2.Timeout(10.0, read=30.0),
                                proxy=settings.proxy or None,
                            ),
                            api_key=server.api_key,
                            server_name=server.name,
                            path_mappings=mappings,
                            notify_topic_id=server.notify_topic_id,
                            request_notify_topic_id=server.request_notify_topic_id,
                        )
                        attach_external_client_hooks(
                            sonarr_client,
                            app=app,
                            server_id=server.id,
                            server_name=server.name,
                            server_type=str(server.server_type),
                        )
                        app.state.sonarr_clients[server.id] = sonarr_client
                    case ServerType.RADARR:
                        radarr_client = RadarrClient(
                            client=httpx2.AsyncClient(
                                base_url=server.url,
                                timeout=httpx2.Timeout(10.0, read=30.0),
                                proxy=settings.proxy or None,
                            ),
                            api_key=server.api_key,
                            server_name=server.name,
                            path_mappings=mappings,
                            notify_topic_id=server.notify_topic_id,
                            request_notify_topic_id=server.request_notify_topic_id,
                        )
                        attach_external_client_hooks(
                            radarr_client,
                            app=app,
                            server_id=server.id,
                            server_name=server.name,
                            server_type=str(server.server_type),
                        )
                        app.state.radarr_clients[server.id] = radarr_client
                    case ServerType.JELLYFIN:
                        jf_client = JellyfinClient(
                            client=httpx2.AsyncClient(
                                base_url=f"{server.url}",
                                timeout=httpx2.Timeout(10.0, read=30.0),
                                proxy=settings.proxy or None,
                            ),
                            api_key=server.api_key,
                            server_name=server.name,
                            notify_topic_id=server.notify_topic_id,
                        )
                        attach_external_client_hooks(
                            jf_client,
                            app=app,
                            server_id=server.id,
                            server_name=server.name,
                            server_type=str(server.server_type),
                        )
                        app.state.media_clients[server.id] = jf_client
                    case ServerType.EMBY:
                        emby_client = EmbyClient(
                            client=httpx2.AsyncClient(
                                base_url=f"{server.url}/emby",
                                timeout=httpx2.Timeout(10.0, read=30.0),
                                proxy=settings.proxy or None,
                            ),
                            api_key=server.api_key,
                            server_name=server.name,
                            notify_topic_id=server.notify_topic_id,
                        )
                        attach_external_client_hooks(
                            emby_client,
                            app=app,
                            server_id=server.id,
                            server_name=server.name,
                            server_type=str(server.server_type),
                        )
                        app.state.media_clients[server.id] = emby_client
                    case ServerType.QBITTORRENT:
                        # 使用 username 时将 api_key 视为登录密码，否则视为 API Key
                        qb_client = QbittorrentClient(
                            client=httpx2.AsyncClient(
                                base_url=server.url,
                                timeout=httpx2.Timeout(10.0, read=30.0),
                                proxy=settings.proxy or None,
                            ),
                            username=server.qb_username or None,
                            password=server.api_key if server.qb_username else None,
                            api_key=(
                                None if server.qb_username else (server.api_key or None)
                            ),
                        )
                        attach_external_client_hooks(
                            qb_client,
                            app=app,
                            server_id=server.id,
                            server_name=server.name,
                            server_type=str(server.server_type),
                        )
                        app.state.qb_clients[server.id] = qb_client
        finally:
            await session.close()

    # 在 Telethon 就绪与配置缓存加载后启动异步通知投递后台队列
    notification_runtime = get_or_create_notification_runtime(app)
    notification_runtime.start()

    app.state.scheduler = AsyncIOScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(url=get_sync_database_url(DATABASE_URL)),
            # Emby 播放态按需心跳仅作用于运行期进程，使用内存 JobStore 避免重启残留无效持久化任务
            "memory": MemoryJobStore(),
        },
        timezone=settings.timezone,
    )

    overrides = get_ops_schedule_overrides()
    for job in SCHEDULER_JOBS_REGISTRY:
        job_id = job.kwargs.get("id")
        if job_id and job_id in overrides:
            trigger, trigger_args = overrides[job_id]
            merged_kwargs = {
                k: v
                for k, v in job.kwargs.items()
                if k not in ("day", "hour", "minute", "hours", "minutes")
            }
            merged_kwargs.update(trigger_args)
            app.state.scheduler.add_job(job.func, trigger, **merged_kwargs)
        else:
            app.state.scheduler.add_job(job.func, job.trigger, **job.kwargs)

    app.state.scheduler.start()
    # 若重启前存在未收尾的 Emby 活跃播放会话，恢复其内存心跳并由对账逻辑兜底结算
    await recover_active_emby_heartbeats(app)
    # 启动恢复校准全部媒体账号的生命周期单次调度节点与停机遗漏状态
    await recover_and_calibrate_account_lifecycles(app)

    # 根据全局流量总开关按需绑定 Nginx UDP 日志监听并启动周期刷盘协程
    try:
        await sync_traffic_listener(app)
    except OSError as exc:
        logger.error("启动 Nginx 流量 UDP 监听失败（已降级跳过）: {}", exc)

    yield

    logger.info("关闭应用程序生命周期上下文")

    traffic_service = getattr(app.state, "traffic_service", None)
    if traffic_service is not None:
        await traffic_service.stop_listener()

    if app.state.mkv_worker:
        app.state.mkv_worker.cancel()
        try:
            await app.state.mkv_worker
        except asyncio.CancelledError:
            logger.info("MKV 工作线程已取消")

    if app.state.scheduler.running:
        app.state.scheduler.shutdown(wait=True)
        logger.info("任务计划程序已关闭")

    ai_client: AIClientWarper | None = getattr(app.state, "ai_client", None)
    if ai_client:
        await ai_client.close()
    tmdb_client: CachedTmdbClient | None = getattr(app.state, "tmdb_client", None)
    if tmdb_client:
        await tmdb_client.close()
    tvdb_client: CachedTvdbClient | None = getattr(app.state, "tvdb_client", None)
    if tvdb_client:
        await tvdb_client.close()

    for client in app.state.qb_clients.values():
        await client.close()

    for client in app.state.sonarr_clients.values():
        await client.close()
    for client in app.state.radarr_clients.values():
        await client.close()
    for client in app.state.media_clients.values():
        await client.close()

    # 先排空并停止异步通知队列，再断开 Telethon 与释放数据库引擎
    notification_runtime = getattr(app.state, "notification_runtime", None)
    if notification_runtime is not None:
        await notification_runtime.stop(drain_timeout=10.0)

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
