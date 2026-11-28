"""全局时区管理、UTC 时钟与跨数据库（PostgreSQL TIMESTAMPTZ / SQLite UTC）时间列类型。"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from core.config import get_settings

# 统一 UTC 纪元哨兵时间（用于未签到等历史缺省值）
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


@lru_cache(maxsize=16)
def _resolve_zoneinfo(tz_name: str) -> ZoneInfo:
    """缓存 IANA 时区对象，避免高频转换重复解析时区数据库。"""
    return ZoneInfo(tz_name)


def get_configured_timezone() -> ZoneInfo:
    """获取当前配置项 ``settings.timezone`` 对应的 ``ZoneInfo`` 对象。"""
    tz_name = (get_settings().timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
    return _resolve_zoneinfo(tz_name)


def apply_process_timezone() -> str:
    """将进程级 ``TZ`` 环境变量同步为配置时区并刷新 C 时区缓存（兼容无 ``tzset`` 平台）。

    Returns:
        最终生效的 IANA 时区名称。
    """
    tz_name = (get_settings().timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
    # 先校验合法性，避免把无效时区名写入进程环境变量
    _resolve_zoneinfo(tz_name)
    os.environ["TZ"] = tz_name
    tzset = getattr(time, "tzset", None)
    if callable(tzset):
        tzset()
    return tz_name


def utc_now() -> datetime:
    """返回带 ``UTC`` 时区信息的当前绝对时间点。"""
    return datetime.now(UTC)


def local_now() -> datetime:
    """返回带配置业务时区（如 ``Asia/Shanghai``）信息的当前本地时间。"""
    return datetime.now(get_configured_timezone())


def to_utc(dt: datetime) -> datetime:
    """将任意 ``datetime`` 规范化为带 ``UTC`` 时区信息的绝对时间点。

    - 若输入已携带 ``tzinfo``，按原时区精确换算到 UTC；
    - 若输入为朴素时间（例如历史兼容调用或测试构造的本地时间），按配置业务时区解释后换算到 UTC。

    Args:
        dt: 待转换的 ``datetime`` 实例。

    Returns:
        ``tzinfo=UTC`` 的时间对象。
    """
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=get_configured_timezone()).astimezone(UTC)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """可空版 ``to_utc``：``None`` 原样返回，否则规范化为 UTC aware 时间。"""
    if dt is None:
        return None
    return to_utc(dt)


def to_local(dt: datetime) -> datetime:
    """将任意 ``datetime`` 转换为配置业务时区的 aware 时间（用于自然日/自然月与文本展示）。"""
    return to_utc(dt).astimezone(get_configured_timezone())


def local_today(now: datetime | None = None) -> date:
    """返回指定时刻（默认当前 UTC 时刻）在配置业务时区下的自然日 ``date``。"""
    return to_local(now if now is not None else utc_now()).date()


def format_local(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """将任意 ``datetime`` 按配置业务时区格式化为人类可读字符串（用于 Telegram 消息等纯文本输出）。"""
    return to_local(dt).strftime(fmt)


class UTCDateTime(TypeDecorator[datetime]):
    """统一 UTC 存储时间列类型（PostgreSQL 原生 ``TIMESTAMPTZ``，SQLite 规范化 UTC 存储）。

    - 写入时：所有输入经 ``to_utc`` 归一为 UTC；PostgreSQL 直接绑定 UTC aware 时间，
      SQLite 剥离 ``tzinfo`` 存为规范 UTC 文本以保持 SQL 字典序比较语义一致；
    - 读取时：统一还原为 ``tzinfo=UTC`` 的 aware ``datetime``，消除双库与会话时区差异。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        """PostgreSQL 使用 ``TIMESTAMP WITH TIME ZONE``，SQLite 使用 ``DATETIME``。"""
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """写入数据库前统一规范化为 UTC；SQLite 存为朴素 UTC 保持字典序比较。"""
        if value is None:
            return None
        utc_val = to_utc(value)
        if dialect.name == "sqlite":
            return utc_val.replace(tzinfo=None)
        return utc_val

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """从数据库读取后统一返回 ``tzinfo=UTC`` 的 aware ``datetime``。"""
        _ = dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
