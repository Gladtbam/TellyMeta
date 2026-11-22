import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from clients.ai_client import AIClientWarper
from clients.qb_client import QbittorrentClient
from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.database import get_db
from core.telegram_manager import TelethonClientWarper
from models.orm import ServerInstance
from repositories.server_repo import ServerRepository
from services.media_service import MediaService
from services.notification_service import NotificationService


def get_task_queue(request: Request) -> asyncio.Queue | None:
    """取共享的任务队列实例。"""
    return request.app.state.task_queue


def get_mkv_worker(request: Request) -> asyncio.Task | None:
    """获取 MKV 合并任务的工作线程。"""
    return request.app.state.mkv_worker


def get_ai_client(request: Request) -> AIClientWarper | None:
    """获取 AI 客户端实例。"""
    return request.app.state.ai_client


def get_qb_clients(request: Request) -> dict[int, QbittorrentClient]:
    """获取所有 qBittorrent 客户端实例。"""
    return request.app.state.qb_clients


def get_tmdb_client(request: Request) -> TmdbClient | None:
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


def get_notification_service(request: Request) -> NotificationService:
    """获取绑定共享异步通知运行时的 ``NotificationService`` 实例。"""
    return NotificationService(request.app)


def get_telethon_client(request: Request) -> TelethonClientWarper:
    """获取 Telethon 客户端实例。"""
    return request.app.state.telethon_client


def _extract_webhook_token(
    authorization: str | None,
    x_webhook_token: str | None,
    query_token: str | None = None,
) -> str | None:
    """按优先级提取 Webhook 鉴权 Token：请求标头优先，查询参数作为可选退回方案。

    Args:
        authorization: HTTP Authorization 请求头，支持 ``Bearer <token>`` 格式。
        x_webhook_token: 自定义 ``X-Webhook-Token`` 请求头。
        query_token: URL 查询参数 ``?token=xxx``（用于不支持自定义请求头的 Emby 等客户端）。

    Returns:
        提取并去除首尾空白后的 Token 字符串；若均未提供或格式无效则返回 ``None``。
    """
    if x_webhook_token and x_webhook_token.strip():
        return x_webhook_token.strip()

    if authorization:
        scheme, _, credentials = authorization.strip().partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return credentials.strip()

    # 退回方案：兼容不支持自定义请求头的 Webhook 客户端（如 Emby）
    if query_token and query_token.strip():
        return query_token.strip()

    return None


async def get_server_by_token(
    authorization: str | None = Header(
        default=None, description="Bearer Webhook Token"
    ),
    x_webhook_token: str | None = Header(
        default=None, alias="X-Webhook-Token", description="Webhook Security Token"
    ),
    query_token: str | None = Query(
        default=None,
        alias="token",
        description="可选的查询参数退回 Token（适用于不支持自定义请求头的 Emby 等客户端）",
    ),
    session: AsyncSession = Depends(get_db),
) -> ServerInstance:
    """验证 Webhook Token 并获取对应服务器实例。

    优先从 ``X-Webhook-Token: <token>`` 或 ``Authorization: Bearer <token>`` 请求标头读取；
    当未提供上述标头时，允许退回到可选的 ``?token=<token>`` 查询参数（兼容不支持自定义标头的 Emby）。

    Args:
        authorization: ``Authorization`` 请求头。
        x_webhook_token: ``X-Webhook-Token`` 请求头。
        query_token: 可选的 ``token`` 查询参数。
        session: 异步数据库会话。

    Returns:
        校验通过的 ``ServerInstance`` 对象。

    Raises:
        HTTPException: 缺少鉴权信息时返回 401；Token 无效时返回 403。
    """
    token = _extract_webhook_token(authorization, x_webhook_token, query_token)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="缺少 Webhook 鉴权信息（请使用 X-Webhook-Token、Authorization: Bearer 或 ?token= 参数）",
        )

    repo = ServerRepository(session)
    server = await repo.get_by_token(token)
    if not server:
        # 轻微延迟以降低暴力枚举 Token 的速率
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=403, detail="无效的 Webhook Token")
    return server
