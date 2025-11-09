from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import TelegramUser


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

    async def update_checkin(self, user_id: int, score_reward: int) -> TelegramUser:
        """处理用户签到逻辑"""
        user = await self.get_or_create(user_id)

        user.checkin_count += 1
        user.score += score_reward
        user.last_checkin = datetime.now()

        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update_warn_and_score(self, user_id: int, increment: int = 1) -> TelegramUser:
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
                users_to_update.append({'id': user_id, 'score': score + TelegramUser.score})
            else:
                users_to_insert.append({'id': user_id, 'score': score})

        if users_to_update:
            await self.session.execute(update(TelegramUser), users_to_update)
        if users_to_insert:
            self.session.add_all([TelegramUser(**data) for data in users_to_insert])

        await self.session.commit()

    async def get_renew_score(self) -> int:
        """获取续费所需积分
        Returns:
            int: 续费所需积分，如果未设置则返回None
         """
        stmt = select(func.avg(TelegramUser.score)).where(TelegramUser.score > 10)
        result = await self.session.execute(stmt)
        score = result.scalar()
        if score is None or score < 100:
            score = 100
        return score

    # async def update_user_score(self, user_id: int, score: int):
    #     await self.session.execute(
    #         update(TelegramUser).where(TelegramUser.id == user_id).values(score=score)
    #     )
    #     await self.session.commit()

    # async def delete_user(self, user_id: int):
    #     await self.session.execute(delete(TelegramUser).where(TelegramUser.id == user_id))
    #     await self.session.commit()
