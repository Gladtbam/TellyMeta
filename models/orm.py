from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base


class TelegramUser(Base):
    """Telegram用户模型"""
    __tablename__ = 'telegram_users'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_admin: Mapped[bool] = mapped_column(server_default=text('false'), nullable=False)
    score: Mapped[int] = mapped_column(server_default=text('0'), nullable=False)
    checkin_count: Mapped[int] = mapped_column(server_default=text('0'), nullable=False)
    warning_count: Mapped[int] = mapped_column(server_default=text('0'), nullable=False)
    last_checkin: Mapped[datetime] = mapped_column(default=datetime(1970, 1, 1), nullable=False)

    emby: Mapped[Emby | None] = relationship(
        back_populates='telegram_user',
        uselist=False,
        cascade='all, delete-orphan',
        lazy='selectin'
    )

class Emby(Base):
    """Emby用户模型"""
    __tablename__ = 'emby'

    id: Mapped[int] = mapped_column(BigInteger, ForeignKey('telegram_users.id'), primary_key=True)
    emby_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    emby_name: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    is_banned: Mapped[bool] = mapped_column(server_default=text('false'), nullable=False)
    delete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    telegram_user: Mapped[TelegramUser] = relationship(back_populates='emby')

class ActiveCode(Base):
    """激活码模型"""
    __tablename__ = 'active_codes'

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)

class PendingVerification(Base):
    """待验证用户模型"""
    __tablename__ = 'pending_verifications'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    captcha_answer: Mapped[str] = mapped_column(String(16), nullable=True)
    scheduler_job_id: Mapped[str] = mapped_column(String(255), nullable=False)

class BotConfiguration(Base):
    """Bot 配置模型"""
    __tablename__ = 'bot_configurations'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
