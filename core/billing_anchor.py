import calendar
from dataclasses import dataclass
from datetime import UTC, datetime

from core.time_utils import ensure_utc, to_local, to_utc, utc_now


@dataclass(frozen=True, slots=True)
class BillingCycleResult:
    """锚定自然月周期计算结果

    Attributes:
        anchor_day: 锚定基准日号数（1..31）
        cycle_started_at: 当前周期起始时间
        next_reset_at: 下一次月度重置时间
        expires_at: 计算后的账号到期时间
        is_reset: 是否发生锚定基准日重置（新注册或过期补续为 True，未过期续期为 False）
    """

    anchor_day: int
    cycle_started_at: datetime
    next_reset_at: datetime
    expires_at: datetime
    is_reset: bool


def _validate_anchor_day(anchor_day: int) -> int:
    """校验锚定日号数范围并返回合法整数"""
    if anchor_day < 1 or anchor_day > 31:
        raise ValueError(f"anchor_day 必须在 1 到 31 之间，当前为 {anchor_day}")
    return anchor_day


def _resolve_anchor_day(anchor_day: int | None, expires_at: datetime) -> int:
    """解析媒体账号有效锚定日；存量记录缺少 ``anchor_day`` 时按业务时区到期日号回退"""
    if anchor_day is not None:
        return _validate_anchor_day(anchor_day)
    return to_local(expires_at).day


def _validate_months(months: int) -> int:
    """校验自然月跨度并返回合法整数"""
    if months < 1:
        raise ValueError(f"expiry_months 必须为大于等于 1 的整数，当前为 {months}")
    return months


def _shift_anchored_months(
    base: datetime, anchor_day: int, month_offset: int
) -> datetime:
    """将基准时间在配置业务时区下按锚定日号数偏移指定自然月数，月末不足时自动钳位并还原为 UTC。

    Args:
        base: 基准时间点（支持 UTC 或带/不带时区时间）
        anchor_day: 原始锚定日号数（1..31）
        month_offset: 偏移自然月数（可正可负或为 0）

    Returns:
        目标年月按 ``min(anchor_day, 当月总天数)`` 落日后的 UTC aware 时间
    """
    _validate_anchor_day(anchor_day)
    local_base = to_local(base)
    total_months = local_base.year * 12 + (local_base.month - 1) + month_offset
    target_year, target_month_zero = divmod(total_months, 12)
    target_month = target_month_zero + 1
    max_day = calendar.monthrange(target_year, target_month)[1]
    target_day = min(anchor_day, max_day)
    shifted_local = datetime(
        target_year,
        target_month,
        target_day,
        local_base.hour,
        local_base.minute,
        local_base.second,
        local_base.microsecond,
        tzinfo=local_base.tzinfo,
    )
    return shifted_local.astimezone(UTC)


def advance_anchored_months(base: datetime, anchor_day: int, months: int) -> datetime:
    """从基准时间按锚定日向后推进指定自然月数

    遇到大小月天数差异时，自动钳位至目标月份最后一天；后续再次推进到大月时，
    因使用原始 ``anchor_day`` 计算，仍会准确恢复原锚定日期而不发生永久漂移。

    Args:
        base: 推进起点时间
        anchor_day: 锚定基准日号数（1..31）
        months: 推进的自然月数（必须 >= 1）

    Returns:
        推进后的到期或重置时间点
    """
    _validate_months(months)
    return _shift_anchored_months(base, anchor_day, months)


def resolve_anchored_cycle_window(
    now: datetime, anchor_day: int, reference_at: datetime
) -> tuple[datetime, datetime]:
    """根据锚定日与参考时刻推算包含当前时间 ``now`` 的单月周期边界 ``[start, end)``

    Args:
        now: 当前时间点
        anchor_day: 锚定基准日号数（1..31）
        reference_at: 用于对齐时分秒的参考时间点

    Returns:
        ``(cycle_started_at, next_reset_at)`` 元组，满足 ``cycle_started_at <= now < next_reset_at``
    """
    _validate_anchor_day(anchor_day)
    # 在配置业务时区下构造当月锚定边界，再转换回 UTC 比较并返回
    local_now_dt = to_local(now)
    local_ref_dt = to_local(reference_at)
    month_head = datetime(
        local_now_dt.year,
        local_now_dt.month,
        1,
        local_ref_dt.hour,
        local_ref_dt.minute,
        local_ref_dt.second,
        local_ref_dt.microsecond,
        tzinfo=local_now_dt.tzinfo,
    )
    candidate = _shift_anchored_months(month_head, anchor_day, 0)
    utc_now_dt = to_utc(now)
    if candidate <= utc_now_dt:
        return candidate, _shift_anchored_months(candidate, anchor_day, 1)
    return _shift_anchored_months(candidate, anchor_day, -1), candidate


