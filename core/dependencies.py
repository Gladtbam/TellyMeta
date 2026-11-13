import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, Header, HTTPException, Request
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
    return NotificationService(request.app)


def get_telethon_client(request: Request) -> TelethonClientWarper:
    """获取 Telethon 客户端实例。"""
    return request.app.state.telethon_client


def _extract_webhook_token(
    authorization: str | None,
    x_webhook_token: str | None,
) -> str | None:
    """从请求标头中提取 Webhook 鉴权 Token。

    Args:
        authorization: HTTP Authorization 请求头，支持 ``Bearer <token>`` 格式。
        x_webhook_token: 自定义 ``X-Webhook-Token`` 请求头。

    Returns:
        提取并去除首尾空白后的 Token 字符串；若未提供或格式无效则返回 ``None``。
    """
    if x_webhook_token and x_webhook_token.strip():
        return x_webhook_token.strip()

    if authorization:
        scheme, _, credentials = authorization.strip().partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return credentials.strip()

    return None


async def get_server_by_token(
    authorization: str | None = Header(
        default=None, description="Bearer Webhook Token"
    ),
    x_webhook_token: str | None = Header(
        default=None, alias="X-Webhook-Token", description="Webhook Security Token"
    ),
    session: AsyncSession = Depends(get_db),
) -> ServerInstance:
    """通过 HTTP Header 标头验证 Webhook Token 并获取对应服务器实例。

    支持 ``X-Webhook-Token: <token>`` 与 ``Authorization: Bearer <token>`` 两种标头，
    不再从 URL 查询参数读取 Token 以避免密钥泄露到访问日志。

    Args:
        authorization: ``Authorization`` 请求头。
        x_webhook_token: ``X-Webhook-Token`` 请求头。
        session: 异步数据库会话。

    Returns:
        校验通过的 ``ServerInstance`` 对象。

    Raises:
        HTTPException: 缺少鉴权标头时返回 401；Token 无效时返回 403。
    """
    token = _extract_webhook_token(authorization, x_webhook_token)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="缺少 Webhook 鉴权标头（请使用 X-Webhook-Token 或 Authorization: Bearer）",
        )

    repo = ServerRepository(session)
    server = await repo.get_by_token(token)
    if not server:
        # 轻微延迟以降低暴力枚举 Token 的速率
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=403, detail="无效的 Webhook Token")
    return server
