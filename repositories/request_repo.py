from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import MediaRequest
from repositories.config_repo import ConfigRepository


class RequestRepository:
    """求片请求数据访问层"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, request_id: int) -> MediaRequest | None:
        """根据请求 ID 获取求片记录"""
        stmt = (
            select(MediaRequest)
            .where(MediaRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_user(
        self, user_id: int, limit: int = 50
    ) -> Sequence[MediaRequest]:
        """获取指定用户的求片记录，按创建时间倒序排列"""
        stmt = (
            select(MediaRequest)
            .where(MediaRequest.user_id == user_id)
            .order_by(MediaRequest.created_at.desc(), MediaRequest.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def has_pending_for_arr(
        self, arr_id: int, media_type: str, media_id: int
    ) -> bool:
        """检查目标 Arr 实例是否已存在同一媒体的未超时待审核求片

        说明：已超时的 pending 记录应由定时任务释放，此处仅把未超时的 pending
        视作有效去重冲突；若存在尚未被定时任务清理的过期记录，仍视为 pending
        直到定时任务或原子状态流转将其关闭，以保持与数据库唯一索引一致。
        """
        stmt = select(MediaRequest.id).where(
            MediaRequest.arr_id == arr_id,
            MediaRequest.media_type == media_type,
            MediaRequest.media_id == media_id,
            MediaRequest.status == "pending",
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def create_request(
        self,
        *,
        user_id: int,
        media_server_id: int,
        arr_id: int,
        library_name: str,
        media_type: str,
        media_id: int,
        title: str,
        reserved_cost: int,
        now: datetime | None = None,
    ) -> MediaRequest:
        """创建一条待审核求片记录并提交事务"""
        created_at = now or datetime.now()
        expire_days = (await ConfigRepository(self.session).get_policy_config())[
            "request_expire_days"
        ]
        req = MediaRequest(
            user_id=user_id,
            media_server_id=media_server_id,
            arr_id=arr_id,
            library_name=library_name,
            media_type=media_type,
            media_id=media_id,
            title=title,
            reserved_cost=reserved_cost,
            status="pending",
            cost_status="reserved",
            created_at=created_at,
            expires_at=created_at + timedelta(days=expire_days),
        )
        self.session.add(req)
        await self.session.commit()
        await self.session.refresh(req)
        return req

    async def update_topic_message_id(self, request_id: int, message_id: int) -> None:
        """记录管理员话题通知消息 ID，便于追踪与更新状态"""
        stmt = (
            update(MediaRequest)
            .where(MediaRequest.id == request_id)
            .values(topic_message_id=message_id)
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def atomic_transition(
        self,
        request_id: int,
        *,
        from_status: str,
        to_status: str,
        cost_status: str,
        approver_name: str | None = None,
        fail_reason: str | None = None,
        user_id: int | None = None,
        require_unexpired: bool = False,
        now: datetime | None = None,
    ) -> MediaRequest | None:
        """原子流转求片状态，防止重复审批、撤销或超时导致的并发双扣/双退

        Args:
            request_id (int): 求片记录 ID
            from_status (str): 期望的当前状态（通常为 "pending"）
            to_status (str): 目标状态
            cost_status (str): 目标积分状态（"reserved" / "settled" / "released"）
            approver_name (str | None): 处理人名称
            fail_reason (str | None): 失败或关闭原因
            user_id (int | None): 若提供，则额外校验请求归属该用户
            require_unexpired (bool): 若为 True，则仅在未超过 expires_at 时允许流转
            now (datetime | None): 当前时间，便于测试注入

        Returns:
            MediaRequest | None: 状态流转成功返回更新后的记录，条件不满足返回 None
        """
        current_time = now or datetime.now()
        conditions = [
            MediaRequest.id == request_id,
            MediaRequest.status == from_status,
        ]
        if user_id is not None:
            conditions.append(MediaRequest.user_id == user_id)
        if require_unexpired:
            conditions.append(MediaRequest.expires_at > current_time)

        values: dict[str, object] = {
            "status": to_status,
            "cost_status": cost_status,
        }
        if to_status in ("approved", "rejected", "canceled", "expired", "failed"):
            values["resolved_at"] = current_time
        if approver_name is not None:
            values["approver_name"] = approver_name
        if fail_reason is not None:
            values["fail_reason"] = fail_reason

        stmt = (
            update(MediaRequest)
            .where(*conditions)
            .values(**values)
            .returning(MediaRequest.id)
            .execution_options(synchronize_session="fetch")
        )
        result = await self.session.execute(stmt)
        if result.scalar_one_or_none() is None:
            await self.session.rollback()
            return None
        await self.session.commit()
        return await self.get_by_id(request_id)

    async def find_expired_pending(
        self, now: datetime | None = None
    ) -> Sequence[MediaRequest]:
        """查询所有已超过 7 天有效期但仍处于 pending 状态的求片记录"""
        current_time = now or datetime.now()
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.status == "pending",
                MediaRequest.expires_at <= current_time,
            )
            .order_by(MediaRequest.id.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
