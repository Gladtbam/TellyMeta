"""Nginx 流量 UDP 采集、异步会话归因与内存聚合刷盘服务"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from httpx2 import HTTPError
from loguru import logger
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.database import async_session
from core.economy_policy import (
    calculate_traffic_balances,
    evaluate_traffic_pack_access,
)
from core.media_utils import expand_media_id_candidates, get_account_lock
from core.telegram_message import TelegramMessage
from models.nginx import Nginx, UrlInfo, format_traffic_bytes
from models.orm import MediaUser, ServerInstance, ServerType
from repositories.config_repo import ConfigRepository
from services.media_service import (
    MediaService,
    compute_external_disabled_state,
    sync_external_disabled_state,
)
from services.notification_service import NotificationService

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "TrafficService",
    "format_traffic_bytes",
    "get_or_create_traffic_service",
    "invalidate_traffic_server_cache",
    "record_traffic_webhook_session",
    "sync_traffic_listener",
]


_CROSS_BIND_KEYS: dict[str, tuple[str, ...]] = {
    "api_key": ("play_session_id", "device_id"),
    "play_session_id": ("api_key", "device_id"),
    # 仅经由 device_id 兜底归因时只学习当次 play_session_id，避免共享设备上新 token 误绑旧账号
    "device_id": ("play_session_id",),
}


@dataclass(slots=True, frozen=True)
class _PendingTrafficAlert:
    """刷盘后待发送的流量配额告警上下文"""

    user_id: int
    server_name: str
    media_name: str
    used_bytes: int
    quota_bytes: int
    next_reset_at: datetime | None
    level_percent: int
    pack_bytes: int = 0
    auto_suspended: bool = False
    traffic_pack_open: bool = False


def _lookup_cache_entry(
    cache: dict[tuple[int, str], tuple[str, float]],
    key: tuple[int, str],
    now: float,
) -> str | None:
    """检索带单调时钟 TTL 的归因缓存条目，过期时自动淘汰

    Args:
        cache: 归因映射缓存字典，值为 ``(media_user_id, expire_monotonic)``。
        key: 复合缓存键 ``(server_id, token_or_device_id)``。
        now: 当前单调时钟秒数。

    Returns:
        未过期时返回媒体平台用户 ID；缺失或已过期时返回 ``None``。
    """
    cached = cache.get(key)
    if cached is None:
        return None
    uid, expires_at = cached
    if expires_at > now:
        return uid
    cache.pop(key, None)
    return None


def _render_quota_alert_html(alert: _PendingTrafficAlert) -> str:
    """渲染 80% 预警、100% 耗尽或超流自动停用的 Telegram HTML 私聊提醒消息

    Args:
        alert: 已提交账务后生成的配额告警上下文。

    Returns:
        经 t-string 自动转义后的安全 HTML 字符串。
    """
    used_str = format_traffic_bytes(alert.used_bytes)
    quota_str = format_traffic_bytes(alert.quota_bytes)
    pack_line = (
        f"\n剩余流量包: <code>{format_traffic_bytes(alert.pack_bytes)}</code>"
        if alert.pack_bytes > 0
        else ""
    )
    reset_str = (
        alert.next_reset_at.strftime("%Y-%m-%d %H:%M")
        if alert.next_reset_at is not None
        else "下个账期"
    )
    server_name = alert.server_name
    media_name = alert.media_name
    if alert.auto_suspended:
        header = "⛔ <b>流量已耗尽（100%），账号已超流停用</b>"
        pack_hint = (
            "可在 Mini App 使用积分兑换流量包立即恢复，或等待下次重置后自动解除。"
            if alert.traffic_pack_open
            else "请联系管理员临时调额恢复，或等待下次重置后自动解除。"
        )
        footer = (
            "本周期基础配额与流量包均已用尽，播放权限已临时停用（不会触发删号）。"
            f"{pack_hint}"
        )
    elif alert.level_percent >= 100:
        header = "🚨 <b>基础流量配额已耗尽（100%）</b>"
        footer = "当前处于软告警模式，暂不会触发超流停用，请注意控制播放码率。"
    else:
        header = "⚠️ <b>流量配额预警（已用达 80%）</b>"
        footer = "请注意合理规划观影清晰度。"

    return TelegramMessage.render_html(t"""\
        {header:raw}

        服务器: <code>{server_name}</code>
        媒体账号: <code>{media_name}</code>
        本周期已用: <code>{used_str}</code> / <code>{quota_str}</code>{pack_line:raw}
        下次重置时间: <code>{reset_str}</code>
        {footer:raw}
    """)


class TrafficService(asyncio.DatagramProtocol):
    """基于 asyncio UDP 的 Nginx 流媒体流量采集、异步会话归因与短周期刷盘服务"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        app: FastAPI | Any | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        session_query_throttle_seconds: float = 10.0,
        attribution_cache_ttl_seconds: float = 21600.0,
        server_cache_ttl_seconds: float = 5.0,
    ) -> None:
        """初始化 TrafficService 实例

        Args:
            session_factory: 异步数据库会话工厂（缺省使用全局 ``async_session``）。
            app: FastAPI 或等价应用状态宿主，用于访问 ``media_clients`` 与通知客户端。
            monotonic_clock: 可注入单调时钟函数，便于确定性测试节流窗口。
            session_query_throttle_seconds: 未知 Token/DeviceId 回查 ``/Sessions`` 的最短节流秒数。
            attribution_cache_ttl_seconds: 会话归因内存映射的有效期秒数。
            server_cache_ttl_seconds: 服务器配置快照的短期缓存秒数。
        """
        self._session_factory = session_factory or async_session
        self.app = app
        self._monotonic = monotonic_clock or time.monotonic
        self._session_query_throttle_seconds = max(0.0, session_query_throttle_seconds)
        self._attribution_cache_ttl_seconds = max(1.0, attribution_cache_ttl_seconds)
        self._server_cache_ttl_seconds = max(0.0, server_cache_ttl_seconds)

        self.transport: asyncio.DatagramTransport | None = None
        self._inflight_tasks: set[asyncio.Task[None]] = set()
        self._pending_bytes: dict[tuple[int, str], int] = defaultdict(int)
        self._flush_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_interval_seconds: int = 10
        self._bound_host: str | None = None
        self._bound_port: int | None = None

        # 归因缓存映射：(server_id, api_key/play_session_id/device_id) -> (媒体平台用户 ID, 单调时钟过期时间)
        self._token_cache: dict[tuple[int, str], tuple[str, float]] = {}
        self._play_session_cache: dict[tuple[int, str], tuple[str, float]] = {}
        self._device_cache: dict[tuple[int, str], tuple[str, float]] = {}
        self._last_session_query: dict[int, float] = {}
        self._server_query_locks: dict[int, asyncio.Lock] = {}

        # 服务器路由快照缓存
        self._servers_by_id: dict[int, ServerInstance] = {}
        self._servers_by_name: dict[str, ServerInstance] = {}
        self._servers_by_netloc: dict[str, ServerInstance] = {}
        self._servers_by_hostname: dict[str, ServerInstance] = {}
        self._server_cache_loaded_at: float | None = None
        self._server_cache_lock = asyncio.Lock()

    @property
    def is_listening(self) -> bool:
        """当前 UDP 套接字是否处于活跃监听状态"""
        return self.transport is not None and not self.transport.is_closing()

    @property
    def bound_address(self) -> tuple[str, int] | None:
        """返回当前实际绑定的 UDP 地址 ``(host, port)``，未监听时返回 ``None``"""
        if not self.is_listening or self.transport is None:
            return None
        sockname = self.transport.get_extra_info("sockname")
        if isinstance(sockname, tuple) and len(sockname) >= 2:
            return str(sockname[0]), int(sockname[1])
        if self._bound_host is not None and self._bound_port is not None:
            return self._bound_host, self._bound_port
        return None

    @property
    def pending_bytes_total(self) -> int:
        """返回当前内存缓冲区尚未刷盘的累计字节总数"""
        return sum(self._pending_bytes.values())

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """UDP DatagramEndpoint 建立回调

        Args:
            transport: 事件循环创建的底层 UDP 传输实例。
        """
        self.transport = transport  # type: ignore[assignment]
        logger.info("Nginx 流量 UDP 监听已启动")

    def connection_lost(self, exc: Exception | None) -> None:
        """UDP DatagramEndpoint 关闭或异常断开回调

        Args:
            exc: 若为异常关闭则携带对应异常对象，正常关闭时为 ``None``。
        """
        if exc is not None:
            logger.warning("Nginx 流量 UDP 监听异常断开: {}", exc)
        else:
            logger.info("Nginx 流量 UDP 监听已关闭")
        self.transport = None

    def invalidate_server_cache(self) -> None:
        """主动失效服务器路由缓存，促使下次日志按最新配置重新加载"""
        self._server_cache_loaded_at = None

    def _store_attribution_keys(
        self,
        server_id: int,
        media_user_id: str | None,
        *,
        expires_at: float,
        api_key: str | None = None,
        play_session_id: str | None = None,
        device_id: str | None = None,
    ) -> None:
        """将单个媒体账号的令牌、播放会话与设备标识统一写入对应 TTL 归因缓存

        Args:
            server_id: 媒体服务器实例 ID。
            media_user_id: 媒体平台侧用户 ID。
            expires_at: 缓存单调时钟到期时间戳。
            api_key: 可选客户端认证令牌。
            play_session_id: 可选播放会话 ID。
            device_id: 可选客户端设备标识。
        """
        if not media_user_id:
            return
        clean_uid = media_user_id.strip()
        if not clean_uid:
            return
        for cache, raw_key in (
            (self._token_cache, api_key),
            (self._play_session_cache, play_session_id),
            (self._device_cache, device_id),
        ):
            if raw_key and raw_key.strip():
                cache[(server_id, raw_key.strip())] = (clean_uid, expires_at)

    def record_webhook_session(
        self,
        server_id: int,
        *,
        media_user_id: str | None,
        device_id: str | None = None,
        api_key: str | None = None,
        play_session_id: str | None = None,
    ) -> None:
        """写入来自播放/认证 Webhook 的会话归因映射缓存

        Args:
            server_id: 媒体服务器实例 ID。
            media_user_id: 媒体平台侧用户 ID。
            device_id: 客户端设备标识。
            api_key: 客户端认证令牌。
            play_session_id: 可选播放会话 ID。
        """
        self._store_attribution_keys(
            server_id,
            media_user_id,
            expires_at=self._monotonic() + self._attribution_cache_ttl_seconds,
            api_key=api_key,
            play_session_id=play_session_id,
            device_id=device_id,
        )

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        """接收单条 UDP syslog/JSON 报文并投递异步处理（遵循 DatagramProtocol 同步契约）

        Args:
            data: UDP 原始字节负载。
            addr: 来源地址二元组 ``(host, port)``。
        """
        if not ConfigRepository.is_traffic_enabled_from_cache():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.process_datagram(data, addr))
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)

    async def process_datagram(
        self,
        data: bytes,
        addr: tuple[str | Any, int] = ("127.0.0.1", 0),
    ) -> None:
        """解析单条 UDP 日志报文、校验过滤条件并累加至对应账号的内存字节缓冲

        副作用：当报文通过流媒体过滤且成功归因时，累加字节增量到内存缓冲 ``_pending_bytes``。

        Args:
            data: 原始 UDP 报文字节。
            addr: 发送方地址二元组，仅用于诊断日志记录。
        """
        if not ConfigRepository.is_traffic_enabled_from_cache():
            return
        try:
            message = data.decode("utf-8")
        except UnicodeDecodeError:
            logger.debug("忽略非 UTF-8 UDP 流量报文: addr={}", addr)
            return

        # Nginx syslog 往往携带 "<pri>timestamp host tag: " 前缀，从首个 '{' 处剥离得到纯 JSON 体
        json_start = message.find("{")
        if json_start == -1:
            return

        try:
            log_entry = Nginx.model_validate_json(message[json_start:])
        except (ValidationError, ValueError) as exc:
            logger.debug(
                "解析 Nginx 流量 JSON 失败，已忽略: addr={}, err={}", addr, exc
            )
            return

        is_stream = log_entry.is_media_stream
        is_manifest = not is_stream and log_entry.is_playback_manifest
        if not is_stream and not is_manifest:
            return

        url_info = log_entry.url_info
        if is_manifest and not (
            url_info.play_session_id and (url_info.api_key or url_info.device_id)
        ):
            return
        if (
            not url_info.api_key
            and not url_info.play_session_id
            and not url_info.device_id
        ):
            return

        server = await self._resolve_server(log_entry)
        if server is None or not server.is_enabled or not server.enable_traffic:
            return

        media_user_id, resolved_via = await self._resolve_media_user_with_source(
            server.id, url_info
        )
        if not media_user_id:
            return

        bind_fields = _CROSS_BIND_KEYS.get(resolved_via or "", ())
        if bind_fields:
            self._store_attribution_keys(
                server.id,
                media_user_id,
                expires_at=self._monotonic() + self._attribution_cache_ttl_seconds,
                api_key=url_info.api_key if "api_key" in bind_fields else None,
                play_session_id=(
                    url_info.play_session_id
                    if "play_session_id" in bind_fields
                    else None
                ),
                device_id=url_info.device_id if "device_id" in bind_fields else None,
            )

        if is_stream:
            self._pending_bytes[(server.id, media_user_id)] += log_entry.body_bytes_sent

    async def _ensure_server_cache(self) -> None:
        """按短期 TTL 懒加载并刷新支持流量统计的媒体服务器映射索引

        副作用：过期或首次查询时访问数据库并原子替换内部路由字典。
        """
        now = self._monotonic()
        if (
            self._server_cache_loaded_at is not None
            and (now - self._server_cache_loaded_at) < self._server_cache_ttl_seconds
        ):
            return

        async with self._server_cache_lock:
            now = self._monotonic()
            if (
                self._server_cache_loaded_at is not None
                and (now - self._server_cache_loaded_at)
                < self._server_cache_ttl_seconds
            ):
                return

            async with self._session_factory() as session:
                stmt = select(ServerInstance).where(
                    ServerInstance.server_type.in_(
                        [ServerType.EMBY, ServerType.JELLYFIN]
                    ),
                    ServerInstance.is_enabled.is_(True),
                )
                servers = list((await session.execute(stmt)).scalars().all())

            by_id: dict[int, ServerInstance] = {}
            by_name: dict[str, ServerInstance] = {}
            by_netloc: dict[str, ServerInstance] = {}
            hostname_buckets: dict[str, list[ServerInstance]] = defaultdict(list)

            for srv in servers:
                by_id[srv.id] = srv
                if srv.name:
                    by_name[srv.name.strip().lower()] = srv
                if srv.url:
                    parsed = urlparse(srv.url.strip())
                    if parsed.netloc:
                        by_netloc[parsed.netloc.lower()] = srv
                    if parsed.hostname:
                        hostname_buckets[parsed.hostname.lower()].append(srv)

            # 仅当同一 hostname 下唯一对应一台服务器时才允许无端口 Host 回退匹配
            by_hostname = {
                host: items[0]
                for host, items in hostname_buckets.items()
                if len(items) == 1
            }

            self._servers_by_id = by_id
            self._servers_by_name = by_name
            self._servers_by_netloc = by_netloc
            self._servers_by_hostname = by_hostname
            self._server_cache_loaded_at = self._monotonic()

    def _match_server_by_host(self, raw_host: str) -> ServerInstance | None:
        """按精确 ``netloc`` 或唯一无端口 ``hostname`` 匹配已缓存服务器实例"""
        normalized = raw_host.strip().lower()
        if not normalized:
            return None
        matched = self._servers_by_netloc.get(normalized)
        if matched is not None:
            return matched
        bare_host = normalized.split(":", 1)[0]
        return self._servers_by_hostname.get(bare_host)

    async def _resolve_server(self, entry: Nginx) -> ServerInstance | None:
        """依据报文中的 server_id、server_name 或 Host 匹配对应的媒体服务器实例

        Args:
            entry: 已反序列化的 ``Nginx`` 日志条目。

        Returns:
            匹配到的已启用媒体服务器实例，未命中则返回 ``None``。
        """
        await self._ensure_server_cache()

        srv_id = entry.resolved_server_id
        if srv_id is not None and srv_id in self._servers_by_id:
            return self._servers_by_id[srv_id]

        srv_name = entry.resolved_server_name
        if srv_name:
            matched = self._servers_by_name.get(srv_name.lower())
            if matched is not None:
                return matched

        host = entry.resolved_host
        if host:
            matched = self._match_server_by_host(host)
            if matched is not None:
                return matched

        # Nginx 若以 "$server_name" 上报虚拟主机域名而非系统展示名，按 Host 再做一次回退匹配
        if srv_name:
            return self._match_server_by_host(srv_name)

        return None

    def _lookup_cached_attribution_with_source(
        self,
        server_id: int,
        *,
        api_key: str | None,
        play_session_id: str | None = None,
        device_id: str | None,
    ) -> tuple[str | None, str | None]:
        """从带 TTL 的内存归因缓存检索媒体账号平台 ID 及命中的标识类型

        Args:
            server_id: 目标服务器 ID。
            api_key: 可选客户端认证令牌。
            play_session_id: 可选播放会话 ID。
            device_id: 可选客户端设备 ID。

        Returns:
            ``(media_user_id, source_name)``；均未命中时返回 ``(None, None)``。
        """
        now = self._monotonic()
        for source_name, cache, key in (
            ("api_key", self._token_cache, api_key),
            ("play_session_id", self._play_session_cache, play_session_id),
            ("device_id", self._device_cache, device_id),
        ):
            if not key:
                continue
            uid = _lookup_cache_entry(cache, (server_id, key), now)
            if uid is not None:
                return uid, source_name

        return None, None

    async def _refresh_active_sessions_throttled(self, server_id: int) -> None:
        """带短期节流保护回查目标媒体服务器 ``/Sessions`` 接口并刷新归因缓存

        副作用：在节流窗口外触发外部 ``get_active_sessions()`` 请求并更新令牌/会话/设备缓存。

        Args:
            server_id: 目标媒体服务器实例 ID。
        """
        media_clients: dict[int, MediaService] = (
            getattr(getattr(self.app, "state", None), "media_clients", None) or {}
        )
        client = media_clients.get(server_id)
        if client is None:
            return

        lock = self._server_query_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            now = self._monotonic()
            last_query = self._last_session_query.get(server_id)
            if (
                last_query is not None
                and (now - last_query) < self._session_query_throttle_seconds
            ):
                return

            self._last_session_query[server_id] = now
            try:
                sessions = await client.get_active_sessions()
            except (HTTPError, ValueError, RuntimeError, OSError) as exc:
                logger.warning(
                    "回查服务器 {} 活跃会话用于流量归因失败: {}", server_id, exc
                )
                return

            expires_at = self._monotonic() + self._attribution_cache_ttl_seconds
            for sess in sessions:
                self._store_attribution_keys(
                    server_id,
                    sess.user_id,
                    expires_at=expires_at,
                    api_key=sess.auth_token,
                    play_session_id=sess.play_session_id,
                    device_id=sess.device_id,
                )

    async def _resolve_media_user_with_source(
        self,
        server_id: int,
        url_info: UrlInfo,
    ) -> tuple[str | None, str | None]:
        """按 Token -> PlaySessionId -> DeviceId 优先级检索缓存，并返回命中的归因来源

        Args:
            server_id: 目标媒体服务器实例 ID。
            url_info: 解析出的请求 ``api_key``、``play_session_id`` 与 ``device_id`` 上下文。

        Returns:
            ``(media_user_id, source_name)``，其中 ``source_name`` 为
            ``"api_key"`` / ``"play_session_id"`` / ``"device_id"`` 之一，未命中返回 ``(None, None)``。
        """
        now = self._monotonic()
        api_key = url_info.api_key
        play_session_id = url_info.play_session_id
        device_id = url_info.device_id

        # 优先检查最高优先级的非空归因键；未命中时先节流回查一次 /Sessions 再全链路回退，
        # 防止共享设备上的旧 device_id 缓存遮蔽携带新 Token 或新 PlaySessionId 的用户
        for source_name, cache, primary_key in (
            ("api_key", self._token_cache, api_key),
            ("play_session_id", self._play_session_cache, play_session_id),
            ("device_id", self._device_cache, device_id),
        ):
            if not primary_key:
                continue
            matched = _lookup_cache_entry(cache, (server_id, primary_key), now)
            if matched is not None:
                return matched, source_name
            await self._refresh_active_sessions_throttled(server_id)
            return self._lookup_cached_attribution_with_source(
                server_id,
                api_key=api_key,
                play_session_id=play_session_id,
                device_id=device_id,
            )

        return None, None

    async def _drain_inflight_tasks(self) -> None:
        """等待所有已入队的 UDP 解析/归因异步任务完成"""
        while self._inflight_tasks:
            pending = list(self._inflight_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _map_batch_to_accounts(
        self,
        batch: dict[tuple[int, str], int],
    ) -> dict[tuple[int, int], tuple[int, tuple[int, str]]]:
        """在独立只读事务中将 ``(server_id, media_user_id)`` 映射为账号主键聚合增量

        Args:
            batch: 内存中按 ``(server_id, media_user_id)`` 汇总的字节增量映射。

        Returns:
            以 ``(user_id, server_id)`` 为键、``(累加字节, 原始缓冲键)`` 为值的映射。
        """
        account_deltas: dict[tuple[int, int], tuple[int, tuple[int, str]]] = {}
        async with self._session_factory() as session:
            for (server_id, media_user_id), delta_bytes in batch.items():
                if delta_bytes <= 0:
                    continue
                candidates = expand_media_id_candidates(media_user_id)
                lookup_stmt = (
                    select(MediaUser.id)
                    .where(
                        MediaUser.server_id == server_id,
                        MediaUser.media_id.in_(candidates),
                    )
                    .limit(1)
                )
                user_id = (await session.execute(lookup_stmt)).scalar_one_or_none()
                if user_id is None:
                    continue
                acct_key = (user_id, server_id)
                if acct_key in account_deltas:
                    prev_bytes, orig_key = account_deltas[acct_key]
                    account_deltas[acct_key] = (prev_bytes + delta_bytes, orig_key)
                else:
                    account_deltas[acct_key] = (delta_bytes, (server_id, media_user_id))
        return account_deltas

    async def flush(self, *, force: bool = False) -> int:
        """将内存中聚合的账号流量按账号互斥锁隔离、逐账号提交落库并触发配额告警

        Args:
            force: 为 ``True`` 时即使全局/单服开关已关闭仍落库已采集的残余缓冲。

        Returns:
            本次成功持久化写入 SQLite 的总字节数。
        """
        async with self._flush_lock:
            await self._drain_inflight_tasks()
            if not self._pending_bytes:
                return 0

            batch = dict(self._pending_bytes)
            self._pending_bytes.clear()
            flushed_total = 0
            pending_alerts: list[_PendingTrafficAlert] = []

            try:
                account_deltas = await self._map_batch_to_accounts(batch)
            except SQLAlchemyError as exc:
                logger.exception(
                    "解析待刷盘流量账号归属失败，已回补缓冲等待下次重试: {}", exc
                )
                for key, delta in batch.items():
                    self._pending_bytes[key] += delta
                return 0

            for (user_id, server_id), (delta_bytes, orig_key) in account_deltas.items():
                # 逐账号持有互斥锁并独立开启短事务提交，避免锁释放与 commit 之间的陈旧快照覆盖
                async with get_account_lock(user_id, server_id):
                    try:
                        async with self._session_factory() as session:
                            fresh_srv = (
                                await session.execute(
                                    select(ServerInstance)
                                    .where(ServerInstance.id == server_id)
                                    .execution_options(populate_existing=True)
                                )
                            ).scalar_one_or_none()
                            if fresh_srv is None or (
                                not force
                                and (
                                    not fresh_srv.is_enabled
                                    or not fresh_srv.enable_traffic
                                )
                            ):
                                continue

                            fresh_mu = (
                                await session.execute(
                                    select(MediaUser)
                                    .where(
                                        MediaUser.id == user_id,
                                        MediaUser.server_id == server_id,
                                    )
                                    .execution_options(populate_existing=True)
                                )
                            ).scalar_one_or_none()
                            if fresh_mu is None:
                                continue

                            self._apply_traffic_consumption(
                                fresh_srv, fresh_mu, delta_bytes
                            )
                            # 先持久化已消费字节数，避免后续外部禁用 API 失败导致流量丢失或重复扣减
                            await session.commit()
                            flushed_total += delta_bytes

                            suspended_now = False
                            if self._should_auto_suspend(fresh_srv, fresh_mu):
                                suspended_now = await self._suspend_media_account(
                                    fresh_srv, fresh_mu
                                )

                            alert = self._evaluate_quota_alert(
                                fresh_srv, fresh_mu, auto_suspended=suspended_now
                            )
                            if suspended_now or alert is not None:
                                await session.commit()
                            if alert is not None:
                                pending_alerts.append(alert)
                    except SQLAlchemyError as exc:
                        logger.exception(
                            "刷盘账号流量失败，已回补该账号未提交增量: user_id={}, server_id={}, err={}",
                            user_id,
                            server_id,
                            exc,
                        )
                        self._pending_bytes[orig_key] += delta_bytes

            # 所有数据库事务均提交并释放账号锁后再异步发送 Bot 私聊告警，避免网络 IO 阻塞锁
            for alert in pending_alerts:
                await self._send_quota_alert_dm(alert)

            return flushed_total

    @staticmethod
    def _apply_traffic_consumption(
        server: ServerInstance,
        media_user: MediaUser,
        delta_bytes: int,
    ) -> None:
        """按「当期有效基础配额（含临时调额）-> 当期流量包」顺序扣减流量并累计历史总流量

        Args:
            server: 当前服务器实例配置。
            media_user: 待扣减的媒体账号 ORM 对象。
            delta_bytes: 本次增量消费字节数。
        """
        bal = calculate_traffic_balances(
            server.traffic_quota_bytes,
            media_user.cycle_traffic_adjustment_bytes,
            media_user.cycle_traffic_bytes,
            media_user.traffic_pack_bytes,
        )
        if bal.is_limited:
            from_base = min(delta_bytes, bal.base_remaining_bytes or 0)
            overflow = delta_bytes - from_base
            from_pack = min(overflow, bal.pack_remaining_bytes)
            unmet = overflow - from_pack
            # 未被当期流量包吸收的溢出字节记入周期消耗，确保软告警与超额审计保持完整
            media_user.cycle_traffic_bytes = bal.cycle_used_bytes + from_base + unmet
            media_user.traffic_pack_bytes = bal.pack_remaining_bytes - from_pack
        else:
            media_user.cycle_traffic_bytes = bal.cycle_used_bytes + delta_bytes
        media_user.total_traffic_bytes = (
            max(0, int(media_user.total_traffic_bytes or 0)) + delta_bytes
        )

    @staticmethod
    def _should_auto_suspend(
        server: ServerInstance,
        media_user: MediaUser,
    ) -> bool:
        """判断是否满足触发超流自动停用的全部前提条件

        仅当全局流量、服务器启用、单服流量、单服超流自动停用均开启且配置了有限配额、
        账号当前尚未停用且有效基础配额（含当期调额）与当期流量包剩余合计归零时返回 ``True``。

        Args:
            server: 目标媒体服务器配置实例。
            media_user: 待评估的媒体账号 ORM 对象。

        Returns:
            满足自动停用触发条件时返回 ``True``，否则返回 ``False``。
        """
        if not ConfigRepository.is_traffic_enabled_from_cache():
            return False
        bal = calculate_traffic_balances(
            server.traffic_quota_bytes,
            media_user.cycle_traffic_adjustment_bytes,
            media_user.cycle_traffic_bytes,
            media_user.traffic_pack_bytes,
        )
        if (
            not bal.is_limited
            or not server.is_enabled
            or not server.enable_traffic
            or not server.enable_traffic_auto_suspend
        ):
            return False
        if media_user.is_traffic_suspended:
            return False
        return (bal.total_remaining_bytes or 0) <= 0

    async def _suspend_media_account(
        self,
        server: ServerInstance,
        media_user: MediaUser,
    ) -> bool:
        """调用媒体服务器接口禁用播放权限并仅标记超流停用（绝不写入 is_banned 或 delete_at）

        Args:
            server: 目标服务器实例。
            media_user: 待停用的媒体账号 ORM 对象。

        Returns:
            成功置为超流停用返回 ``True``，外部禁用失败或客户端未连接返回 ``False``。
        """
        media_clients: dict[int, MediaService] = (
            getattr(getattr(self.app, "state", None), "media_clients", None) or {}
        )
        target_disabled = compute_external_disabled_state(
            is_banned=bool(media_user.is_banned),
            is_traffic_suspended=True,
        )
        outcome = await sync_external_disabled_state(
            media_clients.get(server.id),
            media_id=media_user.media_id,
            target_disabled=target_disabled,
            treat_404_as_synced=True,
            log_context="超流自动停用",
            user_id=media_user.id,
            server_id=server.id,
        )
        if not outcome.ok:
            return False

        # 严格遵循 ADR-0002：仅置位超流停用标志，绝不触碰 is_banned、delete_at 或 expires_at
        media_user.is_traffic_suspended = True
        return True

    @staticmethod
    def _evaluate_quota_alert(
        server: ServerInstance,
        media_user: MediaUser,
        *,
        auto_suspended: bool = False,
    ) -> _PendingTrafficAlert | None:
        """评估并更新本周期 80%/100% 配额告警去重标志，满足条件时返回待发告警上下文

        Args:
            server: 当前服务器实例（含配额与通知开关）。
            media_user: 已完成字节扣减与停用判定的媒体账号 ORM 对象。
            auto_suspended: 本次刷盘是否刚刚触发了超流自动停用。

        Returns:
            达到阈值且尚未发送过该档位告警时返回 ``_PendingTrafficAlert``，否则返回 ``None``。
        """
        bal = calculate_traffic_balances(
            server.traffic_quota_bytes,
            media_user.cycle_traffic_adjustment_bytes,
            media_user.cycle_traffic_bytes,
            media_user.traffic_pack_bytes,
        )
        if not bal.is_limited:
            return None

        used = bal.cycle_used_bytes
        pack_remaining = bal.pack_remaining_bytes
        total_remaining = bal.total_remaining_bytes or 0
        total_pool = bal.total_pool_bytes or 0

        level: int | None = None
        if total_remaining <= 0:
            # 开启自动停用时，仅在本次停用真正落定（或此前未通知且已停用）时发 100% 通知
            if (
                not server.enable_traffic_auto_suspend or auto_suspended
            ) and not media_user.traffic_alert_100_sent:
                # 直接跨越 100% 时同步置位 80% 去重标记，避免连续发送两条提醒
                media_user.traffic_alert_100_sent = True
                media_user.traffic_alert_80_sent = True
                level = 100
        # 使用整数交叉乘法等价判定 total_remaining / total_pool <= 1 / 5（剩余不足 20% 即已用达 80%）
        elif (
            total_pool > 0
            and total_remaining * 5 <= total_pool
            and not media_user.traffic_alert_80_sent
        ):
            media_user.traffic_alert_80_sent = True
            level = 80

        if level is None or not server.enable_traffic_alert:
            return None

        pack_open, _ = evaluate_traffic_pack_access(
            server,
            points_pack_allowed=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_TRAFFIC_PACK
            ),
            points_pack_reason="",
            global_traffic_enabled=ConfigRepository.is_traffic_enabled_from_cache(),
        )

        return _PendingTrafficAlert(
            user_id=media_user.id,
            server_name=server.name,
            media_name=media_user.media_name,
            used_bytes=used,
            quota_bytes=bal.effective_base_quota_bytes,
            next_reset_at=media_user.next_reset_at,
            level_percent=level,
            pack_bytes=pack_remaining,
            auto_suspended=auto_suspended,
            traffic_pack_open=pack_open,
        )

    async def _send_quota_alert_dm(self, alert: _PendingTrafficAlert) -> bool:
        """向关联 Telegram 用户发送流量配额预警或耗尽私聊通知（仅告警不触发停用）

        Args:
            alert: 配额告警上下文。

        Returns:
            Telegram 私聊送达成功返回 ``True``，未配置客户端或发送失败返回 ``False``。
        """
        if self.app is None or getattr(self.app.state, "telethon_client", None) is None:
            return False

        msg = _render_quota_alert_html(alert)
        try:
            return await NotificationService(self.app).send_text_to_user(
                alert.user_id, msg
            )
        except (HTTPError, ValueError, TypeError, OSError) as exc:
            logger.warning(
                "发送流量配额告警私聊失败: user_id={}, server={}, err={}",
                alert.user_id,
                alert.server_name,
                exc,
            )
            return False

    async def stop(self) -> int:
        """先关闭 UDP 监听传输阻断新报文入队，再强制刷盘所有残余内存缓冲

        Returns:
            停机收尾阶段成功持久化到数据库的字节数。
        """
        if self.transport is not None:
            with contextlib.suppress(Exception):
                self.transport.close()
            # 让出一次事件循环调度，确保底层 DatagramTransport 完成关闭且不再触发 datagram_received
            await asyncio.sleep(0)
            self.transport = None
        self._bound_host = None
        self._bound_port = None
        return await self.flush(force=True)

    async def _periodic_flush_loop(self, interval_seconds: int) -> None:
        """周期性将内存聚合的账号流量刷新至 SQLite"""
        interval = max(1, interval_seconds)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.flush()
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.exception("周期性刷盘流量数据异常: {}", exc)
        except asyncio.CancelledError:
            pass

    async def _cancel_flush_task(self) -> None:
        """取消后台定时刷盘任务并等待退出"""
        task = self._flush_task
        self._flush_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def start_listener(
        self,
        host: str = "0.0.0.0",
        port: int = 5140,
        flush_interval_seconds: int = 10,
    ) -> None:
        """按配置启动或热重载 UDP Datagram 监听与定时刷盘协程

        Args:
            host: 绑定监听主机地址。
            port: 绑定监听 UDP 端口。
            flush_interval_seconds: 内存聚合刷盘间隔秒数。
        """
        safe_interval = max(1, int(flush_interval_seconds))
        clean_host = (host or "0.0.0.0").strip() or "0.0.0.0"

        async with self._lifecycle_lock:
            same_endpoint = (
                self.is_listening
                and self._bound_host == clean_host
                and self._bound_port == port
            )
            if same_endpoint:
                if (
                    self._flush_interval_seconds != safe_interval
                    or self._flush_task is None
                ):
                    await self._cancel_flush_task()
                    self._flush_interval_seconds = safe_interval
                    self._flush_task = asyncio.create_task(
                        self._periodic_flush_loop(safe_interval)
                    )
                return

            # 端点变化或重新开启前，先清理旧定时任务并停机刷盘旧套接字
            await self._cancel_flush_task()
            if self.is_listening:
                await self.stop()

            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: self,
                local_addr=(clean_host, port),
            )
            self.transport = transport
            self._bound_host = clean_host
            self._bound_port = port
            self._flush_interval_seconds = safe_interval
            self._flush_task = asyncio.create_task(
                self._periodic_flush_loop(safe_interval)
            )

    async def stop_listener(self) -> int:
        """停止后台定时刷盘协程、关闭 UDP 监听并强制落库残余缓冲字节

        Returns:
            收尾阶段成功持久化写入 SQLite 的总字节数。
        """
        async with self._lifecycle_lock:
            await self._cancel_flush_task()
            return await self.stop()


