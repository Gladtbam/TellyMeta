import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import PendingVerification

logger = logging.getLogger(__name__)

class VerificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self, user_id: int, job_id: str
    ) -> PendingVerification:
        """创建一个新的待验证用户记录"""
        challenge = PendingVerification(
            id=user_id,
            scheduler_job_id=job_id,
        )
        self.session.add(challenge)
        await self.session.commit()
        await self.session.refresh(challenge)
        return challenge

    async def get(self, user_id: int) -> PendingVerification | None:
        """获取待验证用户记录"""
        return await self.session.get(PendingVerification, user_id)

    async def delete(self, user_id: int) -> None:
        stmt = delete(PendingVerification).where(PendingVerification.id == user_id)
        await self.session.execute(stmt)
        await self.session.commit()

    async def update_answer(self, user_id: int, answer: str) -> bool:
        """更新用户的验证码答案"""
        verification = await self.get(user_id)
        if verification:
            verification.captcha_answer = answer
            await self.session.commit()
            await self.session.refresh(verification)
            return True
        return False