def calculate_registration_cycle(
    *, months: int = 1, now: datetime | None = None
) -> BillingCycleResult:
    """计算新注册媒体账号的初始锚定周期

    Args:
        months: 服务器配置的有效自然月数（>= 1）
        now: 可注入的当前时间（默认使用 UTC 当前时间）

    Returns:
        新账号的初始 ``BillingCycleResult``（时间字段均为 UTC aware）
    """
    _validate_months(months)
    current_now = to_utc(now) if now is not None else utc_now()
    anchor_day = to_local(current_now).day
    return BillingCycleResult(
        anchor_day=anchor_day,
        cycle_started_at=current_now,
        next_reset_at=advance_anchored_months(current_now, anchor_day, 1),
        expires_at=advance_anchored_months(current_now, anchor_day, months),
        is_reset=True,
    )


def calculate_renewal_cycle(
    *,
    current_expires_at: datetime,
    anchor_day: int | None = None,
    current_cycle_started_at: datetime | None = None,
    current_next_reset_at: datetime | None = None,
    months: int = 1,
    now: datetime | None = None,
) -> BillingCycleResult:
    """计算续期后的锚定自然月周期

    未过期提前续期（``current_expires_at >= now``）保留原 ``anchor_day`` 并从原到期日顺推；
    已过期进入宽限期后补续（``current_expires_at < now``）则以补续当天重置 ``anchor_day``
    并重新起算周期。

    Args:
        current_expires_at: 当前账号到期时间
        anchor_day: 现有锚定日（存量账号为空时自动回退至 ``current_expires_at.day``）
        current_cycle_started_at: 当前周期起始时间
        current_next_reset_at: 下次月度重置时间
        months: 续期自然月数（>= 1）
        now: 可注入的当前时间（默认使用系统当前时间）

    Returns:
        续期后的 ``BillingCycleResult``
    """
    _validate_months(months)
    current_now = to_utc(now) if now is not None else utc_now()
    expires_utc = to_utc(current_expires_at)
    cycle_start_utc = ensure_utc(current_cycle_started_at)
    next_reset_utc = ensure_utc(current_next_reset_at)

    if expires_utc < current_now:
        return calculate_registration_cycle(months=months, now=current_now)

    resolved_anchor_day = _resolve_anchor_day(anchor_day, expires_utc)
    new_expires_at = advance_anchored_months(expires_utc, resolved_anchor_day, months)

    # 未过期续期时优先保留尚未到点的进行中月度重置窗口，避免误吞或跳过当前账期重置
    if (
        cycle_start_utc is not None
        and next_reset_utc is not None
        and cycle_start_utc <= current_now < next_reset_utc
    ):
        cycle_started_at = cycle_start_utc
        next_reset_at = next_reset_utc
    else:
        cycle_started_at, next_reset_at = resolve_anchored_cycle_window(
            current_now, resolved_anchor_day, expires_utc
        )

    return BillingCycleResult(
        anchor_day=resolved_anchor_day,
        cycle_started_at=cycle_started_at,
        next_reset_at=next_reset_at,
        expires_at=new_expires_at,
        is_reset=False,
    )


def backfill_billing_cycle(
    *,
    current_expires_at: datetime,
    anchor_day: int | None = None,
    current_cycle_started_at: datetime | None = None,
    current_next_reset_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[int, datetime, datetime]:
    """为存量或缺失锚定字段的媒体账号平滑推导 ``(anchor_day, cycle_started_at, next_reset_at)``

    Args:
        current_expires_at: 账号现有到期时间
        anchor_day: 已有锚定日（为空时按现有到期时间在业务时区的日号回填）
        current_cycle_started_at: 已有周期起点（可选）
        current_next_reset_at: 已有下次重置时间（可选）
        now: 可注入的当前时间（默认使用 UTC 当前时间）

    Returns:
        ``(anchor_day, cycle_started_at, next_reset_at)`` 元组（时间均为 UTC aware）
    """
    current_now = to_utc(now) if now is not None else utc_now()
    expires_utc = to_utc(current_expires_at)
    cycle_start_utc = ensure_utc(current_cycle_started_at)
    next_reset_utc = ensure_utc(current_next_reset_at)
    resolved_anchor_day = _resolve_anchor_day(anchor_day, expires_utc)
    if cycle_start_utc is not None and next_reset_utc is not None:
        return resolved_anchor_day, cycle_start_utc, next_reset_utc

    derived_start, derived_end = resolve_anchored_cycle_window(
        current_now, resolved_anchor_day, expires_utc
    )
    return (
        resolved_anchor_day,
        derived_start,
        derived_end,
    )
