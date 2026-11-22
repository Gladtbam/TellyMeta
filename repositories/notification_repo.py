"""通知路由、站内兜底收件箱、运维告警日志与用户通知偏好仓储"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.events import (
    DEFAULT_GLOBAL_ROUTE_TARGETS,
    USER_PREFERENCE_KEYS,
    NotificationRouteCategory,
    NotificationRouteScope,
    NotificationTargetType,
)
from models.orm import (
    NotificationInboxItem,
    NotificationRoute,
    OpsAlertLog,
    UserNotificationPreference,
)


def _rowcount(result: object) -> int:
    """安全提取 SQLAlchemy DML 执行结果影响行数"""
    if isinstance(result, CursorResult):
        return int(result.rowcount or 0)
    return int(getattr(result, "rowcount", 0) or 0)


class NotificationRouteRepository:
    """分层通知路由仓储（支持全局默认与按服务器实例覆盖）"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def parse_enabled_events(raw_json: str | None) -> set[str]:
        """将路由记录中的可选事件 JSON 文本安全解析为事件名集合

        Args:
            raw_json: 数据库中存储的 JSON 字符串（可为空）。

        Returns:
            合法字符串事件标识集合；解析失败或非列表时返回空集合。
        """
        if not raw_json:
            return set()
        try:
            parsed = json.loads(raw_json)
        except TypeError, ValueError:
            return set()
        if not isinstance(parsed, list):
            return set()
        return {str(item) for item in parsed if isinstance(item, str) and item.strip()}

    @staticmethod
    def serialize_enabled_events(events: Sequence[str] | None) -> str | None:
        """将可选事件列表规范化为排序去重后的 JSON 字符串"""
        if events is None:
            return None
        cleaned = sorted({str(item).strip() for item in events if str(item).strip()})
        if not cleaned:
            return None
        return json.dumps(cleaned, ensure_ascii=False)

    async def get_global_routes(self) -> Sequence[NotificationRoute]:
        """获取全部全局默认路由规则，按类别升序返回"""
        stmt = (
            select(NotificationRoute)
            .where(NotificationRoute.scope_type == NotificationRouteScope.GLOBAL)
            .order_by(NotificationRoute.category.asc())
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_server_routes(self, server_id: int) -> Sequence[NotificationRoute]:
        """获取指定服务器实例的全部覆盖路由规则"""
        stmt = (
            select(NotificationRoute)
            .where(
                NotificationRoute.scope_type == NotificationRouteScope.SERVER,
                NotificationRoute.server_id == server_id,
            )
            .order_by(NotificationRoute.category.asc())
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_all_routes(self) -> Sequence[NotificationRoute]:
        """获取全部全局与单服覆盖路由规则"""
        stmt = (
            select(NotificationRoute)
            .order_by(
                NotificationRoute.scope_type.asc(),
                NotificationRoute.server_id.asc(),
                NotificationRoute.category.asc(),
            )
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_route(
        self,
        *,
        scope_type: NotificationRouteScope | str,
        category: NotificationRouteCategory | str,
        server_id: int | None = None,
        fresh: bool = True,
    ) -> NotificationRoute | None:
        """精确查询单条全局或单服覆盖路由规则"""
        normalized_scope = NotificationRouteScope(scope_type)
        normalized_cat = NotificationRouteCategory(category)
        conditions = [
            NotificationRoute.scope_type == normalized_scope,
            NotificationRoute.category == normalized_cat,
        ]
        if normalized_scope == NotificationRouteScope.GLOBAL:
            conditions.append(NotificationRoute.server_id.is_(None))
        else:
            if server_id is None:
                return None
            conditions.append(NotificationRoute.server_id == server_id)

        stmt = select(NotificationRoute).where(*conditions)
        if fresh:
            stmt = stmt.execution_options(populate_existing=True)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def resolve_effective_route(
        self,
        category: NotificationRouteCategory | str,
        server_id: int | None = None,
    ) -> NotificationRoute | None:
        """按分层优先级解析生效路由（单服覆盖优先，未配置覆盖时回退全局默认）

        Args:
            category: 通知事件所属路由类别。
            server_id: 关联服务器实例 ID（为空时仅查询全局默认路由）。

        Returns:
            命中且生效的 ``NotificationRoute``，都不存在时返回 ``None``。
        """
        normalized_cat = NotificationRouteCategory(category)
        if server_id is not None:
            server_route = await self.get_route(
                scope_type=NotificationRouteScope.SERVER,
                category=normalized_cat,
                server_id=server_id,
                fresh=True,
            )
            if server_route is not None:
                return server_route
        return await self.get_route(
            scope_type=NotificationRouteScope.GLOBAL,
            category=normalized_cat,
            server_id=None,
            fresh=True,
        )

    async def upsert_route(
        self,
        *,
        scope_type: NotificationRouteScope | str,
        category: NotificationRouteCategory | str,
        target_type: NotificationTargetType | str,
        server_id: int | None = None,
        target_chat_id: int | None = None,
        target_topic_id: int | None = None,
        escalate_admin_dm: bool = False,
        enabled_events_json: str | None = None,
        enabled_events: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> NotificationRoute:
        """创建或更新一条全局或单服覆盖路由规则并提交事务

        Raises:
            ValueError: 当作用域与 ``server_id`` 约束不匹配或枚举取值非法时抛出。
        """
        normalized_scope = NotificationRouteScope(scope_type)
        normalized_cat = NotificationRouteCategory(category)
        normalized_target = NotificationTargetType(target_type)
        if normalized_scope == NotificationRouteScope.GLOBAL and server_id is not None:
            raise ValueError("全局路由规则不允许绑定 server_id")
        if normalized_scope == NotificationRouteScope.SERVER and server_id is None:
            raise ValueError("单服覆盖路由规则必须指定 server_id")

        effective_now = now or datetime.now()
        serialized_events = (
            self.serialize_enabled_events(enabled_events)
            if enabled_events is not None
            else enabled_events_json
        )

        existing = await self.get_route(
            scope_type=normalized_scope,
            category=normalized_cat,
            server_id=server_id,
            fresh=True,
        )
        if existing is not None:
            existing.target_type = normalized_target
            existing.target_chat_id = target_chat_id
            existing.target_topic_id = target_topic_id
            existing.escalate_admin_dm = bool(escalate_admin_dm)
            existing.enabled_events_json = serialized_events
            existing.updated_at = effective_now
            await self.session.commit()
            await self.session.refresh(existing)
            return existing

        route = NotificationRoute(
            scope_type=normalized_scope,
            server_id=server_id,
            category=normalized_cat,
            target_type=normalized_target,
            target_chat_id=target_chat_id,
            target_topic_id=target_topic_id,
            escalate_admin_dm=bool(escalate_admin_dm),
            enabled_events_json=serialized_events,
            updated_at=effective_now,
        )
        self.session.add(route)
        try:
            await self.session.commit()
        except IntegrityError:
            # 并发写入同一路由键时回滚并改走更新分支，保证幂等
            await self.session.rollback()
            existing = await self.get_route(
                scope_type=normalized_scope,
                category=normalized_cat,
                server_id=server_id,
                fresh=True,
            )
            if existing is None:
                raise
            existing.target_type = normalized_target
            existing.target_chat_id = target_chat_id
            existing.target_topic_id = target_topic_id
            existing.escalate_admin_dm = bool(escalate_admin_dm)
            existing.enabled_events_json = serialized_events
            existing.updated_at = effective_now
            await self.session.commit()
            await self.session.refresh(existing)
            return existing

        await self.session.refresh(route)
        return route

    async def delete_server_route(
        self, server_id: int, category: NotificationRouteCategory | str
    ) -> bool:
        """删除指定服务器的覆盖路由，使其恢复继承全局默认规则"""
        normalized_cat = NotificationRouteCategory(category)
        stmt = delete(NotificationRoute).where(
            NotificationRoute.scope_type == NotificationRouteScope.SERVER,
            NotificationRoute.server_id == server_id,
            NotificationRoute.category == normalized_cat,
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return _rowcount(result) > 0

    async def ensure_default_global_routes(
        self, now: datetime | None = None
    ) -> Sequence[NotificationRoute]:
        """补全缺失的 5 类全局默认路由规则并提交"""
        existing = await self.get_global_routes()
        existing_cats = {route.category for route in existing}
        effective_now = now or datetime.now()
        added = False
        for category, default_target in DEFAULT_GLOBAL_ROUTE_TARGETS.items():
            if category.value in existing_cats:
                continue
            self.session.add(
                NotificationRoute(
                    scope_type=NotificationRouteScope.GLOBAL,
                    server_id=None,
                    category=category,
                    target_type=default_target,
                    escalate_admin_dm=False,
                    updated_at=effective_now,
                )
            )
            added = True
        if added:
            await self.session.commit()
        return await self.get_global_routes()


class NotificationInboxRepository:
    """用户私聊失败兜底站内收件箱仓储"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_item(
        self,
        *,
        user_id: int,
        event_type: str,
        category: str,
        title: str,
        body_text: str,
        severity: str = "info",
        server_id: int | None = None,
        action_url: str | None = None,
        is_mandatory: bool = False,
        delivery_error: str | None = None,
        now: datetime | None = None,
    ) -> NotificationInboxItem:
        """写入一条私聊兜底站内通知并提交事务"""
        item = NotificationInboxItem(
            user_id=user_id,
            server_id=server_id,
            event_type=str(event_type),
            category=str(category),
            severity=str(severity),
            title=title,
            body_text=body_text,
            action_url=action_url,
            is_mandatory=bool(is_mandatory),
            is_read=False,
            delivery_error=delivery_error,
            created_at=now or datetime.now(),
        )
        self.session.add(item)
        await self.session.commit()
        await self.session.refresh(item)
        return item

    async def get_by_id(self, item_id: int) -> NotificationInboxItem | None:
        """根据 ID 获取单条收件箱通知（强制绕过身份映射读取最新行）"""
        stmt = (
            select(NotificationInboxItem)
            .where(NotificationInboxItem.id == item_id)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_user_items(
        self,
        user_id: int,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[NotificationInboxItem]:
        """分页查询用户的站内收件箱通知，按创建时间倒序排列"""
        conditions = [NotificationInboxItem.user_id == user_id]
        if unread_only:
            conditions.append(NotificationInboxItem.is_read.is_(False))
        stmt = (
            select(NotificationInboxItem)
            .where(*conditions)
            .order_by(
                NotificationInboxItem.created_at.desc(),
                NotificationInboxItem.id.desc(),
            )
            .limit(max(1, limit))
            .offset(max(0, offset))
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_unread_summary(self, user_id: int) -> tuple[int, bool]:
        """单次查询返回用户的未读通知总数及是否存在未读必达通知"""
        stmt = select(
            func.count(NotificationInboxItem.id),
            func.coalesce(
                func.max(
                    case((NotificationInboxItem.is_mandatory.is_(True), 1), else_=0)
                ),
                0,
            ),
        ).where(
            NotificationInboxItem.user_id == user_id,
            NotificationInboxItem.is_read.is_(False),
        )
        row = (await self.session.execute(stmt)).one()
        return int(row[0] or 0), bool(row[1])

    async def count_unread(self, user_id: int) -> int:
        """统计用户当前未读收件箱通知数量"""
        unread_count, _ = await self.get_unread_summary(user_id)
        return unread_count

    async def has_unread_mandatory(self, user_id: int) -> bool:
        """检查用户当前是否存在未读的必达通知"""
        _, has_mandatory = await self.get_unread_summary(user_id)
        return has_mandatory

    async def mark_read(
        self,
        user_id: int,
        item_id: int | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        """标记单条或全部未读收件箱通知为已读，返回本次更新条数"""
        conditions = [
            NotificationInboxItem.user_id == user_id,
            NotificationInboxItem.is_read.is_(False),
        ]
        if item_id is not None:
            conditions.append(NotificationInboxItem.id == item_id)
        stmt = (
            update(NotificationInboxItem)
            .where(*conditions)
            .values(is_read=True, read_at=now or datetime.now())
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return _rowcount(result)

    async def delete_expired_records(self, cutoff: datetime) -> int:
        """清理早于指定截止时间的过期站内收件箱记录（保留 30 天内记录）"""
        stmt = delete(NotificationInboxItem).where(
            NotificationInboxItem.created_at < cutoff
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return _rowcount(result)


class OpsAlertRepository:
    """系统运维告警日志与冷却判定仓储"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_log(
        self,
        *,
        source_key: str,
        event_type: str,
        title: str,
        body_text: str,
        severity: str = "warning",
        server_id: int | None = None,
        delivered_targets_json: str = "[]",
        delivery_status: str = "sent",
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> OpsAlertLog:
        """持久化一条运维告警投递日志并提交事务"""
        entry = OpsAlertLog(
            source_key=source_key,
            server_id=server_id,
            event_type=str(event_type),
            severity=str(severity),
            title=title,
            body_text=body_text,
            delivered_targets_json=delivered_targets_json,
            delivery_status=delivery_status,
            error_message=error_message,
            created_at=now or datetime.now(),
        )
        self.session.add(entry)
        await self.session.commit()
        await self.session.refresh(entry)
        return entry

    async def list_logs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        event_type: str | None = None,
        severity: str | None = None,
        server_id: int | None = None,
    ) -> Sequence[OpsAlertLog]:
        """按可选维度筛选并分页查询近期运维告警记录"""
        conditions = []
        if event_type is not None:
            conditions.append(OpsAlertLog.event_type == str(event_type))
        if severity is not None:
            conditions.append(OpsAlertLog.severity == str(severity))
        if server_id is not None:
            conditions.append(OpsAlertLog.server_id == server_id)

        stmt = (
            select(OpsAlertLog)
            .where(*conditions)
            .order_by(OpsAlertLog.created_at.desc(), OpsAlertLog.id.desc())
            .limit(max(1, limit))
            .offset(max(0, offset))
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_latest_by_source(
        self, event_type: str, source_key: str
    ) -> OpsAlertLog | None:
        """获取指定事件类型与告警源的最新一条记录"""
        stmt = (
            select(OpsAlertLog)
            .where(
                OpsAlertLog.event_type == str(event_type),
                OpsAlertLog.source_key == source_key,
            )
            .order_by(OpsAlertLog.created_at.desc(), OpsAlertLog.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def is_in_cooldown(
        self, event_type: str, source_key: str, cooldown_since: datetime
    ) -> bool:
        """判断指定事件源在给定冷却起始时间之后是否已发生过告警"""
        stmt = (
            select(OpsAlertLog.id)
            .where(
                OpsAlertLog.event_type == str(event_type),
                OpsAlertLog.source_key == source_key,
                OpsAlertLog.created_at >= cooldown_since,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def delete_expired_records(self, cutoff: datetime) -> int:
        """清理早于指定截止时间的历史运维告警日志（保留 30 天内记录）"""
        stmt = delete(OpsAlertLog).where(OpsAlertLog.created_at < cutoff)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return _rowcount(result)


_DEFAULT_USER_PREFERENCE_FLAGS: dict[str, bool] = {
    "allow_watch_time_milestone": True,
    "allow_pre_expiry_reminder": True,
    "allow_traffic_80_alert": True,
    "allow_cycle_reset": False,
    "allow_self_action_receipt": True,
}


class UserNotificationPreferenceRepository:
    """用户可选通知偏好读写仓储"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_user_id(self, user_id: int) -> UserNotificationPreference | None:
        """获取指定用户的通知偏好持久化记录（不存在时返回 None，不触发写库）"""
        stmt = (
            select(UserNotificationPreference)
            .where(UserNotificationPreference.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create(
        self, user_id: int, *, now: datetime | None = None
    ) -> UserNotificationPreference:
        """获取或按默认值初始化用户通知偏好记录并提交"""
        pref = await self.get_by_user_id(user_id)
        if pref is not None:
            return pref
        pref = UserNotificationPreference(
            user_id=user_id,
            **_DEFAULT_USER_PREFERENCE_FLAGS,
            updated_at=now or datetime.now(),
        )
        self.session.add(pref)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self.get_by_user_id(user_id)
            if existing is None:
                raise
            return existing
        await self.session.refresh(pref)
        return pref

    async def get_effective_flags(self, user_id: int) -> dict[str, bool]:
        """返回用户全部 5 项可选提醒开关的生效状态（未建行时直接返回默认值）"""
        pref = await self.get_by_user_id(user_id)
        if pref is None:
            return dict(_DEFAULT_USER_PREFERENCE_FLAGS)
        return {key: bool(getattr(pref, key)) for key in USER_PREFERENCE_KEYS}

    async def is_event_enabled(
        self,
        user_id: int,
        preference_key: str | None,
        *,
        default_enabled: bool = True,
    ) -> bool:
        """校验给定偏好键在用户侧是否处于开启状态

        Args:
            user_id: Telegram 用户 ID。
            preference_key: 事件元数据声明的偏好键名（为 ``None`` 表示无偏好约束）。
            default_enabled: 当未传入偏好键或按默认策略判断时的回退值。

        Returns:
            允许投递返回 ``True``，被用户关闭返回 ``False``。

        Raises:
            ValueError: 当传入未定义的偏好键名时抛出。
        """
        if preference_key is None:
            return default_enabled
        if preference_key not in _DEFAULT_USER_PREFERENCE_FLAGS:
            raise ValueError(f"未知的用户通知偏好键: {preference_key}")
        pref = await self.get_by_user_id(user_id)
        if pref is None:
            return _DEFAULT_USER_PREFERENCE_FLAGS[preference_key]
        return bool(getattr(pref, preference_key))

    async def update_preferences(
        self,
        user_id: int,
        *,
        allow_watch_time_milestone: bool | None = None,
        allow_pre_expiry_reminder: bool | None = None,
        allow_traffic_80_alert: bool | None = None,
        allow_cycle_reset: bool | None = None,
        allow_self_action_receipt: bool | None = None,
        now: datetime | None = None,
    ) -> UserNotificationPreference:
        """部分或全量更新用户的可选通知开关并提交事务"""
        pref = await self.get_or_create(user_id, now=now)
        updates: dict[str, bool | None] = {
            "allow_watch_time_milestone": allow_watch_time_milestone,
            "allow_pre_expiry_reminder": allow_pre_expiry_reminder,
            "allow_traffic_80_alert": allow_traffic_80_alert,
            "allow_cycle_reset": allow_cycle_reset,
            "allow_self_action_receipt": allow_self_action_receipt,
        }
        for field_name, field_value in updates.items():
            if field_value is not None:
                setattr(pref, field_name, bool(field_value))
        pref.updated_at = now or datetime.now()
        await self.session.commit()
        await self.session.refresh(pref)
        return pref
