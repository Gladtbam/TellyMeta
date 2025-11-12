from __future__ import annotations

import json
from datetime import datetime

from loguru import logger
from pydantic import BaseModel, ValidationError, field_validator
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
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

class BotConfiguration(Base):
    """Bot 配置模型"""
    __tablename__ = 'bot_configurations'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

class LibraryBindingModel(BaseModel):
    """媒体库绑定模型"""
    library_name: str
    arr_type: str | None = None
    quality_profile_id: int | None = None
    root_folder: str | None = None

    @field_validator('library_name', mode='before')
    @classmethod
    def remove_binding_prefix(cls, value: str) -> str:
        """移除键中的 'binding:' 前缀"""
        if value.startswith('binding:'):
            return value[len('binding:'):]
        return value

    def to_config_key(self) -> str:
        """生成用于存储在 BotConfiguration 表中的键"""
        return f'binding:{self.library_name}'

    def to_config_value(self) -> str:
        """生成用于存储在 BotConfiguration 表中的值，排除 library_name 字段
        """
        data_to_store = self.model_dump(exclude={'library_name'})
        return json.dumps(data_to_store)

    @classmethod
    def from_db_config(cls, config_row: BotConfiguration) -> 'LibraryBindingModel':
        """
        (推荐的辅助方法)
        从一个 BotConfiguration 数据库行对象创建模型实例。
        """
        try:
            data = json.loads(config_row.value)
            # 关键：我们将 'key' 传给 'library_name' 字段进行验证和解析
            data['library_name'] = config_row.key
            return cls.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.error("解析绑定配置 {} 失败: {}。数据: {}", config_row.key, e, config_row.value)
            # 返回一个至少包含名称的“空”模型
            return cls(library_name=config_row.key)
