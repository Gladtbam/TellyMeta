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
from core.telegram_message import TelegramMessage
from models.nginx import Nginx, UrlInfo, format_traffic_bytes
from models.orm import MediaUser, ServerInstance, ServerType
from repositories.config_repo import ConfigRepository
from services.notification_service import NotificationService
from services.watch_time_service import expand_media_id_candidates, get_account_lock

if TYPE_CHECKING:
    from fastapi import FastAPI

    from services.media_service import MediaService

__all__ = [
    "TrafficService",
    "format_traffic_bytes",
    "get_or_create_traffic_service",
    "invalidate_traffic_server_cache",
    "record_traffic_webhook_session",
    "sync_traffic_listener",
]


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
    """渲染 80% 预警或 100% 耗尽的 Telegram HTML 私聊提醒消息

    Args:
        alert: 已提交账务后生成的配额告警上下文。

    Returns:
        经 t-string 自动转义后的安全 HTML 字符串。
    """
    used_str = format_traffic_bytes(alert.used_bytes)
    quota_str = format_traffic_bytes(alert.quota_bytes)
    reset_str = (
        alert.next_reset_at.strftime("%Y-%m-%d %H:%M")
        if alert.next_reset_at is not None
        else "下个账期"
    )
    server_name = alert.server_name
    media_name = alert.media_name
    if alert.level_percent >= 100:
        header = "🚨 <b>基础流量配额已耗尽（100%）</b>"
        footer = "当前处于软告警模式，暂不会触发超流停用，请注意控制播放码率。"
    else:
        header = "⚠️ <b>流量配额预警（已用达 80%）</b>"
        footer = "请注意合理规划观影清晰度。"

    return TelegramMessage.render_html(t"""\
        {header:raw}

        服务器: <code>{server_name}</code>
        媒体账号: <code>{media_name}</code>
        本周期已用: <code>{used_str}</code> / <code>{quota_str}</code>
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

        # (server_id, api_key/device_id) -> (media_user_id, expire_monotonic)
        self._token_cache: dict[tuple[int, str], tuple[str, float]] = {}
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

    def record_webhook_session(
        self,
        server_id: int,
        *,
        media_user_id: str | None,
        device_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """写入来自播放/认证 Webhook 的会话归因映射缓存

        Args:
            server_id: 媒体服务器实例 ID。
            media_user_id: 媒体平台侧用户 ID。
            device_id: 客户端设备标识。
            api_key: 客户端认证令牌。
        """
        if not media_user_id:
            return
        clean_uid = media_user_id.strip()
        if not clean_uid:
            return
        expires_at = self._monotonic() + self._attribution_cache_ttl_seconds
        if api_key and api_key.strip():
            self._token_cache[(server_id, api_key.strip())] = (clean_uid, expires_at)
        if device_id and device_id.strip():
            self._device_cache[(server_id, device_id.strip())] = (clean_uid, expires_at)

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

        if not log_entry.is_media_stream:
            return

        server = await self._resolve_server(log_entry)
        if server is None or not server.is_enabled or not server.enable_traffic:
            return

        url_info = log_entry.url_info
        if not url_info.api_key and not url_info.device_id:
            return

        media_user_id = await self._resolve_media_user_id(server.id, url_info)
        if not media_user_id:
            return

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
            matched = self._servers_by_netloc.get(host)
            if matched is not None:
                return matched
            bare_host = host.split(":", 1)[0]
            return self._servers_by_hostname.get(bare_host)

        return None

    def _lookup_cached_attribution(
        self,
        server_id: int,
        *,
        api_key: str | None,
        device_id: str | None,
    ) -> str | None:
        """从带 TTL 的内存归因缓存检索媒体平台用户 ID（Token 优先于设备 ID）

        Args:
            server_id: 目标服务器 ID。
            api_key: 可选客户端认证令牌。
            device_id: 可选客户端设备 ID。

        Returns:
            命中的媒体用户 ID；均未命中或已过期时返回 ``None``。
        """
        now = self._monotonic()
        if api_key:
            uid = _lookup_cache_entry(self._token_cache, (server_id, api_key), now)
            if uid is not None:
                return uid

        if device_id:
            return _lookup_cache_entry(self._device_cache, (server_id, device_id), now)

        return None

    async def _refresh_active_sessions_throttled(self, server_id: int) -> None:
        """带短期节流保护回查目标媒体服务器 ``/Sessions`` 接口并刷新归因缓存

        副作用：在节流窗口外触发外部 ``get_active_sessions()`` 请求并更新令牌/设备缓存。

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
                if not sess.user_id:
                    continue
                uid = sess.user_id.strip()
                if not uid:
                    continue
                if sess.auth_token and sess.auth_token.strip():
                    self._token_cache[(server_id, sess.auth_token.strip())] = (
                        uid,
                        expires_at,
                    )
                if sess.device_id and sess.device_id.strip():
                    self._device_cache[(server_id, sess.device_id.strip())] = (
                        uid,
                        expires_at,
                    )

    async def _resolve_media_user_id(
        self,
        server_id: int,
        url_info: UrlInfo,
    ) -> str | None:
        """优先命中精确 Token 缓存；遇未知 Token 时先节流回查 ``/Sessions`` 再回退设备缓存

        Args:
            server_id: 目标媒体服务器实例 ID。
            url_info: 解析出的请求 ``api_key`` 与 ``device_id`` 上下文。

        Returns:
            归因得到的媒体平台用户 ID；无法识别时返回 ``None``。
        """
        now = self._monotonic()
        api_key = url_info.api_key
        device_id = url_info.device_id

        # 1. 若存在 api_key 且已命中 Token 缓存，直接采用精确用户归因
        if api_key:
            token_uid = _lookup_cache_entry(
                self._token_cache, (server_id, api_key), now
            )
            if token_uid is not None:
                return token_uid
            # 未见过该 api_key 时先按节流策略回查一次 /Sessions，避免共享设备上旧 device_id 掩盖新登录用户
            await self._refresh_active_sessions_throttled(server_id)
            return self._lookup_cached_attribution(
                server_id, api_key=api_key, device_id=device_id
            )

        # 2. 无 api_key 时查询 device_id 缓存，未命中则按节流策略回查 /Sessions
        if device_id:
            dev_uid = _lookup_cache_entry(
                self._device_cache, (server_id, device_id), now
            )
            if dev_uid is not None:
                return dev_uid
            await self._refresh_active_sessions_throttled(server_id)
            return self._lookup_cached_attribution(
                server_id, api_key=None, device_id=device_id
            )

        return None

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

                            fresh_mu.cycle_traffic_bytes += delta_bytes
                            fresh_mu.total_traffic_bytes += delta_bytes
                            alert = self._evaluate_quota_alert(fresh_srv, fresh_mu)

                            await session.commit()
                            flushed_total += delta_bytes
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
    def _evaluate_quota_alert(
        server: ServerInstance,
        media_user: MediaUser,
    ) -> _PendingTrafficAlert | None:
        """评估并更新本周期 80%/100% 配额告警去重标志，满足条件时返回待发告警上下文

        Args:
            server: 当前服务器实例（含配额与通知开关）。
            media_user: 已累加本次字节增量的媒体账号 ORM 对象。

        Returns:
            达到阈值且尚未发送过该档位告警时返回 ``_PendingTrafficAlert``，否则返回 ``None``。
        """
        quota = int(server.traffic_quota_bytes or 0)
        if quota <= 0 or not server.enable_traffic_alert:
            return None

        used = int(media_user.cycle_traffic_bytes or 0)
        level: int | None = None
        if used >= quota and not media_user.traffic_alert_100_sent:
            # 直接跨越 100% 时同步置位 80% 去重标记，避免连续发送两条提醒
            media_user.traffic_alert_100_sent = True
            media_user.traffic_alert_80_sent = True
            level = 100
        # 使用整数交叉乘法等价判定 used / quota >= 4 / 5，消除浮点精度边界误差
        elif used * 5 >= quota * 4 and not media_user.traffic_alert_80_sent:
            media_user.traffic_alert_80_sent = True
            level = 80

        if level is None:
            return None

        return _PendingTrafficAlert(
            user_id=media_user.id,
            server_name=server.name,
            media_name=media_user.media_name,
            used_bytes=used,
            quota_bytes=quota,
            next_reset_at=media_user.next_reset_at,
            level_percent=level,
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
) -> None:
    """安全将 Webhook 携带的用户/设备映射写入当前 ``TrafficService`` 归因缓存

    Args:
        app: FastAPI 应用实例，为 ``None`` 时直接跳过。
        server_id: 触发 Webhook 的服务器 ID。
        media_user_id: 媒体平台侧用户 ID。
        device_id: 客户端设备标识。
        api_key: 可选客户端认证令牌。
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
