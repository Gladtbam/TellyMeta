import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request

from clients.ai_client import AIClientWarper
from clients.qb_client import QbittorrentClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.telegram_manager import TelethonClientWarper
from services.media_service import MediaService


def get_task_queue(request: Request) -> asyncio.Queue:
    """取共享的任务队列实例。"""
    return request.app.state.task_queue

def get_mkv_worker(request: Request) -> asyncio.Task:
    """获取 MKV 合并任务的工作线程。"""
    return request.app.state.mkv_worker

def get_ai_client(request: Request) -> AIClientWarper:
    """获取 AI 客户端实例。"""
    return request.app.state.ai_client

def get_qb_client(request: Request) -> QbittorrentClient:
    """获取 qBittorrent 客户端实例。    QbittorrentClient: 已初始化的 qBittorrent 客户端实例。
    """
    return request.app.state.qb_client

def get_tmdb_client(request: Request) -> TmdbClient:
    """获取 TMDB 客户端实例。"""
    return request.app.state.tmdb_client

def get_tvdb_client(request: Request) -> TvdbClient | None:
    """获取 TVDB 客户端实例。"""
    return request.app.state.tvdb_client

def get_media_clients(request: Request) -> dict[int, MediaService]:
    """获取媒体服务客户端实例。"""
    return request.app.state.media_clients

def get_sonarr_clients(request: Request) -> dict[int, SonarrClient]:
    """获取所有 Sonarr 客户端实例。"""
    return request.app.state.sonarr_clients

def get_radarr_clients(request: Request) -> dict[int, RadarrClient]:
    """获取所有 Radarr 客户端实例。"""
    return request.app.state.radarr_clients

def get_scheduler(request: Request) -> AsyncIOScheduler:
    """获取任务调度器实例。"""
    return request.app.state.scheduler

def get_telethon_client(request: Request) -> TelethonClientWarper:
    """获取 Telethon 客户端实例。

    Args:
        request (Request): FastAPI 请求对象。
    Returns:
        TelethonClientWarper: 已初始化的 Telethon 客户端实例。
    """
    return request.app.state.telethon_client
