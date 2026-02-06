from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum

from loguru import logger
from pydantic import BaseModel, ValidationError, field_validator
from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer,
                        String, UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base


class ServerType(StrEnum):
    """服务类型枚举"""
    EMBY = "emby"
    JELLYFIN = "jellyfin"
    SONARR = "sonarr"
    RADARR = "radarr"

class RegistrationMode(StrEnum):
    """注册模式枚举"""
    DEFAULT = "default"  # 仅邀请/积分
    OPEN = "open"        # 开放注册
    COUNT = "count"      # 限制名额
    TIME = "time"        # 限时开放
    CLOSE = "close"      # 完全关闭 (仅管理员添加)
    EXTERNAL = "external" # 外部验证

class TelegramUser(Base):
    """Telegram用户模型"""
    __tablename__ = 'telegram_users'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_admin: Mapped[bool] = mapped_column(server_default=text('false'), nullable=False)
    score: Mapped[int] = mapped_column(server_default=text('0'), nullable=False)
    checkin_count: Mapped[int] = mapped_column(server_default=text('0'), nullable=False)
    warning_count: Mapped[int] = mapped_column(server_default=text('0'), nullable=False)
    last_checkin: Mapped[datetime] = mapped_column(default=datetime(1970, 1, 1), nullable=False)

    media_users: Mapped[list[MediaUser]] = relationship(
        back_populates='telegram_user',
        cascade='all, delete-orphan',
        lazy='selectin'
    )

class ServerInstance(Base):
    """服务器实例配置模型"""
    __tablename__ = 'server_instances'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    server_type: Mapped[str] = mapped_column(String(32), nullable=True)
    url: Mapped[str] = mapped_column(String(255), nullable=True)
    api_key: Mapped[str] = mapped_column(String(255), nullable=True)
    webhook_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text('true'), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, server_default=text('0'), nullable=False) # 优先级
    tos: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    notify_topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Jellyfin/Emby 相关配置
    registration_mode: Mapped[str] = mapped_column(String(32), default=RegistrationMode.DEFAULT, server_default=text("'default'"))
    registration_count_limit: Mapped[int] = mapped_column(Integer, default=0, server_default=text('0'))
    registration_time_limit: Mapped[str] = mapped_column(String(32), default="0", server_default=text("'0'"))
    registration_expiry_days: Mapped[int] = mapped_column(Integer, default=30, server_default=text('30'))
    registration_external_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    registration_external_parser: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    code_expiry_days: Mapped[int] = mapped_column(Integer, default=30, server_default=text('30'))
    nsfw_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('false'))
    nsfw_library_ids: Mapped[str] = mapped_column(String(1024), default="", server_default=text("''"))
    nsfw_sub_library_ids: Mapped[str] = mapped_column(String(2048), default="", server_default=text("''"))
    allow_subtitle_upload: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'))

    # Radarr/Sonarr 路径映射: JSON 字符串: {"/remote/path": "/local/path"}
    path_mappings: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    request_notify_topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

class MediaUser(Base):
    """Media 用户模型"""
    __tablename__ = 'media_user'

    id: Mapped[int] = mapped_column(BigInteger, ForeignKey('telegram_users.id'), primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer, ForeignKey('server_instances.id'), primary_key=True)
    media_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    media_name: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, server_default=text('false'), nullable=False)
    delete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    telegram_user: Mapped[TelegramUser] = relationship(back_populates='media_users')
    __table_args__ = (
        UniqueConstraint('server_id', 'media_id', name='uq_server_media_id'),
    )

class ActiveCode(Base):
    """激活码模型"""
    __tablename__ = 'active_codes'

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('server_instances.id'), nullable=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)

    server: Mapped[ServerInstance | None] = relationship()
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
    server_id: int | None = None #server_instance id
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

class ApiCache(Base):
    """API 响应缓存模型"""
    __tablename__ = 'api_cache'

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False) # JSON data
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
