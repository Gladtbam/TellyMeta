import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import ActiveCode


class CodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_code(self, code: str) -> ActiveCode | None:
        """通过注册码获取激活码
        Args:
            code (str): 注册码
        Returns:
            ActiveCode | None: 如果找到激活码则返回激活码对象，否则返回None
        """
        stmt = select(ActiveCode).where(ActiveCode.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, code_type: str, expires: int | None) -> ActiveCode:
        """创建激活码
        Args:
            code_type (str): 激活码类型，'signup' 或 'renew'
            expires (int | None): 激活码过期时间，单位为天。如果为None，则表示永不过期
        Returns:
            ActiveCode: 返回创建的激活码对象
        """
        raw = secrets.token_urlsafe(12)
        code = '-'.join([raw[i:i+4] for i in range(0, len(raw), 4)])
        if expires is None:
            expires_at = datetime(2099, 12, 31)
        else:
            expires_at = datetime.now() + timedelta(days=expires)

        new_code = ActiveCode(code=code, type=code_type, expires_at=expires_at)
        self.session.add(new_code)
        await self.session.commit()
        await self.session.refresh(new_code)
        return new_code

    async def mark_used(self, code: ActiveCode) -> None:
        """标记激活码为已使用
        Args:
            code (ActiveCode): 需要标记为已使用的激活码对象
        """
        stmt = (
            update(ActiveCode)
            .where(ActiveCode.code == code)
            .values(used_at=datetime.now())
        )
        await self.session.execute(stmt)
        await self.session.commit()