def get_or_create_traffic_service(app: FastAPI | Any) -> TrafficService:
    """获取或初始化挂载在 ``app.state`` 上的单例 ``TrafficService`` 实例

    Args:
        app: FastAPI 或包含 ``state`` 的应用宿主对象。

    Returns:
        挂载在 ``app.state.traffic_service`` 的单例服务实例。
    """
    existing: TrafficService | None = getattr(app.state, "traffic_service", None)
    if existing is not None:
        return existing
    session_factory = getattr(app.state, "traffic_session_factory", None)
    service = TrafficService(session_factory=session_factory, app=app)
    app.state.traffic_service = service
    return service


async def sync_traffic_listener(app: FastAPI | Any) -> TrafficService:
    """根据缓存中的全局流量开关与 UDP 参数动态启停或重载 ``TrafficService`` 监听

    Args:
        app: FastAPI 或包含 ``state`` 的应用宿主对象。

    Returns:
        同步处理后的 ``TrafficService`` 实例。
    """
    service = get_or_create_traffic_service(app)
    if ConfigRepository.is_traffic_enabled_from_cache():
        cfg = ConfigRepository.get_traffic_config_from_cache()
        await service.start_listener(
            host=cfg["traffic_udp_host"],
            port=cfg["traffic_udp_port"],
            flush_interval_seconds=cfg["traffic_flush_interval_seconds"],
        )
    else:
        await service.stop_listener()
    return service


