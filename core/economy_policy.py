"""积分、流量与播放时长三体系的有效启用态评估、单服定价与流量额度纯规则策略模块"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from random import randint
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from models.orm import MediaUser, ServerInstance

BYTES_PER_GIB = 1024**3
DEFAULT_TRAFFIC_PACK_COST_SCORE = 50
DEFAULT_TRAFFIC_PACK_SIZE_GIB = 10.0

PricingMode = Literal["fixed", "ratio"]
VALID_PRICING_MODES: tuple[PricingMode, ...] = ("fixed", "ratio")


def normalize_pricing_mode(mode: str | None) -> PricingMode:
    """将请求或库中的积分定价模式字符串归一化为合法的 ``PricingMode`` 值。

    Args:
        mode: 原始定价模式字符串（如 ``"fixed"``、``"ratio"`` 或历史空值）。

    Returns:
        合法的 ``"fixed"`` 或 ``"ratio"``，未知值回退为 ``"fixed"``。
    """
    return mode if mode in VALID_PRICING_MODES else "fixed"  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class TrafficBalance:
    """单账号当期流量有效基础配额与双池剩余额度快照（字节）"""

    is_limited: bool
    server_quota_bytes: int
    adjustment_bytes: int
    effective_base_quota_bytes: int
    cycle_used_bytes: int
    pack_remaining_bytes: int
    base_remaining_bytes: int | None
    total_remaining_bytes: int | None
    total_pool_bytes: int | None


def is_watch_time_effective(
    global_watch_time_enabled: bool,
    server: ServerInstance | None,
) -> bool:
    """判断目标服务器当前是否有效开启有效播放时长采集。

    Args:
        global_watch_time_enabled: 全局 ``watch_time:enable`` 开关状态。
        server: 目标媒体服务器实例。

    Returns:
        全局开关、服务器实例及其单服时长采集开关均开启时返回 ``True``，否则返回 ``False``。
    """
    return bool(
        global_watch_time_enabled
        and server is not None
        and bool(server.enable_watch_time)
    )


def is_watch_time_renewal_open(
    global_watch_time_enabled: bool,
    server: ServerInstance | None,
) -> bool:
    """按逐级一票否决规则判断目标服务器当前是否开放时长保活续期。

    依次受制于 ``全局时长总开关 -> 单服时长采集 -> 单服保活开关且门槛 > 0``，
    上层关闭时下层对外生效态一律视为关闭，同时不改变数据库既有持久化阈值。

    Args:
        global_watch_time_enabled: 全局 ``watch_time:enable`` 开关状态。
        server: 目标媒体服务器实例。

    Returns:
        仅当逐级前置开关均开启且保活门槛大于 0 秒时返回 ``True``。
    """
    if not is_watch_time_effective(global_watch_time_enabled, server) or server is None:
        return False
    threshold_seconds = max(0, int(server.watch_time_renewal_threshold_seconds or 0))
    return bool(server.enable_watch_time_renewal) and threshold_seconds > 0


def is_auto_renew_open(
    server: ServerInstance | None,
    *,
    watch_time_renewal_open: bool,
    points_renew_open: bool,
) -> bool:
    """判断指定服务器当前是否开放“自动续期”能力

    只要目标服务器已启用且存在至少一种可自动触发的续期条件
    （单服时长保活续期或全局积分续期），即允许用户为该服账号开启自动续期。

    Args:
        server: 目标媒体服务器实例。
        watch_time_renewal_open: 当前服务器是否有效开放时长保活续期。
        points_renew_open: 当前系统是否有效开放积分续期。

    Returns:
        服务器已启用且任一续期条件生效时返回 ``True``，否则返回 ``False``。
    """
    if server is None or not server.is_enabled:
        return False
    return bool(watch_time_renewal_open or points_renew_open)


def is_traffic_effective(
    global_traffic_enabled: bool,
    server: ServerInstance | None,
) -> bool:
    """判断目标服务器当前是否有效开启流量采集与统计。

    Args:
        global_traffic_enabled: 全局 ``traffic:enable`` 开关状态。
        server: 目标媒体服务器实例。

    Returns:
        全局开关与单服 ``enable_traffic`` 均开启时返回 ``True``，否则返回 ``False``。
    """
    return bool(
        global_traffic_enabled and server is not None and bool(server.enable_traffic)
    )


def is_traffic_quota_limited(
    global_traffic_enabled: bool,
    server: ServerInstance | None,
) -> bool:
    """判断目标服务器当前是否有效处于有限流量配额管制下。

    Args:
        global_traffic_enabled: 全局 ``traffic:enable`` 开关状态。
        server: 目标媒体服务器实例。

    Returns:
        流量有效启用且月基础配额大于 0 字节时返回 ``True``。
    """
    return bool(
        is_traffic_effective(global_traffic_enabled, server)
        and server is not None
        and max(0, int(server.traffic_quota_bytes or 0)) > 0
    )


def is_traffic_alert_effective(
    global_traffic_enabled: bool,
    server: ServerInstance | None,
) -> bool:
    """按逐级一票否决规则判断目标服务器当前是否生效 80%/100% 超流告警。

    Args:
        global_traffic_enabled: 全局 ``traffic:enable`` 开关状态。
        server: 目标媒体服务器实例。

    Returns:
        有限配额流量生效且单服开启超流告警时返回 ``True``。
    """
    return bool(
        is_traffic_quota_limited(global_traffic_enabled, server)
        and server is not None
        and bool(server.enable_traffic_alert)
    )


def is_traffic_auto_suspend_effective(
    global_traffic_enabled: bool,
    server: ServerInstance | None,
) -> bool:
    """按逐级一票否决规则判断目标服务器当前是否生效超流自动停用。

    Args:
        global_traffic_enabled: 全局 ``traffic:enable`` 开关状态。
        server: 目标媒体服务器实例。

    Returns:
        有限配额流量生效且单服开启超流自动停用时返回 ``True``。
    """
    return bool(
        is_traffic_quota_limited(global_traffic_enabled, server)
        and server is not None
        and bool(server.enable_traffic_auto_suspend)
    )


def calculate_request_points_cost(
    server: ServerInstance | None,
    renew_score: int,
) -> int:
    """计算目标服务器当前单次求片所需积分。

    - ``fixed`` 模式：直接取 ``server.request_points_fixed``（最小 0）；
    - ``ratio`` 模式：按 ``int(renew_score * ratio)`` 计算，向下取整并允许为 0。

    Args:
        server: 目标媒体服务器实例。
        renew_score: 全局月续期基础积分（仅 ``ratio`` 模式使用）。

    Returns:
        非负整数求片积分单价；``0`` 表示免费求片。
    """
    if server is None:
        return 0
    if server.request_points_mode == "fixed":
        return max(0, int(server.request_points_fixed or 0))
    ratio = max(0.0, float(server.request_points_ratio or 0.0))
    return max(0, int(max(0, int(renew_score)) * ratio))


def evaluate_request_access(
    server: ServerInstance | None,
    *,
    cost: int,
    points_request_allowed: bool,
    points_request_reason: str,
) -> tuple[bool, str, str]:
    """判定目标服务器当前的求片路由/提交放行状态。

    当计算单价 ``cost == 0`` 时完全独立于积分开关运行；
    当 ``cost > 0`` 时受全局积分总开关及 ``points:enable_request`` 共同约束。

    Args:
        server: 目标媒体服务器实例。
        cost: 目标服务器本次计算所得求片积分价格。
        points_request_allowed: 积分求片功能是否生效（``enable_points && enable_points_request``）。
        points_request_reason: 积分求片未生效时的提示文案。

    Returns:
        ``(allowed, error_code, message)`` 三元组；放行时返回 ``(True, "", "")``。
    """
    if server is None:
        return False, "SERVER_NOT_FOUND", "服务器不存在"
    if not server.is_enabled:
        return False, "REQUEST_DISABLED", "该服务器已停用，暂不支持求片功能"
    if not server.allow_request:
        return False, "REQUEST_DISABLED", "该服务器未开启求片功能"
    if max(0, int(cost)) > 0 and not points_request_allowed:
        return False, "REQUEST_POINTS_DISABLED", points_request_reason
    return True, "", ""


def calculate_traffic_balances(
    server_quota_bytes: int | None,
    cycle_adjustment_bytes: int | None,
    cycle_used_bytes: int | None,
    pack_bytes: int | None,
) -> TrafficBalance:
    """统一计算单账号当期有效基础配额、基础池剩余、当期流量包剩余及合计可用池。

    当服务器原始配额 ``server_quota_bytes > 0`` 时，即便管理员负向调额使
    ``effective_base_quota_bytes == 0``，依然保持有限配额模式（``is_limited=True``），
    绝不会因调额归零而误判为不限流。

    Args:
        server_quota_bytes: 服务器月基础流量配额字节数（0 表示不限流）。
        cycle_adjustment_bytes: 管理员当期临时调额字节数（可正可负）。
        cycle_used_bytes: 当期已计入基础池的累计流量字节数。
        pack_bytes: 当期剩余可用流量包字节数。

    Returns:
        结构化的 ``TrafficBalance`` 对象。
    """
    quota = max(0, int(server_quota_bytes or 0))
    adjustment = int(cycle_adjustment_bytes or 0)
    used = max(0, int(cycle_used_bytes or 0))
    pack = max(0, int(pack_bytes or 0))
    is_limited = quota > 0
    if not is_limited:
        return TrafficBalance(
            is_limited=False,
            server_quota_bytes=0,
            adjustment_bytes=adjustment,
            effective_base_quota_bytes=0,
            cycle_used_bytes=used,
            pack_remaining_bytes=pack,
            base_remaining_bytes=None,
            total_remaining_bytes=None,
            total_pool_bytes=None,
        )
    effective_quota = max(0, quota + adjustment)
    base_remaining = max(0, effective_quota - used)
    total_remaining = base_remaining + pack
    total_pool = effective_quota + pack
    return TrafficBalance(
        is_limited=True,
        server_quota_bytes=quota,
        adjustment_bytes=adjustment,
        effective_base_quota_bytes=effective_quota,
        cycle_used_bytes=used,
        pack_remaining_bytes=pack,
        base_remaining_bytes=base_remaining,
        total_remaining_bytes=total_remaining,
        total_pool_bytes=total_pool,
    )


def calculate_traffic_pack_points_cost(
    server: ServerInstance | None,
    renew_score: int,
) -> int:
    """计算目标服务器单包流量兑换所需积分（fixed 与 ratio 模式均强制保底至少 1 积分）。

    Args:
        server: 目标媒体服务器实例。
        renew_score: 全局月续期基础积分（仅 ``ratio`` 模式使用）。

    Returns:
        至少为 1 的单包积分单价。
    """
    if server is None:
        return DEFAULT_TRAFFIC_PACK_COST_SCORE
    if server.traffic_pack_points_mode == "fixed":
        return max(
            1, int(server.traffic_pack_points_fixed or DEFAULT_TRAFFIC_PACK_COST_SCORE)
        )
    ratio = max(1e-6, float(server.traffic_pack_points_ratio or 0.5))
    return max(1, int(max(0, int(renew_score)) * ratio))


def calculate_traffic_pack_unit_bytes(server: ServerInstance | None) -> int:
    """计算目标服务器单包流量容量字节数（至少 1 字节）。"""
    raw_gib = float(
        (server.traffic_pack_size_gib if server is not None else None)
        or DEFAULT_TRAFFIC_PACK_SIZE_GIB
    )
    size_gib = raw_gib if raw_gib > 0 else DEFAULT_TRAFFIC_PACK_SIZE_GIB
    return max(1, round(size_gib * BYTES_PER_GIB))


def evaluate_traffic_pack_access(
    server: ServerInstance | None,
    *,
    points_pack_allowed: bool,
    points_pack_reason: str,
    global_traffic_enabled: bool,
) -> tuple[bool, str]:
    """按严格交集校验目标服务器当前是否允许购买流量包。

    必须同时满足：``积分开启且流量包买包子开关开启`` ∩ ``全局流量总开关开启`` ∩
    ``服务器启用 && 单服流量开启 && 月配额 > 0`` ∩ ``单服 allow_traffic_pack 开启``。

    Args:
        server: 目标媒体服务器实例。
        points_pack_allowed: 积分总开关与流量包子开关是否同时允许。
        points_pack_reason: 积分侧关闭时的拒因文案。
        global_traffic_enabled: 全局 ``traffic:enable`` 开关状态。

    Returns:
        ``(allowed, reason)`` 二元组。
    """
    if not points_pack_allowed:
        return False, points_pack_reason
    if server is None or not server.is_enabled:
        return False, "服务器不存在或已停用，暂不支持兑换流量包。"
    if not is_traffic_effective(global_traffic_enabled, server):
        return False, "当前服务器未启用流量限制，无需兑换流量包。"
    if max(0, int(server.traffic_quota_bytes or 0)) <= 0:
        return False, "当前服务器为不限流模式，无需兑换流量包。"
    if not server.allow_traffic_pack:
        return False, "该服务器未开启流量包购买功能。"
    return True, ""


def sync_traffic_alert_flags(media_user: MediaUser, bal: TrafficBalance) -> None:
    """依据最新有效配额与剩余余额就地刷新 80%/100% 配额预警去重标记。

    Args:
        media_user: 已持有账号锁的媒体账号 ORM 对象。
        bal: 按当前配额与流量包计算的 ``TrafficBalance`` 快照。

    副作用：当账号脱离不限流外的耗尽/预警线时清空对应预警标记；不执行数据库提交。
    """
    if not bal.is_limited:
        media_user.traffic_alert_80_sent = False
        media_user.traffic_alert_100_sent = False
        return
    total_remaining = bal.total_remaining_bytes or 0
    total_pool = bal.total_pool_bytes or 0
    if total_remaining > 0:
        media_user.traffic_alert_100_sent = False
    if total_pool > 0 and total_remaining * 5 > total_pool:
        media_user.traffic_alert_80_sent = False


class LuckyRewardCategory(StrEnum):
    """幸运签到奖励大类枚举"""

    POINTS = "points"
    CODE = "code"
    EXTEND = "extend"
    TRAFFIC = "traffic"


class LuckyRewardKind(StrEnum):
    """幸运签到精细化奖励档位标识枚举"""

    POINTS_DOUBLE = "points_double"
    POINTS_TRIPLE = "points_triple"
    POINTS_JACKPOT = "points_jackpot"
    CODE_SIGNUP = "code_signup"
    CODE_RENEW = "code_renew"
    EXTEND_1D = "extend_1d"
    EXTEND_3D = "extend_3d"
    EXTEND_7D = "extend_7d"
    EXTEND_15D = "extend_15d"
    EXTEND_30D = "extend_30d"
    TRAFFIC_SMALL = "traffic_small"
    TRAFFIC_MEDIUM = "traffic_medium"
    TRAFFIC_LARGE = "traffic_large"


@dataclass(frozen=True, slots=True)
class LuckyRewardSpec:
    """单个幸运签到奖励档位的结构化定义（权重、类别及发放参数）"""

    kind: LuckyRewardKind
    category: LuckyRewardCategory
    label: str
    weight: int
    points_multiplier: int = 0
    extend_days: int = 0
    code_type: Literal["signup", "renew"] | None = None
    traffic_pack_ratio: float = 0.0


LUCKY_REWARD_SPECS: tuple[LuckyRewardSpec, ...] = (
    LuckyRewardSpec(
        kind=LuckyRewardKind.POINTS_DOUBLE,
        category=LuckyRewardCategory.POINTS,
        label="双倍幸运积分",
        weight=42,
        points_multiplier=2,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.POINTS_TRIPLE,
        category=LuckyRewardCategory.POINTS,
        label="三倍暴击积分",
        weight=16,
        points_multiplier=3,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.POINTS_JACKPOT,
        category=LuckyRewardCategory.POINTS,
        label="超级积分福袋",
        weight=4,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.CODE_SIGNUP,
        category=LuckyRewardCategory.CODE,
        label="注册码大奖",
        weight=3,
        code_type="signup",
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.CODE_RENEW,
        category=LuckyRewardCategory.CODE,
        label="续期码大奖",
        weight=4,
        code_type="renew",
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.EXTEND_1D,
        category=LuckyRewardCategory.EXTEND,
        label="1天延寿",
        weight=12,
        extend_days=1,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.EXTEND_3D,
        category=LuckyRewardCategory.EXTEND,
        label="3天延寿",
        weight=8,
        extend_days=3,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.EXTEND_7D,
        category=LuckyRewardCategory.EXTEND,
        label="7天延寿",
        weight=5,
        extend_days=7,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.EXTEND_15D,
        category=LuckyRewardCategory.EXTEND,
        label="15天延寿",
        weight=2,
        extend_days=15,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.EXTEND_30D,
        category=LuckyRewardCategory.EXTEND,
        label="30天延寿",
        weight=1,
        extend_days=30,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.TRAFFIC_SMALL,
        category=LuckyRewardCategory.TRAFFIC,
        label="轻量当期流量包",
        weight=8,
        traffic_pack_ratio=0.2,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.TRAFFIC_MEDIUM,
        category=LuckyRewardCategory.TRAFFIC,
        label="标准当期流量包",
        weight=4,
        traffic_pack_ratio=0.5,
    ),
    LuckyRewardSpec(
        kind=LuckyRewardKind.TRAFFIC_LARGE,
        category=LuckyRewardCategory.TRAFFIC,
        label="豪华当期流量包",
        weight=2,
        traffic_pack_ratio=1.0,
    ),
)

LUCKY_REWARD_SPEC_MAP: dict[LuckyRewardKind, LuckyRewardSpec] = {
    spec.kind: spec for spec in LUCKY_REWARD_SPECS
}


@dataclass(frozen=True, slots=True)
class LuckyPointsBreakdown:
    """幸运签到积分奖励数值及明细拆解"""

    total: int
    base_roll: int
    multiplier: int
    base_total: int
    bonus: int


def build_eligible_lucky_rewards(
    *,
    has_signup_servers: bool,
    has_renew_code_servers: bool,
    has_extend_accounts: bool,
    has_traffic_accounts: bool,
) -> list[LuckyRewardSpec]:
    """按系统开关生效态与用户当前账号状态动态过滤幸运签到可用奖池。

    积分档位始终入池以保证奖池非空；注册码、续期码、账号延寿与当期流量包
    仅在对应前置条件成立时才进入抽奖池。

    Args:
        has_signup_servers: 是否存在用户尚未注册的可用 ``DEFAULT`` 媒体服务器。
        has_renew_code_servers: 用户是否持有至少一个可发放续期码的已连接媒体账号。
        has_extend_accounts: 用户是否持有至少一个支持直接延长有效期的有效媒体账号。
        has_traffic_accounts: 用户是否持有至少一个启用有限流量配额的有效媒体账号。

    Returns:
        满足当前资格条件的 ``LuckyRewardSpec`` 列表（至少包含全部积分档位）。
    """
    eligible: list[LuckyRewardSpec] = []
    for spec in LUCKY_REWARD_SPECS:
        if spec.category == LuckyRewardCategory.POINTS:
            eligible.append(spec)
        elif spec.kind == LuckyRewardKind.CODE_SIGNUP:
            if has_signup_servers:
                eligible.append(spec)
        elif spec.kind == LuckyRewardKind.CODE_RENEW:
            if has_renew_code_servers:
                eligible.append(spec)
        elif spec.category == LuckyRewardCategory.EXTEND:
            if has_extend_accounts:
                eligible.append(spec)
        elif spec.category == LuckyRewardCategory.TRAFFIC and has_traffic_accounts:
            eligible.append(spec)
    return eligible


def calculate_lucky_points_reward(
    spec: LuckyRewardSpec,
    *,
    checkin_min: int,
    checkin_max: int,
    consecutive_bonus: int,
    is_consecutive: bool,
    randint_fn: Callable[[int, int], int] = randint,
) -> LuckyPointsBreakdown:
    """根据后台配置的签到基础区间与连签奖励计算幸运签到积分奖励。

    Args:
        spec: 目标积分奖励档位定义（双倍、三倍或超级福袋）。
        checkin_min: 系统配置的每日签到最小基础积分。
        checkin_max: 系统配置的每日签到最大基础积分。
        consecutive_bonus: 系统配置的连续签到额外奖励积分。
        is_consecutive: 本次是否处于连续签到周期。
        randint_fn: 随机整数生成函数（支持注入以方便确定性测试）。

    Returns:
        包含总积分、基础随机值、倍率及连签加成的 ``LuckyPointsBreakdown``。
    """
    low = max(0, int(checkin_min))
    high = max(low, int(checkin_max))
    bonus = max(0, int(consecutive_bonus)) if is_consecutive else 0

    if spec.kind == LuckyRewardKind.POINTS_JACKPOT:
        # 超级福袋严格高于三倍暴击上限（3 * high），按配置的上限 4x-6x 浮动
        base_total = randint_fn(high * 4, high * 6)
        return LuckyPointsBreakdown(
            total=base_total + bonus,
            base_roll=base_total,
            multiplier=0,
            base_total=base_total,
            bonus=bonus,
        )

    multiplier = max(1, int(spec.points_multiplier))
    roll = randint_fn(low, high)
    base_total = roll * multiplier
    return LuckyPointsBreakdown(
        total=base_total + bonus,
        base_roll=roll,
        multiplier=multiplier,
        base_total=base_total,
        bonus=bonus,
    )


def select_priority_bucket[T](
    items: Sequence[T], tier_fn: Callable[[T], int]
) -> list[T]:
    """从候选列表中提取优先级数值最小（即最高优先级）的一组元素。

    Args:
        items: 候选对象序列。
        tier_fn: 将单个候选对象映射为整型优先级（越小越优先）的函数。

    Returns:
        全部处于最高优先级梯队的候选对象列表；输入为空时返回空列表。
    """
    if not items:
        return []
    paired = [(item, tier_fn(item)) for item in items]
    best_tier = min(tier for _, tier in paired)
    return [item for item, tier in paired if tier == best_tier]


def expiry_target_tier(
    expires_at: datetime,
    is_banned: bool,
    *,
    now: datetime,
    advance_days: int,
) -> int:
    """评估延寿/续期码目标账号的紧急程度梯队（0=已过期/封禁，1=临近到期，2=常规有效）。"""
    if is_banned or expires_at <= now:
        return 0
    soon_window = timedelta(days=max(7, int(advance_days)))
    if expires_at <= now + soon_window:
        return 1
    return 2


def traffic_target_tier(is_traffic_suspended: bool, bal: TrafficBalance) -> int:
    """评估当期流量包奖励目标账号的需求梯队（0=已停用/耗尽，1=高占用>=80%，2=中占用>=50%，3=其他）。"""
    if is_traffic_suspended or (
        bal.total_remaining_bytes is not None and bal.total_remaining_bytes <= 0
    ):
        return 0
    if bal.total_pool_bytes and bal.total_remaining_bytes is not None:
        if bal.total_remaining_bytes * 5 <= bal.total_pool_bytes:
            return 1
        if bal.total_remaining_bytes * 2 <= bal.total_pool_bytes:
            return 2
    return 3
