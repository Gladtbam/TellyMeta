from typing import Any, ClassVar, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.orm import BotConfiguration

BYTES_PER_GIB = 1024**3
DEFAULT_TRAFFIC_PACK_COST_SCORE = 50
DEFAULT_TRAFFIC_PACK_SIZE_GIB = 10.0
DEFAULT_TRAFFIC_PACK_UNIT_BYTES = round(DEFAULT_TRAFFIC_PACK_SIZE_GIB * BYTES_PER_GIB)


class TrafficConfigDict(TypedDict):
    """流量 UDP 监听、刷盘参数及流量包单价配置类型字典"""

    traffic_udp_host: str
    traffic_udp_port: int
    traffic_flush_interval_seconds: int
    traffic_pack_cost_score: int
    traffic_pack_size_gib: float
    traffic_pack_unit_bytes: int


class ConfigRepository:
    cache: ClassVar[dict[str, str]] = {}

    # 积分细粒度子开关映射：字段名 -> (数据库配置键, 中文能力名称)
    POINTS_SUB_SWITCHES: ClassVar[dict[str, tuple[str, str]]] = {
        "enable_points_checkin": ("points:enable_checkin", "签到积分"),
        "enable_points_chat": ("points:enable_chat", "群聊发言积分"),
        "enable_points_renew": ("points:enable_renew", "积分续期"),
        "enable_points_request": ("points:enable_request", "积分求片"),
        "enable_points_generate_code": (
            "points:enable_generate_code",
            "积分生成激活码",
        ),
        "enable_points_traffic_pack": (
            "points:enable_traffic_pack",
            "积分兑换流量包",
        ),
    }
    _POINTS_SUB_SWITCH_LABELS: ClassVar[dict[str, str]] = dict(
        POINTS_SUB_SWITCHES.values()
    )

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
    KEY_ENABLE_POINTS_CHECKIN = "points:enable_checkin"
    KEY_ENABLE_POINTS_CHAT = "points:enable_chat"
    KEY_ENABLE_POINTS_RENEW = "points:enable_renew"
    KEY_ENABLE_POINTS_REQUEST = "points:enable_request"
    KEY_ENABLE_POINTS_GENERATE_CODE = "points:enable_generate_code"
    KEY_ENABLE_POINTS_TRAFFIC_PACK = "points:enable_traffic_pack"
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

    # 播放时长统计与 Emby 心跳配置键
    KEY_ENABLE_WATCH_TIME = "watch_time:enable"
    KEY_WATCH_TIME_DAILY_CAP_SECONDS = "watch_time:daily_cap_seconds"
    KEY_WATCH_TIME_HEARTBEAT_INTERVAL_SECONDS = (
        "watch_time:heartbeat_interval_seconds"
    )
    KEY_WATCH_TIME_DISCONNECT_GRACE_SECONDS = "watch_time:disconnect_grace_seconds"

    # 流量统计、UDP 接收、刷盘与流量包兑换单价配置键
    KEY_ENABLE_TRAFFIC = "traffic:enable"
    KEY_TRAFFIC_UDP_HOST = "traffic:udp_host"
    KEY_TRAFFIC_UDP_PORT = "traffic:udp_port"
    KEY_TRAFFIC_FLUSH_INTERVAL_SECONDS = "traffic:flush_interval_seconds"
    KEY_TRAFFIC_PACK_COST_SCORE = "traffic:pack_cost_score"
    KEY_TRAFFIC_PACK_SIZE_GIB = "traffic:pack_size_gib"

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
            cls.KEY_ENABLE_POINTS_CHECKIN: "true",
            cls.KEY_ENABLE_POINTS_CHAT: "true",
            cls.KEY_ENABLE_POINTS_RENEW: "true",
            cls.KEY_ENABLE_POINTS_REQUEST: "true",
            cls.KEY_ENABLE_POINTS_GENERATE_CODE: "true",
            cls.KEY_ENABLE_POINTS_TRAFFIC_PACK: "true",
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
            cls.KEY_ENABLE_WATCH_TIME: "true",
            cls.KEY_WATCH_TIME_DAILY_CAP_SECONDS: "43200",
            cls.KEY_WATCH_TIME_HEARTBEAT_INTERVAL_SECONDS: "30",
            cls.KEY_WATCH_TIME_DISCONNECT_GRACE_SECONDS: "45",
            cls.KEY_ENABLE_TRAFFIC: "false",
            cls.KEY_TRAFFIC_UDP_HOST: "0.0.0.0",
            cls.KEY_TRAFFIC_UDP_PORT: "5140",
            cls.KEY_TRAFFIC_FLUSH_INTERVAL_SECONDS: "10",
            cls.KEY_TRAFFIC_PACK_COST_SCORE: str(DEFAULT_TRAFFIC_PACK_COST_SCORE),
            cls.KEY_TRAFFIC_PACK_SIZE_GIB: str(int(DEFAULT_TRAFFIC_PACK_SIZE_GIB)),
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

        sub_switches = {
            field: (source.get(db_key, "true") == "true")
            for field, (db_key, _) in cls.POINTS_SUB_SWITCHES.items()
        }

        return {
            **sub_switches,
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
    def _evaluate_points_feature(
        cls,
        master_raw: str | None,
        sub_raw: str | None,
        sub_key: str,
    ) -> tuple[bool, str]:
        """校验单项目积分能力是否生效（总开关具备一票否决权）。

        Args:
            master_raw: 全局积分总开关原始字符串值（缺省视为 ``"true"``）
            sub_raw: 目标子开关原始字符串值（缺省视为 ``"true"``）
            sub_key: 目标子开关对应的数据库配置键

        Returns:
            ``(是否生效, 禁用原因说明)``；生效时原因文本为空字符串
        """
        label = cls._POINTS_SUB_SWITCH_LABELS.get(sub_key, "该积分功能")
        # 全局积分总开关未开启时对全部子能力一票否决
        if (master_raw if master_raw is not None else "true") != "true":
            return False, f"全局积分总开关已关闭，{label}功能暂不可用。"
        if (sub_raw if sub_raw is not None else "true") != "true":
            return False, f"{label}功能已关闭。"
        return True, ""

    @classmethod
    def check_points_feature_from_cache(cls, sub_key: str) -> tuple[bool, str]:
        """同步读取内存缓存，校验指定积分子能力是否生效并返回原因。

        Args:
            sub_key: 目标子开关配置键（如 ``KEY_ENABLE_POINTS_CHECKIN``）

        Returns:
            ``(是否生效, 禁用原因说明)``
        """
        return cls._evaluate_points_feature(
            cls.cache.get(cls.KEY_ENABLE_POINTS, "true"),
            cls.cache.get(sub_key, "true"),
            sub_key,
        )

    @classmethod
    def is_points_feature_enabled_from_cache(cls, sub_key: str) -> bool:
        """同步读取内存缓存，判断指定积分子能力是否在总开关放行下处于开启状态。

        Args:
            sub_key: 目标子开关配置键

        Returns:
            总开关与目标子开关同时开启返回 ``True``，否则返回 ``False``
        """
        return cls.check_points_feature_from_cache(sub_key)[0]

    async def check_points_feature(self, sub_key: str) -> tuple[bool, str]:
        """读取缓存或数据库，校验指定积分子能力是否生效并返回原因。

        Args:
            sub_key: 目标子开关配置键

        Returns:
            ``(是否生效, 禁用原因说明)``
        """
        master_raw = await self.get_settings(self.KEY_ENABLE_POINTS, "true")
        sub_raw = await self.get_settings(sub_key, "true")
        return self._evaluate_points_feature(master_raw, sub_raw, sub_key)

    async def is_points_feature_enabled(self, sub_key: str) -> bool:
        """读取缓存或数据库，判断指定积分子能力是否生效。

        Args:
            sub_key: 目标子开关配置键

        Returns:
            总开关与目标子开关同时开启返回 ``True``，否则返回 ``False``
        """
        return (await self.check_points_feature(sub_key))[0]

    @classmethod
    def get_points_sub_switch_label(cls, sub_key: str) -> str | None:
        """根据子开关配置键返回对应中文能力名称，非积分子开关时返回 None。

        Args:
            sub_key: 数据库配置键名。

        Returns:
            中文能力名称字符串或 None。
        """
        return cls._POINTS_SUB_SWITCH_LABELS.get(sub_key)

    async def get_effective_points_switches(self) -> dict[str, bool]:
        """获取 6 项积分细粒度子开关经过总开关一票否决后的实际生效状态字典。

        Returns:
            以字段名（如 ``enable_points_checkin``）为键、生效布尔值为值的字典
        """
        master_raw = await self.get_settings(self.KEY_ENABLE_POINTS, "true")
        effective: dict[str, bool] = {}
        for field, (db_key, _) in self.POINTS_SUB_SWITCHES.items():
            sub_raw = await self.get_settings(db_key, "true")
            effective[field] = self._evaluate_points_feature(
                master_raw, sub_raw, db_key
            )[0]
        return effective

    @classmethod
    def get_points_config_from_cache(cls) -> dict[str, Any]:
        """同步读取内存缓存中的积分、签到与定价规则配置。"""
        return cls._build_points_config(cls.cache)

    async def get_points_config(self) -> dict[str, Any]:
        """读取数据库/缓存中的积分、签到与定价规则配置。"""
        keys = (
            *(db_key for db_key, _ in self.POINTS_SUB_SWITCHES.values()),
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

    @classmethod
    def _build_watch_time_config(cls, source: dict[str, str]) -> dict[str, int]:
        """从配置键值字典解析播放时长上限、Emby 心跳间隔与断线宽限参数。

        Args:
            source: 包含播放时长配置键的字典。

        Returns:
            包含整数化参数与边界保底后的配置字典。
        """
        return {
            "watch_time_daily_cap_seconds": cls._parse_int(
                source.get(cls.KEY_WATCH_TIME_DAILY_CAP_SECONDS), 43200, 0
            ),
            "watch_time_heartbeat_interval_seconds": cls._parse_int(
                source.get(cls.KEY_WATCH_TIME_HEARTBEAT_INTERVAL_SECONDS), 30, 5
            ),
            "watch_time_disconnect_grace_seconds": cls._parse_int(
                source.get(cls.KEY_WATCH_TIME_DISCONNECT_GRACE_SECONDS), 45, 5
            ),
        }

    @classmethod
    def get_watch_time_config_from_cache(cls) -> dict[str, int]:
        """同步读取内存缓存中的播放时长与心跳配置。"""
        return cls._build_watch_time_config(cls.cache)

    @classmethod
    def is_watch_time_enabled_from_cache(cls) -> bool:
        """同步读取全局播放时长统计总开关状态。"""
        return cls.cache.get(cls.KEY_ENABLE_WATCH_TIME, "true") == "true"

    async def is_watch_time_enabled(self) -> bool:
        """异步读取全局播放时长统计总开关状态。"""
        return await self.get_settings(self.KEY_ENABLE_WATCH_TIME, "true") == "true"

    async def get_watch_time_config(self) -> dict[str, int]:
        """读取数据库/缓存中的播放时长上限与 Emby 心跳参数配置。"""
        keys = (
            self.KEY_WATCH_TIME_DAILY_CAP_SECONDS,
            self.KEY_WATCH_TIME_HEARTBEAT_INTERVAL_SECONDS,
            self.KEY_WATCH_TIME_DISCONNECT_GRACE_SECONDS,
        )
        source: dict[str, str] = {}
        for key in keys:
            val = await self.get_settings(key)
            if val is not None:
                source[key] = val
        return self._build_watch_time_config(source)

    @classmethod
    def _build_traffic_config(cls, source: dict[str, str]) -> TrafficConfigDict:
        """从配置键值字典解析 Nginx 流量 UDP 监听、刷盘周期及流量包积分兑换单价

        Args:
            source: 包含流量监听、刷盘及流量包单价配置键的字典。

        Returns:
            经过端口上下界保底与类型转换后的 ``TrafficConfigDict``。
        """
        raw_host = (source.get(cls.KEY_TRAFFIC_UDP_HOST) or "").strip()
        udp_host = raw_host or "0.0.0.0"
        udp_port = cls._parse_int(source.get(cls.KEY_TRAFFIC_UDP_PORT), 5140, 1)
        # UDP 端口号有效范围为 1..65535，越界时回退默认端口 5140
        if udp_port > 65535:
            udp_port = 5140
        flush_interval = cls._parse_int(
            source.get(cls.KEY_TRAFFIC_FLUSH_INTERVAL_SECONDS), 10, 1
        )
        pack_cost_score = cls._parse_int(
            source.get(cls.KEY_TRAFFIC_PACK_COST_SCORE),
            DEFAULT_TRAFFIC_PACK_COST_SCORE,
            1,
        )
        pack_size_gib = cls._parse_float(
            source.get(cls.KEY_TRAFFIC_PACK_SIZE_GIB),
            DEFAULT_TRAFFIC_PACK_SIZE_GIB,
            0.0,
        )
        if pack_size_gib <= 0:
            pack_size_gib = DEFAULT_TRAFFIC_PACK_SIZE_GIB
        pack_unit_bytes = max(1, round(pack_size_gib * BYTES_PER_GIB))
        return {
            "traffic_udp_host": udp_host,
            "traffic_udp_port": udp_port,
            "traffic_flush_interval_seconds": flush_interval,
            "traffic_pack_cost_score": pack_cost_score,
            "traffic_pack_size_gib": pack_size_gib,
            "traffic_pack_unit_bytes": pack_unit_bytes,
        }

    @classmethod
    def get_traffic_config_from_cache(cls) -> TrafficConfigDict:
        """同步读取内存缓存中的流量 UDP 监听、刷盘及流量包单价参数

        Returns:
            包含当前缓存流量参数的 ``TrafficConfigDict``。
        """
        return cls._build_traffic_config(cls.cache)

    @classmethod
    def is_traffic_enabled_from_cache(cls) -> bool:
        """同步读取全局流量采集总开关状态（默认关闭以确保未配置 Nginx 时安全降级）

        Returns:
            全局流量总开关开启返回 ``True``，否则返回 ``False``。
        """
        return cls.cache.get(cls.KEY_ENABLE_TRAFFIC, "false") == "true"

    async def is_traffic_enabled(self) -> bool:
        """异步读取全局流量采集总开关状态

        Returns:
            全局流量总开关开启返回 ``True``，否则返回 ``False``。
        """
        return await self.get_settings(self.KEY_ENABLE_TRAFFIC, "false") == "true"

    async def get_traffic_config(self) -> TrafficConfigDict:
        """读取数据库/缓存中的流量 UDP 监听、刷盘周期及流量包兑换单价配置

        Returns:
            经规范化与默认值兜底后的 ``TrafficConfigDict``。
        """
        keys = (
            self.KEY_TRAFFIC_UDP_HOST,
            self.KEY_TRAFFIC_UDP_PORT,
            self.KEY_TRAFFIC_FLUSH_INTERVAL_SECONDS,
            self.KEY_TRAFFIC_PACK_COST_SCORE,
            self.KEY_TRAFFIC_PACK_SIZE_GIB,
        )
        source: dict[str, str] = {}
        for key in keys:
            val = await self.get_settings(key)
            if val is not None:
                source[key] = val
        return self._build_traffic_config(source)