def record_traffic_webhook_session(
    app: FastAPI | Any | None,
    server_id: int,
    *,
    media_user_id: str | None,
    device_id: str | None = None,
    api_key: str | None = None,
    play_session_id: str | None = None,
) -> None:
    """安全将 Webhook 携带的用户/设备映射写入当前 ``TrafficService`` 归因缓存

    Args:
        app: FastAPI 应用实例，为 ``None`` 时直接跳过。
        server_id: 触发 Webhook 的服务器 ID。
        media_user_id: 媒体平台侧用户 ID。
        device_id: 客户端设备标识。
        api_key: 可选客户端认证令牌。
        play_session_id: 可选播放会话 ID。
    """
    if app is None:
        return
    service: TrafficService | None = getattr(app.state, "traffic_service", None)
    if service is None:
        return
    service.record_webhook_session(
        server_id,
        media_user_id=media_user_id,
        device_id=device_id,
        api_key=api_key,
        play_session_id=play_session_id,
    )


def invalidate_traffic_server_cache(app: FastAPI | Any | None) -> None:
    """服务器流量配置变更时失效 ``TrafficService`` 内部路由缓存

    Args:
        app: FastAPI 应用实例，为 ``None`` 时直接跳过。
    """
    if app is None:
        return
    service: TrafficService | None = getattr(app.state, "traffic_service", None)
    if service is not None:
        service.invalidate_server_cache()
