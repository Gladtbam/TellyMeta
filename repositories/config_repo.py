from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import BotConfiguration


class ConfigRepository:
    cache: dict[str, str] = {}

    # System Feature Keys
    KEY_ENABLE_POINTS = "system:enable_points"
    KEY_ENABLE_VERIFICATION = "system:enable_verification"
    KEY_ENABLE_CLEANUP_INACTIVE_USERS = "system:enable_cleanup_inactive_users"
    KEY_ENABLE_MKV_PROCESS = "system:enable_mkv_process"
    KEY_MKV_EXCLUDE_KEYWORDS = "system:mkv_exclude_keywords"
    KEY_ENABLE_LLM_TRANSLATE = "system:enable_llm_translate"
    KEY_AI_API_KEY = "system:ai_api_key"
    KEY_AI_BASE_URL = "system:ai_base_url"
    KEY_AI_MODEL = "system:ai_model"
    KEY_AI_TEMPERATURE = "system:ai_temperature"
    KEY_AI_RPM = "system:ai_rpm"
    KEY_AI_RPD = "system:ai_rpd"
    KEY_AI_TPM = "system:ai_tpm"
    KEY_AI_CONCURRENCY = "system:ai_concurrency"
    KEY_TMDB_API_KEY = "system:tmdb_api_key"
    KEY_TVDB_API_KEY = "system:tvdb_api_key"

    # Points / Check-in / Pricing Keys
    KEY_POINTS_RENEW_MODE = "points:renew_mode"
    KEY_POINTS_RENEW_FIXED = "points:renew_fixed"
    KEY_POINTS_RENEW_MIN = "points:renew_min"
    KEY_POINTS_RENEW_MAX = "points:renew_max"
    KEY_POINTS_REQUEST_MODE = "points:request_mode"
    KEY_POINTS_REQUEST_RATIO = "points:request_ratio"
    KEY_POINTS_REQUEST_FIXED = "points:request_fixed"
    KEY_POINTS_CHECKIN_MIN = "points:checkin_min"
    KEY_POINTS_CHECKIN_MAX = "points:checkin_max"
    KEY_POINTS_CHECKIN_CONSECUTIVE_BONUS = "points:checkin_consecutive_bonus"
    KEY_POINTS_CHECKIN_LUCKY_INTERVAL = "points:checkin_lucky_interval"
    KEY_POINTS_MESSAGE_DAILY_CAP = "points:message_daily_cap"
    KEY_POINTS_SPAM_THRESHOLD = "points:spam_threshold"

    # Policy / Grace Period Keys
    KEY_VERIFICATION_TIMEOUT_MINUTES = "policy:verification_timeout_minutes"
    KEY_ACCOUNT_DELETE_GRACE_DAYS = "policy:account_delete_grace_days"
    KEY_ACCOUNT_RENEW_ADVANCE_DAYS = "policy:account_renew_advance_days"
    KEY_REQUEST_EXPIRE_DAYS = "policy:request_expire_days"

    # Ops / Scheduler Keys
    KEY_SCHEDULE_BAN_EXPIRED_DAY = "ops:schedule_ban_expired_day"
    KEY_SCHEDULE_BAN_EXPIRED_TIME = "ops:schedule_ban_expired_time"
    KEY_SCHEDULE_DELETE_BANNED_DAY = "ops:schedule_delete_banned_day"
    KEY_SCHEDULE_DELETE_BANNED_TIME = "ops:schedule_delete_banned_time"
    KEY_SCHEDULE_CLEANUP_INACTIVE_DAY = "ops:schedule_cleanup_inactive_day"
    KEY_SCHEDULE_CLEANUP_INACTIVE_TIME = "ops:schedule_cleanup_inactive_time"
    KEY_SCHEDULE_SETTLE_SCORES_DAY = "ops:schedule_settle_scores_day"
    KEY_SCHEDULE_SETTLE_SCORES_TIME = "ops:schedule_settle_scores_time"
    KEY_SCHEDULE_CLEANUP_CACHE_DAY = "ops:schedule_cleanup_cache_day"
    KEY_SCHEDULE_CLEANUP_CACHE_TIME = "ops:schedule_cleanup_cache_time"
    KEY_SCHEDULE_BACKUP_DB_DAY = "ops:schedule_backup_db_day"
    KEY_SCHEDULE_BACKUP_DB_TIME = "ops:schedule_backup_db_time"
    KEY_SCHEDULE_REQUEST_EXPIRE_HOURS = "ops:schedule_request_expire_hours"
    KEY_SCHEDULE_NSFW_RETRY_MINUTES = "ops:schedule_nsfw_retry_minutes"
    KEY_DB_BACKUP_RETENTION_DAYS = "ops:db_backup_retention_days"

    @staticmethod
    def parse_exclude_keywords(raw: str | None) -> list[str]:
        """解析 MKV 排除关键词列表（支持逗号或换行分隔）"""
        if not raw:
            return []
        normalized = raw.replace("\r\n", "\n").replace(",", "\n")
        return [item.strip() for item in normalized.split("\n") if item.strip()]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @classmethod
    async def load_all_to_cache(cls, session: AsyncSession):
        """加载所有配置到缓存，并初始化默认配置"""

        # Load all existing configs
        stmt = select(BotConfiguration)
        result = await session.execute(stmt)
        configs = result.scalars().all()

        cls.cache.clear()
        for config in configs:
            cls.cache[config.key] = config.value

        # 定义默认值以确保它们存在于数据库（持久性）和缓存中
        # 字典格式：{key:value}
        persistent_defaults = {
            cls.KEY_ENABLE_POINTS: "true",
            cls.KEY_ENABLE_VERIFICATION: "true",
            cls.KEY_ENABLE_CLEANUP_INACTIVE_USERS: "false",
            cls.KEY_ENABLE_MKV_PROCESS: "true",
            cls.KEY_MKV_EXCLUDE_KEYWORDS: "",
            cls.KEY_ENABLE_LLM_TRANSLATE: "false",
            cls.KEY_AI_API_KEY: "",
            cls.KEY_AI_BASE_URL: "https://api.openai.com/v1",
            cls.KEY_AI_MODEL: "gpt-4o-mini",
            cls.KEY_AI_TEMPERATURE: "1.3",
            cls.KEY_AI_RPM: "60",
            cls.KEY_AI_RPD: "",
            cls.KEY_AI_TPM: "",
            cls.KEY_AI_CONCURRENCY: "1",
            cls.KEY_TMDB_API_KEY: "",
            cls.KEY_TVDB_API_KEY: "",
            cls.KEY_POINTS_RENEW_MODE: "median",
            cls.KEY_POINTS_RENEW_FIXED: "100",
            cls.KEY_POINTS_RENEW_MIN: "100",
            cls.KEY_POINTS_RENEW_MAX: "1000",
            cls.KEY_POINTS_REQUEST_MODE: "ratio",
            cls.KEY_POINTS_REQUEST_RATIO: "0.1",
            cls.KEY_POINTS_REQUEST_FIXED: "10",
            cls.KEY_POINTS_CHECKIN_MIN: "1",
            cls.KEY_POINTS_CHECKIN_MAX: "3",
            cls.KEY_POINTS_CHECKIN_CONSECUTIVE_BONUS: "2",
            cls.KEY_POINTS_CHECKIN_LUCKY_INTERVAL: "7",
            cls.KEY_POINTS_MESSAGE_DAILY_CAP: "20",
            cls.KEY_POINTS_SPAM_THRESHOLD: "5",
            cls.KEY_VERIFICATION_TIMEOUT_MINUTES: "5",
            cls.KEY_ACCOUNT_DELETE_GRACE_DAYS: "7",
            cls.KEY_ACCOUNT_RENEW_ADVANCE_DAYS: "7",
            cls.KEY_REQUEST_EXPIRE_DAYS: "7",
            cls.KEY_SCHEDULE_BAN_EXPIRED_DAY: "*",
            cls.KEY_SCHEDULE_BAN_EXPIRED_TIME: "00:15",
            cls.KEY_SCHEDULE_DELETE_BANNED_DAY: "*",
            cls.KEY_SCHEDULE_DELETE_BANNED_TIME: "00:30",
            cls.KEY_SCHEDULE_CLEANUP_INACTIVE_DAY: "*",
            cls.KEY_SCHEDULE_CLEANUP_INACTIVE_TIME: "01:00",
            cls.KEY_SCHEDULE_SETTLE_SCORES_DAY: "*",
            cls.KEY_SCHEDULE_SETTLE_SCORES_TIME: "23:00",
            cls.KEY_SCHEDULE_CLEANUP_CACHE_DAY: "*",
            cls.KEY_SCHEDULE_CLEANUP_CACHE_TIME: "03:00",
            cls.KEY_SCHEDULE_BACKUP_DB_DAY: "*",
            cls.KEY_SCHEDULE_BACKUP_DB_TIME: "04:00",
            cls.KEY_SCHEDULE_REQUEST_EXPIRE_HOURS: "1",
            cls.KEY_SCHEDULE_NSFW_RETRY_MINUTES: "10",
            cls.KEY_DB_BACKUP_RETENTION_DAYS: "7",
        }

        new_items = []
        for key, value in persistent_defaults.items():
            if key not in cls.cache:
                cls.cache[key] = value
                new_items.append(BotConfiguration(key=key, value=value))

        if new_items:
            session.add_all(new_items)
            await session.commit()

    async def get_settings(self, key: str, default: str | None = None) -> str | None:
        """通过键获取配置值，优先读取缓存
        Args:
            key (str): 配置键
            default (str | None): 默认值，可选
        Returns:
            str | None: 如果找到配置则返回配置值，否则返回默认值
        """
        if key in self.cache:
            return self.cache[key]

        value = await self.session.get(BotConfiguration, key)
        if value:
            self.cache[key] = value.value
            return value.value

        return default

    async def set_settings(self, key: str, value: str) -> BotConfiguration:
        """设置配置值，同时更新缓存
        Args:
            key (str): 配置键
            value (str): 配置值
        """
        # Update Cache immediately
        self.cache[key] = value

        setting = await self.session.get(BotConfiguration, key)

        if setting:
            setting.value = value
        else:
            setting = BotConfiguration(key=key, value=value)
            self.session.add(setting)

        await self.session.commit()
        await self.session.refresh(setting)
        return setting

    @staticmethod
    def _parse_optional_int(raw: str | None) -> int | None:
        """将配置字符串解析为可选正整数，空字符串或无效值返回 None。"""
        if not raw or not raw.strip():
            return None
        try:
            val = int(raw.strip())
            return val if val > 0 else None
        except ValueError:
            return None

    async def get_llm_config(self) -> dict[str, Any]:
        """读取并解析数据库中的 LLM 翻译开关及全部 AI 参数。

        Returns:
            包含 ``enable_llm_translate`` 及 ``ai_*`` 类型化字段的字典。
        """
        enable_raw = await self.get_settings(self.KEY_ENABLE_LLM_TRANSLATE, "false")
        api_key = (await self.get_settings(self.KEY_AI_API_KEY, "") or "").strip()
        base_url = (
            await self.get_settings(self.KEY_AI_BASE_URL, "https://api.openai.com/v1")
            or "https://api.openai.com/v1"
        ).strip()
        model = (
            await self.get_settings(self.KEY_AI_MODEL, "gpt-4o-mini") or "gpt-4o-mini"
        ).strip()

        temp_raw = await self.get_settings(self.KEY_AI_TEMPERATURE, "1.3")
        try:
            temperature = float(temp_raw) if temp_raw else 1.3
        except ValueError:
            temperature = 1.3

        rpm = self._parse_optional_int(await self.get_settings(self.KEY_AI_RPM, "60"))
        rpd = self._parse_optional_int(await self.get_settings(self.KEY_AI_RPD, ""))
        tpm = self._parse_optional_int(await self.get_settings(self.KEY_AI_TPM, ""))
        concurrency = (
            self._parse_optional_int(
                await self.get_settings(self.KEY_AI_CONCURRENCY, "1")
            )
            or 1
        )

        return {
            "enable_llm_translate": enable_raw == "true",
            "ai_api_key": api_key,
            "ai_base_url": base_url,
            "ai_model": model,
            "ai_temperature": temperature,
            "ai_rpm": rpm,
            "ai_rpd": rpd,
            "ai_tpm": tpm,
            "ai_concurrency": concurrency,
        }

    async def get_metadata_config(self) -> dict[str, str]:
        """读取数据库中的 TMDB 与 TVDB API Key 配置。

        Returns:
            包含 ``tmdb_api_key`` 与 ``tvdb_api_key`` 字符串的字典。
        """
        tmdb_key = (await self.get_settings(self.KEY_TMDB_API_KEY, "") or "").strip()
        tvdb_key = (await self.get_settings(self.KEY_TVDB_API_KEY, "") or "").strip()
        return {
            "tmdb_api_key": tmdb_key,
            "tvdb_api_key": tvdb_key,
        }

    @staticmethod
    def _parse_int(raw: str | None, default: int, minimum: int = 0) -> int:
        """将配置字符串解析为不小于 ``minimum`` 的整数，无效时返回默认值。"""
        if not raw or not raw.strip():
            return default
        try:
            val = int(raw.strip())
            return val if val >= minimum else default
        except ValueError:
            return default

    @staticmethod
    def _parse_float(raw: str | None, default: float, minimum: float = 0.0) -> float:
        """将配置字符串解析为不小于 ``minimum`` 的浮点数，无效时返回默认值。"""
        if not raw or not raw.strip():
            return default
        try:
            val = float(raw.strip())
            return val if val >= minimum else default
        except ValueError:
            return default

    @classmethod
    def _build_points_config(cls, source: dict[str, str]) -> dict[str, Any]:
        """从配置键值字典解析积分、签到与定价规则配置。"""
        renew_mode = (source.get(cls.KEY_POINTS_RENEW_MODE) or "median").strip()
        if renew_mode not in ("median", "fixed"):
            renew_mode = "median"

        renew_fixed = cls._parse_int(source.get(cls.KEY_POINTS_RENEW_FIXED), 100, 0)
        renew_min = cls._parse_int(source.get(cls.KEY_POINTS_RENEW_MIN), 100, 0)
        renew_max = cls._parse_int(source.get(cls.KEY_POINTS_RENEW_MAX), 1000, 0)
        if renew_max < renew_min:
            renew_max = renew_min

        request_mode = (source.get(cls.KEY_POINTS_REQUEST_MODE) or "ratio").strip()
        if request_mode not in ("ratio", "fixed"):
            request_mode = "ratio"

        request_ratio = cls._parse_float(
            source.get(cls.KEY_POINTS_REQUEST_RATIO), 0.1, 0.0
        )
        request_fixed = cls._parse_int(source.get(cls.KEY_POINTS_REQUEST_FIXED), 10, 0)

        checkin_min = cls._parse_int(source.get(cls.KEY_POINTS_CHECKIN_MIN), 1, 0)
        checkin_max = cls._parse_int(source.get(cls.KEY_POINTS_CHECKIN_MAX), 3, 0)
        if checkin_max < checkin_min:
            checkin_max = checkin_min

        checkin_bonus = cls._parse_int(
            source.get(cls.KEY_POINTS_CHECKIN_CONSECUTIVE_BONUS), 2, 0
        )
        checkin_lucky_interval = cls._parse_int(
            source.get(cls.KEY_POINTS_CHECKIN_LUCKY_INTERVAL), 7, 0
        )
        message_daily_cap = cls._parse_int(
            source.get(cls.KEY_POINTS_MESSAGE_DAILY_CAP), 20, 0
        )
        spam_threshold = cls._parse_int(source.get(cls.KEY_POINTS_SPAM_THRESHOLD), 5, 0)

        return {
            "points_renew_mode": renew_mode,
            "points_renew_fixed": renew_fixed,
            "points_renew_min": renew_min,
            "points_renew_max": renew_max,
            "points_request_mode": request_mode,
            "points_request_ratio": request_ratio,
            "points_request_fixed": request_fixed,
            "points_checkin_min": checkin_min,
            "points_checkin_max": checkin_max,
            "points_checkin_consecutive_bonus": checkin_bonus,
            "points_checkin_lucky_interval": checkin_lucky_interval,
            "points_message_daily_cap": message_daily_cap,
            "points_spam_threshold": spam_threshold,
        }

    @classmethod
    def get_points_config_from_cache(cls) -> dict[str, Any]:
        """同步读取内存缓存中的积分、签到与定价规则配置。"""
        return cls._build_points_config(cls.cache)

    async def get_points_config(self) -> dict[str, Any]:
        """读取数据库/缓存中的积分、签到与定价规则配置。"""
        keys = (
            self.KEY_POINTS_RENEW_MODE,
            self.KEY_POINTS_RENEW_FIXED,
            self.KEY_POINTS_RENEW_MIN,
            self.KEY_POINTS_RENEW_MAX,
            self.KEY_POINTS_REQUEST_MODE,
            self.KEY_POINTS_REQUEST_RATIO,
            self.KEY_POINTS_REQUEST_FIXED,
            self.KEY_POINTS_CHECKIN_MIN,
            self.KEY_POINTS_CHECKIN_MAX,
            self.KEY_POINTS_CHECKIN_CONSECUTIVE_BONUS,
            self.KEY_POINTS_CHECKIN_LUCKY_INTERVAL,
            self.KEY_POINTS_MESSAGE_DAILY_CAP,
            self.KEY_POINTS_SPAM_THRESHOLD,
        )
        source: dict[str, str] = {}
        for key in keys:
            val = await self.get_settings(key)
            if val is not None:
                source[key] = val
        return self._build_points_config(source)

    @classmethod
    def _build_policy_config(cls, source: dict[str, str]) -> dict[str, int]:
        """从配置键值字典解析验证超时与账号/求片宽限期配置。"""
        return {
            "verification_timeout_minutes": cls._parse_int(
                source.get(cls.KEY_VERIFICATION_TIMEOUT_MINUTES), 5, 1
            ),
            "account_delete_grace_days": cls._parse_int(
                source.get(cls.KEY_ACCOUNT_DELETE_GRACE_DAYS), 7, 0
            ),
            "account_renew_advance_days": cls._parse_int(
                source.get(cls.KEY_ACCOUNT_RENEW_ADVANCE_DAYS), 7, 0
            ),
            "request_expire_days": cls._parse_int(
                source.get(cls.KEY_REQUEST_EXPIRE_DAYS), 7, 1
            ),
        }

    @classmethod
    def get_policy_config_from_cache(cls) -> dict[str, int]:
        """同步读取内存缓存中的验证超时与账号/求片宽限期配置。"""
        return cls._build_policy_config(cls.cache)

    async def get_policy_config(self) -> dict[str, int]:
        """读取数据库/缓存中的验证超时与账号/求片宽限期配置。"""
        keys = (
            self.KEY_VERIFICATION_TIMEOUT_MINUTES,
            self.KEY_ACCOUNT_DELETE_GRACE_DAYS,
            self.KEY_ACCOUNT_RENEW_ADVANCE_DAYS,
            self.KEY_REQUEST_EXPIRE_DAYS,
        )
        source: dict[str, str] = {}
        for key in keys:
            val = await self.get_settings(key)
            if val is not None:
                source[key] = val
        return self._build_policy_config(source)

    @staticmethod
    def _parse_hhmm(raw: str | None, default: str) -> str:
        """校验并规范化 ``HH:MM`` 时间字符串，无效时返回默认值。"""
        text = (raw or "").strip()
        parts = text.split(":")
        if len(parts) != 2:
            return default
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            return default
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return default

    @staticmethod
    def _parse_cron_day(raw: str | None) -> str:
        """读取并规范化 cron ``day`` 字段，留空时回退为 ``*``。"""
        text = (raw or "").strip()
        return text or "*"

    @classmethod
    def _build_ops_config(cls, source: dict[str, str]) -> dict[str, Any]:
        """从配置键值字典解析定时任务调度与运维保留策略配置。"""
        return {
            "schedule_ban_expired_day": cls._parse_cron_day(
                source.get(cls.KEY_SCHEDULE_BAN_EXPIRED_DAY)
            ),
            "schedule_ban_expired_time": cls._parse_hhmm(
                source.get(cls.KEY_SCHEDULE_BAN_EXPIRED_TIME), "00:15"
            ),
            "schedule_delete_banned_day": cls._parse_cron_day(
                source.get(cls.KEY_SCHEDULE_DELETE_BANNED_DAY)
            ),
            "schedule_delete_banned_time": cls._parse_hhmm(
                source.get(cls.KEY_SCHEDULE_DELETE_BANNED_TIME), "00:30"
            ),
            "schedule_cleanup_inactive_day": cls._parse_cron_day(
                source.get(cls.KEY_SCHEDULE_CLEANUP_INACTIVE_DAY)
            ),
            "schedule_cleanup_inactive_time": cls._parse_hhmm(
                source.get(cls.KEY_SCHEDULE_CLEANUP_INACTIVE_TIME), "01:00"
            ),
            "schedule_settle_scores_day": cls._parse_cron_day(
                source.get(cls.KEY_SCHEDULE_SETTLE_SCORES_DAY)
            ),
            "schedule_settle_scores_time": cls._parse_hhmm(
                source.get(cls.KEY_SCHEDULE_SETTLE_SCORES_TIME), "23:00"
            ),
            "schedule_cleanup_cache_day": cls._parse_cron_day(
                source.get(cls.KEY_SCHEDULE_CLEANUP_CACHE_DAY)
            ),
            "schedule_cleanup_cache_time": cls._parse_hhmm(
                source.get(cls.KEY_SCHEDULE_CLEANUP_CACHE_TIME), "03:00"
            ),
            "schedule_backup_db_day": cls._parse_cron_day(
                source.get(cls.KEY_SCHEDULE_BACKUP_DB_DAY)
            ),
            "schedule_backup_db_time": cls._parse_hhmm(
                source.get(cls.KEY_SCHEDULE_BACKUP_DB_TIME), "04:00"
            ),
            "schedule_request_expire_hours": cls._parse_int(
                source.get(cls.KEY_SCHEDULE_REQUEST_EXPIRE_HOURS), 1, 1
            ),
            "schedule_nsfw_retry_minutes": cls._parse_int(
                source.get(cls.KEY_SCHEDULE_NSFW_RETRY_MINUTES), 10, 1
            ),
            "db_backup_retention_days": cls._parse_int(
                source.get(cls.KEY_DB_BACKUP_RETENTION_DAYS), 7, 0
            ),
        }

    @classmethod
    def get_ops_config_from_cache(cls) -> dict[str, Any]:
        """同步读取内存缓存中的定时任务调度与运维策略配置。"""
        return cls._build_ops_config(cls.cache)

    async def get_ops_config(self) -> dict[str, Any]:
        """读取数据库/缓存中的定时任务调度与运维策略配置。"""
        keys = (
            self.KEY_SCHEDULE_BAN_EXPIRED_DAY,
            self.KEY_SCHEDULE_BAN_EXPIRED_TIME,
            self.KEY_SCHEDULE_DELETE_BANNED_DAY,
            self.KEY_SCHEDULE_DELETE_BANNED_TIME,
            self.KEY_SCHEDULE_CLEANUP_INACTIVE_DAY,
            self.KEY_SCHEDULE_CLEANUP_INACTIVE_TIME,
            self.KEY_SCHEDULE_SETTLE_SCORES_DAY,
            self.KEY_SCHEDULE_SETTLE_SCORES_TIME,
            self.KEY_SCHEDULE_CLEANUP_CACHE_DAY,
            self.KEY_SCHEDULE_CLEANUP_CACHE_TIME,
            self.KEY_SCHEDULE_BACKUP_DB_DAY,
            self.KEY_SCHEDULE_BACKUP_DB_TIME,
            self.KEY_SCHEDULE_REQUEST_EXPIRE_HOURS,
            self.KEY_SCHEDULE_NSFW_RETRY_MINUTES,
            self.KEY_DB_BACKUP_RETENTION_DAYS,
        )
        source: dict[str, str] = {}
        for key in keys:
            val = await self.get_settings(key)
            if val is not None:
                source[key] = val
        return self._build_ops_config(source)
