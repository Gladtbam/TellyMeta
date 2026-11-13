from collections.abc import Sequence
from datetime import datetime
from statistics import median

from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import TelegramUser
from repositories.config_repo import ConfigRepository


class TelegramRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: int) -> TelegramUser | None:
        """通过ID获取Telegram用户
        Args:
            user_id (int): Telegram用户ID
        Returns:
            TelegramUser | None: 如果找到用户则返回用户对象，否则返回None
        """
        return await self.session.get(TelegramUser, user_id)

    async def __create_by_id(self, user_id: int) -> TelegramUser:
        """创建Telegram用户
        Args:
            user_id (int): Telegram用户ID
        Returns:
            tuple[TelegramUser, bool]: 返回用户对象和一个布尔值，表示是否创建了新用户
        """
        new_user = TelegramUser(id=user_id)
        self.session.add(new_user)
        return new_user

    async def get_or_create(self, user_id: int) -> TelegramUser:
        """获取或创建Telegram用户"""
        user = None

        user = await self.get_by_id(user_id)
        if user is None:
            user = await self.__create_by_id(user_id)
            await self.session.commit()
            await self.session.refresh(user)
        return user

    async def delete_by_id(self, user_id: int) -> TelegramUser | None:
        """删除Telegram用户
        Args:
            user (TelegramUser): 需要删除的用户对象
        """
        user = await self.get_by_id(user_id)
        if user is None:
            return
        await self.session.delete(user)
        await self.session.commit()
        return user

    async def get_admins(self) -> Sequence[int]:
        """查找所有管理员用户
        Returns:
            Sequence[int]: 管理员用户列表
        """
        stmt = select(TelegramUser.id).where(TelegramUser.is_admin.is_(True))
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def toggle_admin(self, user_id: int, is_admin: bool) -> TelegramUser:
        """切换管理员用户状态
        Args:
            user_id (int): Telegram用户ID
            is_admin (bool): 是否设置为管理员
        Returns:
            TelegramUser: 更新后的用户对象
        Raises:
            ValueError: 如果用户的管理员状态无需更改则抛出异常
        """
        user = await self.get_or_create(user_id)
        if user.is_admin == is_admin:
            raise ValueError("用户的管理员状态无需更改")

        user.is_admin = is_admin
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update_checkin(
        self, user_id: int, score_reward: int, is_consecutive: bool = False
    ) -> TelegramUser:
        """处理用户签到逻辑"""
        user = await self.get_or_create(user_id)

        user.checkin_count += 1
        if is_consecutive:
            user.consecutive_checkin_count = (user.consecutive_checkin_count or 0) + 1
        else:
            user.consecutive_checkin_count = 1
        user.score += score_reward
        user.last_checkin = datetime.now()

        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update_warn_and_score(
        self, user_id: int, increment: int = 1
    ) -> TelegramUser:
        """更新用户警告次数
        Args:
            user_id (int): Telegram用户ID
            increment (int, optional): 增加的警告次数，默认为1。可以为负数以减少警告次数。
        Returns:
            TelegramUser: 更新后的用户对象
        """
        user = await self.get_or_create(user_id)

        user.warning_count += increment
        user.score -= user.warning_count
        if user.score < 0:
            user.score = 0

        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update_score(self, user_id: int, score: int) -> TelegramUser:
        """更新用户积分
        Args:
            user_id (int): Telegram用户ID
            score (int): 需要增加的积分，可以为负数以减少积分
        Returns:
            TelegramUser: 更新后的用户对象
        """
        user = await self.get_or_create(user_id)
        user.score += score
        if user.score < 0:
            user.score = 0
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def deduct_score_if_sufficient(self, user_id: int, cost: int) -> bool:
        """原子校验并预留扣除用户积分，防止并发超扣

        Args:
            user_id (int): Telegram 用户 ID
            cost (int): 需要扣除的积分数（必须 >= 0）

        Returns:
            bool: 积分充足并扣除成功返回 True，否则返回 False
        """
        if cost < 0:
            raise ValueError("扣除积分不能为负数")
        await self.get_or_create(user_id)
        if cost == 0:
            return True

        stmt = (
            update(TelegramUser)
            .where(TelegramUser.id == user_id, TelegramUser.score >= cost)
            .values(score=TelegramUser.score - cost)
            .returning(TelegramUser.id)
            .execution_options(synchronize_session="fetch")
        )
        result = await self.session.execute(stmt)
        if result.scalar_one_or_none() is None:
            await self.session.rollback()
            return False
        await self.session.commit()
        return True

    async def get_top_users(self, limit: int = 10) -> Sequence[TelegramUser]:
        """获取积分排行榜"""
        stmt = select(TelegramUser).order_by(TelegramUser.score.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_all_users(self) -> Sequence[TelegramUser]:
        """获取所有用户"""
        stmt = select(TelegramUser)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def batch_update_scores(self, score_deltas: dict[int, int]):
        """批量更新用户积分
        Args:
            score_updates (dict[int, int]): 包含用户ID和对应积分更新值的字典
        """
        if not score_deltas:
            return

        user_ids = list(score_deltas.keys())

        stmt = select(TelegramUser.id).where(TelegramUser.id.in_(user_ids))
        result = await self.session.execute(stmt)
        existing_user_ids = {row[0] for row in result}

        users_to_update = []
        users_to_insert = []
        for user_id, score in score_deltas.items():
            if user_id in existing_user_ids:
                users_to_update.append({"id": user_id, "score_delta": score})
            else:
                users_to_insert.append({"id": user_id, "score": score})

        if users_to_update:
            update_stmt = (
                update(TelegramUser)
                .values(score=TelegramUser.score + bindparam("score_delta"))
                .execution_options(synchronize_session=False)
            )
            await self.session.execute(update_stmt, users_to_update)
        if users_to_insert:
            self.session.add_all([TelegramUser(**data) for data in users_to_insert])

        await self.session.commit()

    async def get_renew_score(self) -> int:
        """获取续费/注册所需积分（支持固定积分或动态中位数模式）。

        Returns:
            int: 计算后的积分门槛。
        """
        points_cfg = await ConfigRepository(self.session).get_points_config()
        if points_cfg["points_renew_mode"] == "fixed":
            return int(points_cfg["points_renew_fixed"])

        min_score = int(points_cfg["points_renew_min"])
        max_score = int(points_cfg["points_renew_max"])

        stmt = select(TelegramUser.score).where(TelegramUser.score > 10)
        result = await self.session.execute(stmt)
        scores = result.scalars().all()

        if not scores:
            return min_score

        final_score = int(median(scores))

        return max(min_score, min(final_score, max_score))

    # async def update_user_score(self, user_id: int, score: int):
    #     await self.session.execute(
    #         update(TelegramUser).where(TelegramUser.id == user_id).values(score=score)
    #     )
    #     await self.session.commit()

    # async def delete_user(self, user_id: int):
    #     await self.session.execute(delete(TelegramUser).where(TelegramUser.id == user_id))
    #     await self.session.commit()
