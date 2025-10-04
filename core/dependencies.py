import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from clients.ai_client import AIClientWarper
from clients.qb_client import QbittorrentClient
from clients.tmdb_client import TmdbService
from clients.tvdb_client import TvdbClient
from core.database import get_db
from core.telegram_manager import TelethonClientWarper
from services.media_service import MediaService


def get_task_queue(request: Request) -> asyncio.Queue:
    """取共享的任务队列实例。

    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        asyncio.Queue: 已初始化的任务队列实例。
    """
    # if not hasattr(request.app.state, 'task_queue'):
    #     request.app.state.task_queue = asyncio.Queue()
    return request.app.state.task_queue

def get_mkv_worker(request: Request) -> asyncio.Task:
    """获取 MKV 合并任务的工作线程。
    
    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        asyncio.Task: 已初始化的 MKV 合并任务工作线程。
    """
    return request.app.state.mkv_worker

def get_ai_client(request: Request) -> AIClientWarper:
    """获取 AI 客户端实例。
    
    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        AIClientWarper: 已初始化的 AI 客户端实例。
    """
    return request.app.state.ai_client

def get_qb_client(request: Request) -> QbittorrentClient:
    """获取 qBittorrent 客户端实例。

    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        QbittorrentClient: 已初始化的 qBittorrent 客户端实例。
    """
    return request.app.state.qb_client

def get_tmdb_client(request: Request) -> TmdbService:
    """获取 TMDB 客户端实例。

    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        TmdbService: 已初始化的 TMDB 客户端实例。
    """
    return request.app.state.tmdb_client

def get_tvdb_client(request: Request) -> TvdbClient:
    """获取 TVDB 客户端实例。

    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        TvdbClient: 已初始化的 TVDB 客户端实例。
    """
    return request.app.state.tvdb_client

def get_media_client(request: Request) -> MediaService | None:
    """获取媒体服务客户端实例。

    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        MediaService: 已初始化的媒体服务客户端实例。
    """
    return request.app.state.media_client

def get_scheduler(request: Request) -> AsyncIOScheduler:
    """获取任务调度器实例。

    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        AsyncIOScheduler: 已初始化的任务调度器实例。
    """
    return request.app.state.scheduler

# def get_tg_repo(db: AsyncSession = Depends(get_db)) -> TelegramUserRepository:
#     """获取 Telegram 用户仓库实例。
    
#     Args:
#         db (AsyncSession): 异步数据库会话。
#     Returns:
#         TelegramUserRepository: 已初始化的 Telegram 用户仓库实例。
#     """
#     return TelegramUserRepository(db)

def get_telethon_client(request: Request) -> TelethonClientWarper:
    """获取 Telethon 客户端实例。

    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        TelethonClientWarper: 已初始化的 Telethon 客户端实例。
    """
    return request.app.state.telethon_client
