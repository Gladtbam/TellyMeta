import contextlib
import inspect
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from random import choice, choices, randint
from typing import Any, Literal

from fastapi import FastAPI
from httpx2 import HTTPError, HTTPStatusError
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.billing_anchor import backfill_billing_cycle
from core.config import get_settings
from core.economy_policy import (
    DEFAULT_TRAFFIC_PACK_COST_SCORE,
    DEFAULT_TRAFFIC_PACK_SIZE_GIB,
    LUCKY_REWARD_SPEC_MAP,
    LuckyRewardCategory,
    LuckyRewardKind,
    LuckyRewardSpec,
    TrafficBalance,
    build_eligible_lucky_rewards,
    calculate_lucky_points_reward,
    calculate_request_points_cost,
    calculate_traffic_balances,
    calculate_traffic_pack_points_cost,
    calculate_traffic_pack_unit_bytes,
    evaluate_request_access,
    evaluate_traffic_pack_access,
    expiry_target_tier,
    is_auto_renew_open,
    is_traffic_effective,
    is_traffic_quota_limited,
    is_watch_time_effective,
    select_priority_bucket,
    sync_traffic_alert_flags,
    traffic_target_tier,
)
from core.media_utils import get_account_lock
from core.scheduler_jobs import (
    clear_traffic_unsuspend_job,
    remove_account_lifecycle_jobs,
    schedule_account_lifecycle_jobs,
    schedule_traffic_unsuspend_retry,
)
from core.telegram_message import TelegramMessage
from models.nginx import format_traffic_bytes
from models.orm import RegistrationMode, ServerInstance, ServerType, TelegramUser
from repositories.code_repo import CodeRepository
from repositories.config_repo import (
    DEFAULT_TRAFFIC_PACK_UNIT_BYTES,
    ConfigRepository,
)
from repositories.media_repo import MediaRepository
from repositories.server_repo import ServerRepository
from repositories.telegram_repo import TelegramRepository
from services.media_service import (
    MediaService,
    compute_external_disabled_state,
    sync_external_disabled_state,
)
from services.traffic_service import flush_app_traffic
from services.watch_time_service import evaluate_watch_time_renewal

settings = get_settings()


@dataclass
class Result:
    """封装服务方法的结果，用于返回给 Handler 处理器。"""

    success: bool
    message: str = ""
    private_message: str | int | None = None
    keyboard: Any | None = None
    extra_data: Any | None = None


@dataclass(slots=True)
class _LuckyCheckinContext:
    """单次幸运签到上下文快照（含纯标量标识及过滤后的候选服务器/账号）"""

    user_id: int
    username: str | Literal[False] | None
    is_consecutive: bool
    points_cfg: dict[str, Any]
    signup_servers: list[ServerInstance]
    renew_code_targets: list[tuple[int, int, datetime, bool, str]]
    extend_targets: list[tuple[int, int, datetime, bool, str]]
    traffic_targets: list[tuple[int, int, bool, TrafficBalance, str]]
    now: datetime
    renew_advance_days: int


class UserService:
    def __init__(self, app: FastAPI, session: AsyncSession) -> None:
        """初始化用户服务及关联的数据仓储、媒体服务器客户端映射。

        Args:
            app: FastAPI 应用实例，用于读取运行期媒体客户端与调度器引用。
            session: 当前异步数据库会话。
        """
        self.app = app
        self.session = session
        self.telegram_repo = TelegramRepository(session)
        self.config_repo = ConfigRepository(session)
        self.media_repo = MediaRepository(session)
        self.code_repo = CodeRepository(session)
        self.server_repo = ServerRepository(session)
        self.media_clients: dict[int, MediaService] = (
            getattr(getattr(app, "state", None), "media_clients", None) or {}
        )

    async def perform_checkin(
        self, user_id: int, username: str | Literal[False] | None
    ) -> Result:
        """执行用户签到逻辑，并在入口统一校验积分总开关与签到子开关。

        Args:
            user_id: Telegram 用户 ID。
            username: Telegram 显示名称或用户名。

        Returns:
            包含成功标志与提示消息的 Result 对象。
        """
        # 防止直调服务绕过 Bot 指令守卫触发常规或幸运签到奖励
        allowed, reason = await self.config_repo.check_points_feature(
            ConfigRepository.KEY_ENABLE_POINTS_CHECKIN
        )
        if not allowed:
            return Result(success=False, message=reason)

        user = await self.telegram_repo.get_or_create(user_id)

        today = date.today()
        last_date = user.last_checkin.date()

        if user.last_checkin:
            if last_date == today:
                return Result(
                    success=False,
                    message=f"[您](tg://user?id={user_id})今天已经签到过了，请明天再来！",
                )

            is_consecutive = last_date == (today - timedelta(days=1))
        else:
            is_consecutive = False
        points_cfg = await self.config_repo.get_points_config()
        bonus = (
            int(points_cfg["points_checkin_consecutive_bonus"]) if is_consecutive else 0
        )

        consecutive_checkin_count = (
            (user.consecutive_checkin_count + 1) if is_consecutive else 1
        )
        lucky_interval = int(points_cfg["points_checkin_lucky_interval"])
        is_lucky = (
            lucky_interval > 0 and consecutive_checkin_count % lucky_interval == 0
        )

        if not is_lucky:
            checkin_min = int(points_cfg["points_checkin_min"])
            checkin_max = max(checkin_min, int(points_cfg["points_checkin_max"]))
            base_score = randint(checkin_min, checkin_max)
            final_score = base_score + bonus

            update_user = await self.telegram_repo.update_checkin(
                user_id, final_score, is_consecutive=is_consecutive
            )
            if update_user:
                msg = TelegramMessage.render_markdown(
                    t"✅ 签到成功！[{username}](tg://user?id={user_id})获得了 **{final_score}** 积分。"
                )
                if is_consecutive:
                    msg += f"\n(基础 {base_score} + 连签 {bonus})"
                return Result(success=True, message=msg)
            return Result(success=False, message="签到失败，请稍后再试。")
        return await self._perform_lucky_checkin(
            user, username, is_consecutive, points_cfg=points_cfg
        )

    async def _collect_lucky_checkin_context(
        self,
        *,
        user_id: int,
        username: str | Literal[False] | None,
        is_consecutive: bool,
        target_pairs: list[tuple[int, int]],
        points_cfg: dict[str, Any],
    ) -> _LuckyCheckinContext:
        """收集幸运签到抽奖所需的服务器能力状态与用户各账号资格快照。

        Args:
            user_id: Telegram 用户 ID。
            username: 用于展示的用户名或显示名。
            is_consecutive: 本次是否处于连续签到周期。
            target_pairs: 提前提取的用户媒体账号主键对列表 ``[(user_id, server_id), ...]``。
            points_cfg: 积分与签到规则配置字典。

        Returns:
            过滤与刷新后供奖池构建及奖励发放使用的 ``_LuckyCheckinContext``。
        """
        now = datetime.now()
        enabled_servers = await self.server_repo.get_all_enabled()
        enabled_media_servers: dict[int, ServerInstance] = {
            int(s.id): s
            for s in enabled_servers
            if s.id is not None
            and s.server_type in (ServerType.EMBY, ServerType.JELLYFIN)
        }
        registered_server_ids = {sid for _, sid in target_pairs}
        # 注册码仅面向用户尚未注册且处于 DEFAULT（积分/激活码）模式的已连接媒体服务器
        signup_servers = [
            srv
            for sid, srv in enabled_media_servers.items()
            if sid in self.media_clients
            and srv.registration_mode == RegistrationMode.DEFAULT
            and sid not in registered_server_ids
        ]

        global_traffic_on = await self.config_repo.is_traffic_enabled()
        policy_cfg = await self.config_repo.get_policy_config()
        renew_advance_days = int(policy_cfg["account_renew_advance_days"])

        has_potential_traffic = any(
            is_traffic_quota_limited(global_traffic_on, enabled_media_servers.get(sid))
            for _, sid in target_pairs
        )
        # 在评估账号流量余额及占用梯队前先刷盘内存缓冲，确保用量判断准确
        if has_potential_traffic:
            await flush_app_traffic(self.app)

        renew_code_targets: list[tuple[int, int, datetime, bool, str]] = []
        extend_targets: list[tuple[int, int, datetime, bool, str]] = []
        traffic_targets: list[tuple[int, int, bool, TrafficBalance, str]] = []

        for uid, sid in target_pairs:
            srv = enabled_media_servers.get(sid)
            if srv is None:
                continue
            mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
            if mu is None:
                continue
            srv_name = str(srv.name)
            if sid in self.media_clients:
                renew_code_targets.append(
                    (uid, sid, mu.expires_at, bool(mu.is_banned), srv_name)
                )
            # 若账号仅因过期封禁且需外部解封，必须保证所属服务器客户端已连接才纳入直接延寿候选
            needs_external_unban = bool(
                mu.is_banned or mu.delete_at is not None
            ) and not bool(mu.is_traffic_suspended)
            if not needs_external_unban or sid in self.media_clients:
                extend_targets.append(
                    (uid, sid, mu.expires_at, bool(mu.is_banned), srv_name)
                )
            if is_traffic_quota_limited(global_traffic_on, srv):
                bal = calculate_traffic_balances(
                    srv.traffic_quota_bytes,
                    mu.cycle_traffic_adjustment_bytes,
                    mu.cycle_traffic_bytes,
                    mu.traffic_pack_bytes,
                )
                traffic_targets.append(
                    (uid, sid, bool(mu.is_traffic_suspended), bal, srv_name)
                )

        return _LuckyCheckinContext(
            user_id=user_id,
            username=username,
            is_consecutive=is_consecutive,
            points_cfg=points_cfg,
            signup_servers=signup_servers,
            renew_code_targets=renew_code_targets,
            extend_targets=extend_targets,
            traffic_targets=traffic_targets,
            now=now,
            renew_advance_days=renew_advance_days,
        )

    async def _grant_lucky_points_reward(
        self, ctx: _LuckyCheckinContext, spec: LuckyRewardSpec
    ) -> Result:
        """发放幸运签到多档位积分奖励（读取配置的签到区间与连签奖励）。"""
        breakdown = calculate_lucky_points_reward(
            spec,
            checkin_min=int(ctx.points_cfg["points_checkin_min"]),
            checkin_max=int(ctx.points_cfg["points_checkin_max"]),
            consecutive_bonus=int(ctx.points_cfg["points_checkin_consecutive_bonus"]),
            is_consecutive=ctx.is_consecutive,
            randint_fn=randint,
        )
        await self.telegram_repo.update_checkin(
            ctx.user_id, breakdown.total, is_consecutive=ctx.is_consecutive
        )
        header_map = {
            LuckyRewardKind.POINTS_DOUBLE: "🎉 **幸运签到·积分翻倍！**",
            LuckyRewardKind.POINTS_TRIPLE: "🔥 **幸运签到·三倍暴击！**",
            LuckyRewardKind.POINTS_JACKPOT: "👑 **幸运签到·超级积分福袋！**",
        }
        header = header_map.get(spec.kind, "🎉 **幸运签到！**")
        uid = ctx.user_id
        uname = ctx.username
        msg = TelegramMessage.render_markdown(
            t"{header:raw} [{uname}](tg://user?id={uid})获得了 **{breakdown.total}** 积分。"
        )
        detail = (
            f"奖池 {breakdown.base_total}"
            if spec.kind == LuckyRewardKind.POINTS_JACKPOT
            else f"基础 {breakdown.base_roll} × {breakdown.multiplier}"
        )
        if ctx.is_consecutive and breakdown.bonus > 0:
            detail += f" + 连签 {breakdown.bonus}"
        msg += f"\n({detail})"
        return Result(success=True, message=msg)

    async def _grant_lucky_code_reward(
        self, ctx: _LuckyCheckinContext, spec: LuckyRewardSpec
    ) -> Result | None:
        """发放独立区分的注册码或续期码奖励；无可用目标服务器时返回 ``None`` 交由上层兜底。"""
        if spec.code_type == "signup":
            if not ctx.signup_servers:
                return None
            target_server = choice(ctx.signup_servers)
            code_type: Literal["signup", "renew"] = "signup"
            title = "注册码大奖"
            code_name = "注册码"
        else:
            if not ctx.renew_code_targets:
                return None
            bucket = select_priority_bucket(
                ctx.renew_code_targets,
                lambda item: expiry_target_tier(
                    item[2], item[3], now=ctx.now, advance_days=ctx.renew_advance_days
                ),
            )
            chosen_sid = choice(bucket)[1]
            target_server = await self.server_repo.get_by_id(chosen_sid, fresh=True)
            if target_server is None or not target_server.is_enabled:
                return None
            code_type = "renew"
            title = "续期码大奖"
            code_name = "续期码"

        if target_server.id is None:
            return None

        server_name = str(target_server.name)
        code = await self.code_repo.create(
            code_type, target_server.code_expiry_days, target_server.id
        )
        await self.telegram_repo.update_checkin(
            ctx.user_id, 0, is_consecutive=ctx.is_consecutive
        )
        uid = ctx.user_id
        uname = ctx.username
        exp_str = code.expires_at.strftime("%Y-%m-%d")
        return Result(
            success=True,
            message=TelegramMessage.render_markdown(
                t"🎉 **幸运签到·{title}！** [{uname}](tg://user?id={uid})抽中 **{server_name}** 的 **{code_name}**，已通过私信发送给您。"
            ),
            private_message=TelegramMessage.render_markdown(t"""\
                🎁 幸运签到·{code_name}奖励！
                服务器: {server_name}
                类型: {code_name}
                代码: `{code.code}`
                有效期至: {exp_str}
                请妥善保管！"""),
        )

    async def _grant_lucky_extend_reward(
        self, ctx: _LuckyCheckinContext, spec: LuckyRewardSpec
    ) -> Result | None:
        """随机选取 1 个媒体账号（优先快到期/已过期）直接延长有效期并遵守 ADR-0002。"""
        remaining = list(ctx.extend_targets)
        while remaining:
            bucket = select_priority_bucket(
                remaining,
                lambda item: expiry_target_tier(
                    item[2], item[3], now=ctx.now, advance_days=ctx.renew_advance_days
                ),
            )
            picked = choice(bucket)
            remaining.remove(picked)
            uid, sid, _, _, _ = picked

            async with get_account_lock(uid, sid):
                await self.media_repo.end_clean_read_tx()
                fresh_mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
                fresh_srv = await self.server_repo.get_by_id(sid, fresh=True)
                if fresh_mu is None or fresh_srv is None or not fresh_srv.is_enabled:
                    continue

                server_name = str(fresh_srv.name)
                # 仅当清除过期封禁且账号不同时处于超流停用时才解封外部账户（遵循 ADR-0002）
                keep_disabled = compute_external_disabled_state(
                    is_banned=False,
                    is_traffic_suspended=bool(fresh_mu.is_traffic_suspended),
                )
                if (
                    fresh_mu.is_banned or fresh_mu.delete_at is not None
                ) and not keep_disabled:
                    outcome = await sync_external_disabled_state(
                        self.media_clients.get(sid),
                        media_id=fresh_mu.media_id,
                        target_disabled=False,
                        log_context="签到续期恢复媒体账号",
                        user_id=uid,
                        server_id=sid,
                    )
                    if not outcome.ok:
                        continue

                updated_mu = await self.media_repo.extend_expiry(
                    fresh_mu, spec.extend_days
                )
                schedule_account_lifecycle_jobs(
                    getattr(getattr(self.app, "state", None), "scheduler", None),
                    updated_mu,
                )
                exp_str = updated_mu.expires_at.strftime("%Y-%m-%d %H:%M")
                is_traffic_suspended = bool(updated_mu.is_traffic_suspended)

            await self.telegram_repo.update_checkin(
                ctx.user_id, 0, is_consecutive=ctx.is_consecutive
            )
            uname = ctx.username
            msg = TelegramMessage.render_markdown(
                t"🎉 **幸运签到·{spec.label}！** [{uname}](tg://user?id={uid})的媒体账号 ({server_name}) 已自动延长 **{spec.extend_days}** 天有效期至 `{exp_str}`！"
            )
            if is_traffic_suspended:
                msg += "\n（提示：该账号当前仍处于超流停用状态，恢复流量额度后即可正常播放）"
            return Result(success=True, message=msg)
        return None

    async def _grant_lucky_traffic_reward(
        self, ctx: _LuckyCheckinContext, spec: LuckyRewardSpec
    ) -> Result | None:
        """随机选取 1 个有限流媒体账号（优先已停用/高占用）发放当期流量包并安全解封。"""
        remaining = list(ctx.traffic_targets)
        scheduler = getattr(getattr(self.app, "state", None), "scheduler", None)
        while remaining:
            bucket = select_priority_bucket(
                remaining,
                lambda item: traffic_target_tier(item[2], item[3]),
            )
            picked = choice(bucket)
            remaining.remove(picked)
            uid, sid, _, _, _ = picked

            async with get_account_lock(uid, sid):
                await self.media_repo.end_clean_read_tx()
                fresh_mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
                fresh_srv = await self.server_repo.get_by_id(sid, fresh=True)
                global_traffic_on = await self.config_repo.is_traffic_enabled()
                if fresh_mu is None or not is_traffic_quota_limited(
                    global_traffic_on, fresh_srv
                ):
                    continue
                assert fresh_srv is not None

                unit_bytes = calculate_traffic_pack_unit_bytes(fresh_srv)
                added_bytes = max(1, round(unit_bytes * spec.traffic_pack_ratio))
                fresh_mu.traffic_pack_bytes = (
                    max(0, int(fresh_mu.traffic_pack_bytes or 0)) + added_bytes
                )
                bal = calculate_traffic_balances(
                    fresh_srv.traffic_quota_bytes,
                    fresh_mu.cycle_traffic_adjustment_bytes,
                    fresh_mu.cycle_traffic_bytes,
                    fresh_mu.traffic_pack_bytes,
                )
                sync_traffic_alert_flags(fresh_mu, bal)

                was_suspended = bool(fresh_mu.is_traffic_suspended)
                still_banned = bool(fresh_mu.is_banned)
                unsuspended = False
                retry_scheduled = False
                if was_suspended and (bal.total_remaining_bytes or 0) > 0:
                    target_disabled = compute_external_disabled_state(
                        is_banned=still_banned,
                        is_traffic_suspended=False,
                    )
                    if not target_disabled:
                        outcome = await sync_external_disabled_state(
                            self.media_clients.get(sid),
                            media_id=fresh_mu.media_id,
                            target_disabled=False,
                            treat_404_as_synced=True,
                            log_context="幸运签到流量包解除超流停用",
                            user_id=uid,
                            server_id=sid,
                        )
                        if outcome.ok:
                            fresh_mu.is_traffic_suspended = False
                            clear_traffic_unsuspend_job(scheduler, uid, sid)
                            unsuspended = True
                        else:
                            retry_scheduled = True
                            schedule_traffic_unsuspend_retry(
                                scheduler,
                                uid,
                                sid,
                                run_date=datetime.now() + timedelta(minutes=15),
                            )
                    else:
                        # 账号同时处于过期封禁时仅清除超流标记，严禁外部解封（ADR-0002）
                        fresh_mu.is_traffic_suspended = False
                        clear_traffic_unsuspend_job(scheduler, uid, sid)

                try:
                    await self.media_repo.session.commit()
                    await self.media_repo.session.refresh(fresh_mu)
                except SQLAlchemyError as exc:
                    logger.warning(
                        "幸运签到发放当期流量包提交失败，尝试回退其他候选: user_id={}, server_id={}, err={}",
                        uid,
                        sid,
                        exc,
                    )
                    await self.media_repo.session.rollback()
                    continue

                server_name = str(fresh_srv.name)
                pack_total_bytes = int(fresh_mu.traffic_pack_bytes)
                next_reset_at = fresh_mu.next_reset_at

            await self.telegram_repo.update_checkin(
                ctx.user_id, 0, is_consecutive=ctx.is_consecutive
            )
            added_str = format_traffic_bytes(added_bytes)
            pack_total_str = format_traffic_bytes(pack_total_bytes)
            reset_clause = (
                f"将于 {next_reset_at.strftime('%Y-%m-%d %H:%M')} 账期重置时清零"
                if next_reset_at is not None
                else "将于本周期重置时清零"
            )
            uname = ctx.username
            msg = TelegramMessage.render_markdown(
                t"🎉 **幸运签到·{spec.label}！** [{uname}](tg://user?id={uid})的媒体账号 ({server_name}) 获得 **{added_str}** 当期流量包（当前流量包余额 {pack_total_str}，{reset_clause}）！"
            )
            if unsuspended:
                msg += "\n✅ 已为您自动解除超流停用，播放权限已恢复！"
            elif still_banned:
                msg += "\n（提示：该账号仍处于过期封禁状态，续期后即可恢复播放）"
            elif retry_scheduled:
                msg += (
                    "\n（提示：外部解封暂时失败，已登记自动重试任务）"
                    if scheduler is not None
                    else "\n（提示：外部解封暂时失败，请稍后重试或联系管理员处理）"
                )
            return Result(success=True, message=msg)
        return None

    async def _perform_lucky_checkin(
        self,
        user: TelegramUser,
        username: str | Literal[False] | None,
        is_consecutive: bool,
        *,
        points_cfg: dict[str, Any] | None = None,
    ) -> Result:
        """执行幸运签到动态抽奖，区分并按资格过滤多档积分、注册/续期码、账号延寿与当期流量包。

        基于全局开关、服务器生效态与用户当前账号绑定情况动态构建奖池；
        多服场景下按优先级梯队随机选取 1 个目标媒体账号发放延寿或当期流量包，
        并严格遵守 ADR-0002（超流停用与过期封禁隔离）及 ADR-0004（流量包当期清零）。

        Args:
            user: 当前签到的 Telegram 用户 ORM 对象。
            username: 用于展示的用户名或显示名；无用户名时为 ``None``/``False``。
            is_consecutive: 本次签到是否计入连续签到周期。
            points_cfg: 可选的已加载积分配置字典；缺省时自动从仓储读取。

        Returns:
            包含签到群消息文案及可选激活码私聊文案的 ``Result`` 对象。
        """
        # 提前提取纯标量 ID 与关联账号主键，避免后续刷新/提交事务导致 ORM 属性过期
        user_id = int(user.id)
        target_pairs = [
            (int(mu.id), int(mu.server_id)) for mu in (user.media_users or [])
        ]
        resolved_cfg = points_cfg or await self.config_repo.get_points_config()
        ctx = await self._collect_lucky_checkin_context(
            user_id=user_id,
            username=username,
            is_consecutive=is_consecutive,
            target_pairs=target_pairs,
            points_cfg=resolved_cfg,
        )
        eligible_specs = build_eligible_lucky_rewards(
            has_signup_servers=bool(ctx.signup_servers),
            has_renew_code_servers=bool(ctx.renew_code_targets),
            has_extend_accounts=bool(ctx.extend_targets),
            has_traffic_accounts=bool(ctx.traffic_targets),
        )
        candidate_kinds = [spec.kind for spec in eligible_specs]
        candidate_weights = [spec.weight for spec in eligible_specs]
        raw_choice = choices(candidate_kinds, candidate_weights)[0]
        try:
            chosen_kind = LuckyRewardKind(raw_choice)
        except ValueError:
            chosen_kind = LuckyRewardKind.POINTS_DOUBLE
        chosen_spec = LUCKY_REWARD_SPEC_MAP[chosen_kind]

        result: Result | None = None
        if chosen_spec.category == LuckyRewardCategory.CODE:
            result = await self._grant_lucky_code_reward(ctx, chosen_spec)
        elif chosen_spec.category == LuckyRewardCategory.EXTEND:
            result = await self._grant_lucky_extend_reward(ctx, chosen_spec)
        elif chosen_spec.category == LuckyRewardCategory.TRAFFIC:
            result = await self._grant_lucky_traffic_reward(ctx, chosen_spec)
        elif chosen_spec.category == LuckyRewardCategory.POINTS:
            result = await self._grant_lucky_points_reward(ctx, chosen_spec)

        if result is not None:
            return result
        # 并发竞态导致选中奖品已失效时，安全兜底为双倍积分
        return await self._grant_lucky_points_reward(
            ctx, LUCKY_REWARD_SPEC_MAP[LuckyRewardKind.POINTS_DOUBLE]
        )

    async def get_user_info_data(self, user_id: int) -> dict:
        """获取指定 Telegram 用户的结构化个人与媒体账号概览数据

        包括基础积分、签到统计、周期/总有效播放时长、各媒体账号的保活资格、
        本周期流量消耗、月基础流量配额、剩余基础流量及平滑推导后的下次重置时间。

        Args:
            user_id: Telegram 用户 ID。

        Returns:
            包含 Telegram 用户记录、各媒体账号明细及可注册服务器的聚合字典。
        """
        user = await self.telegram_repo.get_or_create(user_id)
        renew_score = int(await self.telegram_repo.get_renew_score())
        global_watch_time_enabled = await self.config_repo.is_watch_time_enabled()
        global_traffic_enabled = await self.config_repo.is_traffic_enabled()
        global_points_enabled = (
            await self.config_repo.get_settings(
                ConfigRepository.KEY_ENABLE_POINTS, "true"
            )
            == "true"
        )
        effective_points = await self.config_repo.get_effective_points_switches()
        enable_points_register = bool(
            effective_points.get("enable_points_register", False)
        )
        enable_points_request = bool(
            effective_points.get("enable_points_request", False)
        )
        enable_points_renew = bool(effective_points.get("enable_points_renew", False))
        enable_points_generate_code = bool(
            effective_points.get("enable_points_generate_code", False)
        )
        enable_points_traffic_pack = bool(
            effective_points.get("enable_points_traffic_pack", False)
        )

        media_accounts = []
        registered_server_ids: set[int] = set()
        cycle_watch_time_seconds = 0
        total_watch_time_seconds = 0
        if user.media_users:
            for mu in user.media_users:
                server = await self.server_repo.get_by_id(mu.server_id)
                registered_server_ids.add(mu.server_id)
                cycle_watch = int(mu.cycle_watch_time_seconds or 0)
                total_watch = int(mu.total_watch_time_seconds or 0)
                cycle_watch_time_seconds += cycle_watch
                total_watch_time_seconds += total_watch
                wt_status = evaluate_watch_time_renewal(
                    server,
                    mu,
                    global_watch_time_enabled=global_watch_time_enabled,
                )

                anchor_day = mu.anchor_day
                next_reset_at = mu.next_reset_at
                # 存量历史账号缺失锚定字段时，按现有到期日平滑推算周期重置点以保证展示完整
                if anchor_day is None or next_reset_at is None:
                    derived_anchor, _, derived_reset = backfill_billing_cycle(
                        current_expires_at=mu.expires_at,
                        anchor_day=anchor_day,
                        current_cycle_started_at=mu.cycle_started_at,
                        current_next_reset_at=next_reset_at,
                    )
                    anchor_day = derived_anchor
                    next_reset_at = derived_reset

                # 仅当全局及单服对应开关均生效时才视为启用
                enable_watch_time = is_watch_time_effective(
                    global_watch_time_enabled, server
                )
                enable_traffic = is_traffic_effective(global_traffic_enabled, server)
                request_points_cost = calculate_request_points_cost(server, renew_score)
                request_open, _, _ = evaluate_request_access(
                    server,
                    cost=request_points_cost,
                    points_request_allowed=enable_points_request,
                    points_request_reason="",
                )
                pack_open, _ = evaluate_traffic_pack_access(
                    server,
                    points_pack_allowed=enable_points_traffic_pack,
                    points_pack_reason="",
                    global_traffic_enabled=global_traffic_enabled,
                )
                pack_cost = (
                    calculate_traffic_pack_points_cost(server, renew_score)
                    if server is not None
                    else DEFAULT_TRAFFIC_PACK_COST_SCORE
                )
                pack_unit_bytes = (
                    calculate_traffic_pack_unit_bytes(server)
                    if server is not None
                    else DEFAULT_TRAFFIC_PACK_UNIT_BYTES
                )
                pack_size_gib = float(
                    server.traffic_pack_size_gib
                    if server is not None and server.traffic_pack_size_gib
                    else DEFAULT_TRAFFIC_PACK_SIZE_GIB
                )
                cycle_traffic = max(0, int(mu.cycle_traffic_bytes or 0))
                pack_bytes = max(0, int(mu.traffic_pack_bytes or 0))
                total_traffic = max(0, int(mu.total_traffic_bytes or 0))
                bal = calculate_traffic_balances(
                    server.traffic_quota_bytes if server else 0,
                    mu.cycle_traffic_adjustment_bytes,
                    cycle_traffic,
                    pack_bytes,
                )
                quota_bytes = bal.effective_base_quota_bytes if bal.is_limited else 0
                remaining_bytes = bal.base_remaining_bytes
                total_remaining_bytes = bal.total_remaining_bytes
                is_traffic_suspended = bool(mu.is_traffic_suspended)
                if mu.is_banned and is_traffic_suspended:
                    status_text = "🚫 封禁 · 超流停用"
                elif mu.is_banned:
                    status_text = "🚫 封禁"
                elif is_traffic_suspended:
                    status_text = "⛔ 超流停用"
                else:
                    status_text = "✅ 正常"

                media_accounts.append(
                    {
                        "media_user": mu,
                        "server": server,
                        "server_name": (
                            server.name if server else f"Server {mu.server_id}"
                        ),
                        "server_url": (
                            server.url if server and server.url else "Unknown"
                        ),
                        "server_type": (
                            server.server_type.capitalize() if server else "Undefined"
                        ),
                        "status_text": status_text,
                        "is_banned": mu.is_banned,
                        "media_name": mu.media_name,
                        "expires_at": mu.expires_at,
                        "allow_subtitle_upload": (
                            server.allow_subtitle_upload if server else False
                        ),
                        "allow_request": server.allow_request if server else False,
                        "request_open": request_open,
                        "request_points_mode": (
                            server.request_points_mode if server else "fixed"
                        ),
                        "request_points_cost": request_points_cost,
                        "tos": server.tos if server else None,
                        "enable_watch_time": enable_watch_time,
                        "cycle_watch_time_seconds": cycle_watch,
                        "total_watch_time_seconds": total_watch,
                        "anchor_day": anchor_day,
                        "next_reset_at": next_reset_at,
                        "auto_renew": bool(mu.auto_renew),
                        "auto_renew_open": is_auto_renew_open(
                            server,
                            watch_time_renewal_open=wt_status.is_open,
                            points_renew_open=enable_points_renew,
                        ),
                        "enable_watch_time_renewal": (
                            bool(server.enable_watch_time_renewal) if server else False
                        ),
                        "watch_time_renewal_threshold_seconds": wt_status.threshold_seconds,
                        "watch_time_renewal_open": wt_status.is_open,
                        "watch_time_renewal_eligible": wt_status.is_eligible,
                        "watch_time_renewal_status_text": wt_status.status_text,
                        "watch_time_progress_percent": wt_status.progress_percent,
                        "enable_traffic": enable_traffic,
                        "traffic_limited": bal.is_limited,
                        "cycle_traffic_bytes": cycle_traffic,
                        "traffic_pack_bytes": pack_bytes,
                        "total_traffic_bytes": total_traffic,
                        "traffic_quota_bytes": quota_bytes,
                        "server_traffic_quota_bytes": bal.server_quota_bytes,
                        "cycle_traffic_adjustment_bytes": bal.adjustment_bytes,
                        "traffic_remaining_bytes": remaining_bytes,
                        "traffic_total_remaining_bytes": total_remaining_bytes,
                        "is_traffic_suspended": is_traffic_suspended,
                        "enable_traffic_auto_suspend": (
                            bool(server.enable_traffic_auto_suspend)
                            if server
                            else False
                        ),
                        "allow_traffic_pack": (
                            bool(server.allow_traffic_pack) if server else False
                        ),
                        "traffic_pack_open": pack_open,
                        "traffic_pack_points_mode": (
                            server.traffic_pack_points_mode if server else "fixed"
                        ),
                        "traffic_pack_cost_score": pack_cost,
                        "traffic_pack_size_gib": pack_size_gib,
                        "traffic_pack_unit_bytes": pack_unit_bytes,
                        "can_points_renew": enable_points_renew,
                        "can_generate_code": enable_points_generate_code,
                    }
                )

        all_enabled = await self.server_repo.get_all_enabled()
        available_servers = []
        for srv in all_enabled:
            if srv.server_type not in (ServerType.EMBY, ServerType.JELLYFIN):
                continue
            if srv.id in registered_server_ids:
                continue

            mode = srv.registration_mode
            can_register = False
            reason = ""
            status_label = ""

            if mode == RegistrationMode.CLOSE:
                reason = "该服务器未开放注册"
                status_label = "已关闭"
            elif mode == RegistrationMode.OPEN:
                can_register = True
                status_label = "开放注册"
            elif mode == RegistrationMode.COUNT:
                if srv.registration_count_limit > 0:
                    can_register = True
                    status_label = f"剩{srv.registration_count_limit}名额"
                else:
                    reason = "注册名额已满"
                    status_label = "名额已满"
            elif mode == RegistrationMode.TIME:
                with contextlib.suppress(ValueError, TypeError):
                    ts = float(srv.registration_time_limit)
                    if datetime.now().timestamp() < ts:
                        can_register = True
                        status_label = "限时开放"
                    else:
                        reason = "开放注册时间已截止"
                        status_label = "已截止"
            elif mode == RegistrationMode.EXTERNAL:
                can_register = True
                status_label = "验证注册"
            elif mode == RegistrationMode.DEFAULT:
                if not enable_points_register:
                    reason = "当前仅开放激活码注册，请使用注册激活码或联系管理员。"
                    status_label = "需激活码"
                elif user.score >= renew_score:
                    can_register = True
                    status_label = f"需{renew_score}积分"
                else:
                    reason = (
                        f"积分不足，注册需要 {renew_score} 积分（当前 {user.score}）"
                    )
                    status_label = f"需{renew_score}积分"
            else:
                reason = "未知的注册模式"
                status_label = "不可用"

            requires_points = (
                mode == RegistrationMode.DEFAULT and enable_points_register
            )
            available_servers.append(
                {
                    "server_id": srv.id,
                    "server_name": srv.name,
                    "server_type": (
                        srv.server_type.capitalize() if srv.server_type else "Unknown"
                    ),
                    "server_url": srv.url or "",
                    "registration_mode": mode,
                    "can_register": can_register,
                    "reason": reason,
                    "status_label": status_label,
                    "tos": srv.tos,
                    "has_external_verification": mode == RegistrationMode.EXTERNAL,
                    "requires_points": requires_points,
                    "points_cost": renew_score if requires_points else 0,
                }
            )

        return {
            "user": user,
            "media_accounts": media_accounts,
            "available_servers": available_servers,
            "cycle_watch_time_seconds": cycle_watch_time_seconds,
            "total_watch_time_seconds": total_watch_time_seconds,
            "enable_points": global_points_enabled,
            **effective_points,
            "enable_watch_time": global_watch_time_enabled,
            "enable_traffic": global_traffic_enabled,
        }

    async def get_user_info(self, user_id: int) -> Result:
        """获取用于 Bot 文本降级展示的用户信息（按生效开关隐藏禁用体系字段）"""
        data = await self.get_user_info_data(user_id)
        user: TelegramUser = data["user"]
        media_accounts = data["media_accounts"]

        lines = [
            "👤 **个人信息**",
            "",
            f"**Telegram ID**: `{user.id}`",
        ]
        if data["enable_points"]:
            lines.append(f"**积分**: `{user.score}`")
        if data.get("enable_points_checkin"):
            lines.append(f"**连续签到**: `{user.current_consecutive_checkin_count}` 天")
            lines.append(f"**累计签到**: `{user.checkin_count}` 天")
        lines.append(f"**警告**: `{user.warning_count}` 次")
        message = "\n".join(lines)

        if media_accounts:
            from models.nginx import format_traffic_bytes

            message += "\n\n**媒体账户**\n"
            for mu in media_accounts:
                parts = [
                    f"- {mu['server_name']}: {mu['media_name']} ({mu['status_text']})"
                ]
                if mu.get("enable_watch_time"):
                    cycle_min = int(mu.get("cycle_watch_time_seconds", 0)) // 60
                    total_min = int(mu.get("total_watch_time_seconds", 0)) // 60
                    parts.append(
                        f"  有效播放时长: 本周期 {cycle_min} 分钟 / 累计 {total_min} 分钟"
                    )
                if mu.get("enable_traffic"):
                    used_s = format_traffic_bytes(int(mu.get("cycle_traffic_bytes", 0)))
                    quota_val = int(mu.get("traffic_quota_bytes", 0))
                    quota_s = (
                        format_traffic_bytes(quota_val)
                        if mu.get("traffic_limited")
                        else "不限"
                    )
                    pack_val = int(mu.get("traffic_pack_bytes", 0))
                    pack_note = (
                        f"（含流量包 {format_traffic_bytes(pack_val)}）"
                        if pack_val > 0
                        else ""
                    )
                    parts.append(f"  流量用量: {used_s} / {quota_s}{pack_note}")
                message += "\n".join(parts) + "\n"

        return Result(success=True, message=message)

    async def _safe_resolve_user_name(self, user_id: int | None) -> str:
        """安全解析 Telegram 用户展示名（兼容同步/异步/缺失测试替身）。

        Args:
            user_id: 目标 Telegram 用户 ID。

        Returns:
            非空用户名字符串；解析失败时返回空字符串。
        """
        if user_id is None:
            return ""
        client = getattr(getattr(self.app, "state", None), "telethon_client", None)
        getter = getattr(client, "get_user_name", None) if client is not None else None
        if not callable(getter):
            return ""
        try:
            res = getter(user_id)
            if inspect.isawaitable(res):
                res = await res
            if isinstance(res, str) and res.strip():
                return res.strip()
        except Exception:  # pylint: disable=broad-exception-caught
            return ""
        return ""

    async def _emit_admin_user_action(
        self,
        payload: Any,
        *,
        skip_inbox_fallback: bool = False,
    ) -> None:
        """通过统一通知管线发送管理员操作回执与审计消息（严格隔离外部发送异常）。

        Args:
            payload: ``AdminUserActionPayload`` 载荷实例。
            skip_inbox_fallback: 目标 ``TelegramUser`` 已被删除时传 ``True``，防止站内兜底外键失效。
        """
        from services.notification_service import NotificationService

        try:
            await NotificationService(self.app).emit(
                user_id=payload.target_user_id,
                payload=payload,
                session=self.session,
                skip_inbox_fallback=skip_inbox_fallback,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "发送管理员操作通知失败（已忽略，不影响主业务）: action={}, target={}, err={}",
                getattr(payload, "action_type", "unknown"),
                getattr(payload, "target_user_id", None),
                exc,
            )

    async def resolve_user_display_name(
        self, user_id: int | None, *, fallback: str = ""
    ) -> str:
        """公开的 Telegram 用户展示名安全解析助手（兼容 Bot 与 Mini App 管理入口）。

        Args:
            user_id: 目标 Telegram 用户 ID（可为 ``None``）。
            fallback: 无法解析或为空时的兜底文案。

        Returns:
            解析得到的非空展示名或 ``fallback``。
        """
        resolved = await self._safe_resolve_user_name(user_id)
        return resolved if resolved else fallback

    async def snapshot_user_media_summary(
        self, user_id: int
    ) -> tuple[str | None, str | None, ServerInstance | None]:
        """在删号前采集目标用户关联媒体服务器与媒体账号摘要，供踢出审计通知使用。

        Args:
            user_id: 目标 Telegram 用户 ID。

        Returns:
            ``(server_display, media_display, single_server_or_none)`` 三元组。
        """
        user = await self.telegram_repo.get_by_id(user_id)
        if not user or not user.media_users:
            return None, None, None
        pairs: list[tuple[str, str]] = []
        last_srv: ServerInstance | None = None
        for mu in user.media_users:
            srv = await self.server_repo.get_by_id(mu.server_id)
            srv_name = srv.name if srv is not None else f"Server {mu.server_id}"
            pairs.append((srv_name, mu.media_name))
            last_srv = srv
        if len(pairs) == 1:
            return pairs[0][0], pairs[0][1], last_srv
        return ", ".join(s for s, _ in pairs), ", ".join(m for _, m in pairs), None

    async def notify_admin_ban_or_kick(
        self,
        *,
        action_type: Literal["kick", "ban"],
        target_user_id: int,
        operator_name: str = "管理员",
        target_name: str = "",
        server_name: str | None = None,
        media_name: str | None = None,
        context_server: ServerInstance | None = None,
    ) -> None:
        """发送管理员踢出或封禁操作回执与审计事件，并自动规避已删号外键兜底冲突。

        Args:
            action_type: 管理动作类型（``"kick"`` 或 ``"ban"``）。
            target_user_id: 目标 Telegram 用户 ID。
            operator_name: 执行操作的管理员展示名。
            target_name: 预解析的目标用户展示名。
            server_name: 关联服务器展示名称（可选）。
            media_name: 关联媒体账号名（可选）。
            context_server: 单服场景的服务器实例，用于构建 ADR-0004 经济上下文。
        """
        from services.notification_renderer import (
            AdminUserActionPayload,
            EffectiveEconomyContext,
        )

        resolved_target = target_name.strip() or await self._safe_resolve_user_name(
            target_user_id
        )
        tg_exists = await self.telegram_repo.get_by_id(target_user_id) is not None
        await self._emit_admin_user_action(
            AdminUserActionPayload(
                action_type=action_type,
                target_user_id=target_user_id,
                target_user_name=resolved_target,
                operator_name=operator_name.strip() or "管理员",
                server_name=server_name,
                media_name=media_name,
                economy=EffectiveEconomyContext.from_cache(context_server),
            ),
            # 踢出删号或未入库用户不存在 TelegramUser 外键记录，跳过站内落箱防止 FK 失败
            skip_inbox_fallback=(not tg_exists),
        )

    async def warn_user(
        self,
        target_user_id: int,
        *,
        increment: int = 1,
        operator_name: str = "管理员",
        reason: str = "",
    ) -> TelegramUser:
        """为目标用户累计警告次数；仅在积分惩罚通道生效时连带扣除等额积分并发送操作回执。

        Args:
            target_user_id: 目标用户 Telegram ID。
            increment: 本次新增警告次数（默认 ``1``）。
            operator_name: 执行操作的管理员显示名。
            reason: 可选操作备注说明。

        Returns:
            更新并提交后的 ``TelegramUser`` 对象。
        """
        from services.notification_renderer import (
            AdminUserActionPayload,
            EffectiveEconomyContext,
        )

        existing = await self.telegram_repo.get_by_id(target_user_id)
        prev_score = int(existing.score) if existing is not None else 0

        warn_penalty_enabled = await self.config_repo.is_points_feature_enabled(
            ConfigRepository.KEY_ENABLE_POINTS_WARN
        )
        if warn_penalty_enabled:
            user = await self.telegram_repo.update_warn_and_score(
                target_user_id, increment=increment
            )
        else:
            user = await self.telegram_repo.update_warn_only(
                target_user_id, increment=increment
            )

        target_name = await self._safe_resolve_user_name(target_user_id)
        actual_delta = (int(user.score) - prev_score) if warn_penalty_enabled else None
        await self._emit_admin_user_action(
            AdminUserActionPayload(
                action_type="warn",
                target_user_id=target_user_id,
                target_user_name=target_name,
                operator_name=operator_name.strip() or "管理员",
                reason=reason,
                warning_count=int(user.warning_count),
                score_delta=actual_delta,
                current_score=int(user.score) if warn_penalty_enabled else None,
                economy=EffectiveEconomyContext.from_cache(None),
            )
        )
        return user

    async def change_user_score(
        self,
        target_user_id: int,
        score_change: int,
        *,
        operator_name: str = "管理员",
        reason: str = "",
    ) -> Result:
        """管理员手动调整用户社区积分；全局积分总开关关闭时拒绝操作，成功时发送透明回执。

        Args:
            target_user_id: 目标用户 Telegram ID。
            score_change: 积分增减量（正数加分，负数扣分）。
            operator_name: 执行操作的管理员显示名。
            reason: 可选操作备注说明。

        Returns:
            ``Result``，成功时 ``extra_data`` 携带更新后的 ``user`` 与 ``score``。
        """
        if not await self.config_repo.is_points_enabled():
            return Result(False, "全局积分总开关已关闭，无法修改积分。")
        user = await self.telegram_repo.update_score(target_user_id, score_change)

        from services.notification_renderer import (
            AdminUserActionPayload,
            EffectiveEconomyContext,
        )

        target_name = await self._safe_resolve_user_name(target_user_id)
        await self._emit_admin_user_action(
            AdminUserActionPayload(
                action_type="score_adjust",
                target_user_id=target_user_id,
                target_user_name=target_name,
                operator_name=operator_name.strip() or "管理员",
                reason=reason,
                score_delta=int(score_change),
                current_score=int(user.score),
                economy=EffectiveEconomyContext.from_cache(None),
            )
        )

        prefix = "+" if score_change > 0 else ""
        return Result(
            True,
            f"已修改积分 ({prefix}{score_change})，当前积分: {user.score}",
            extra_data={"score": user.score, "user": user},
        )

    async def get_rank_list(self) -> Result:
        """获取排行榜"""
        users = await self.telegram_repo.get_top_users(10)
        if not users:
            return Result(True, "暂无排名数据。")

        return Result(True, "排行榜获取成功", extra_data=users)

    async def delete_account(
        self,
        user_id: int,
        account_type: Literal["media", "tg", "both"],
        *,
        operator_name: str = "管理员",
        emit_notification: bool = False,
        reason: str = "",
    ) -> Result:
        """删除用户关联的媒体账号或 Telegram 绑定记录，并按需发送管理员删号回执与审计。

        Args:
            user_id: 目标 Telegram 用户 ID。
            account_type: 删除范围（``media`` / ``tg`` / ``both``）。
            operator_name: 执行操作的管理员显示名。
            emit_notification: 是否发射 ``ADMIN_USER_ACTION(delete)``（如 ``/kick`` 外层统一发 ``kick`` 时传 ``False``）。
            reason: 可选操作原因说明。

        Returns:
            ``Result`` 删除结果。
        """
        user = await self.telegram_repo.get_by_id(user_id)
        if not user:
            return Result(True, "未找到该 Telegram 账户，无需删除。")

        target_name = await self._safe_resolve_user_name(user_id)
        deleted_media: list[tuple[str, str]] = []
        last_deleted_server: ServerInstance | None = None
        telegram_deleted = False

        logs = []
        has_media_failure = False
        try:
            if account_type in ["media", "both"] and user.media_users:
                for mu in list(user.media_users):
                    client = self.media_clients.get(mu.server_id)
                    server_name = f"Server {mu.server_id}"

                    server = await self.server_repo.get_by_id(mu.server_id)
                    if server:
                        server_name = server.name

                    if not client:
                        has_media_failure = True
                        logs.append(f"{server_name} 实例未连接，保留本地记录以便重试。")
                        continue

                    try:
                        await client.delete_user(mu.media_id)
                        logs.append(f"已删除 {server_name} 上的账户。")
                    except HTTPStatusError as e:
                        if e.response.status_code == 404:
                            logs.append(
                                f"{server_name} 上的账户已不存在，已清理本地记录。"
                            )
                        else:
                            has_media_failure = True
                            logger.error(t"删除 {server_name} 上的用户失败: {e}")
                            logs.append(
                                f"删除 {server_name} 账户失败 (API错误)，保留本地记录以便重试。"
                            )
                            continue
                    except (HTTPError, ValueError) as e:
                        has_media_failure = True
                        logger.error(t"删除 {server_name} 上的用户失败: {e}")
                        logs.append(
                            f"删除 {server_name} 账户失败 (API错误)，保留本地记录以便重试。"
                        )
                        continue

                    mu_uid = int(mu.id)
                    mu_sid = int(mu.server_id)
                    deleted_media.append((server_name, mu.media_name))
                    last_deleted_server = server
                    await self.media_repo.delete(mu)
                    remove_account_lifecycle_jobs(
                        getattr(getattr(self.app, "state", None), "scheduler", None),
                        mu_uid,
                        mu_sid,
                    )

            if account_type in ["tg", "both"]:
                if has_media_failure:
                    logs.append(
                        "存在未删除的媒体账户记录，已保留 Telegram 绑定记录以便重试。"
                    )
                else:
                    await self.telegram_repo.delete_by_id(user_id)
                    telegram_deleted = True
                    logs.append("Telegram 绑定记录已清除。")

            if emit_notification and (deleted_media or telegram_deleted):
                from services.notification_renderer import (
                    AdminUserActionPayload,
                    EffectiveEconomyContext,
                )

                srv_display = (
                    deleted_media[0][0]
                    if len(deleted_media) == 1
                    else (
                        ", ".join(s for s, _ in deleted_media)
                        if deleted_media
                        else None
                    )
                )
                media_display = (
                    deleted_media[0][1]
                    if len(deleted_media) == 1
                    else (
                        ", ".join(m for _, m in deleted_media)
                        if deleted_media
                        else None
                    )
                )
                ctx_server = last_deleted_server if len(deleted_media) == 1 else None
                await self._emit_admin_user_action(
                    AdminUserActionPayload(
                        action_type="delete",
                        target_user_id=user_id,
                        target_user_name=target_name,
                        operator_name=operator_name.strip() or "管理员",
                        server_name=srv_display,
                        media_name=media_display,
                        reason=reason,
                        economy=EffectiveEconomyContext.from_cache(ctx_server),
                    ),
                    # TelegramUser 记录已被清除时外键不可写入收件箱，跳过兜底落箱避免数据库约束冲突
                    skip_inbox_fallback=telegram_deleted,
                )

            msg = "\n".join(logs) if logs else "无关联账户需要删除。"
            return Result(not has_media_failure, msg)

        except (SQLAlchemyError, HTTPError, ValueError) as e:
            logger.exception(t"删除用户 {user_id} 账户时出错: {e}")
            return Result(False, f"删除过程中发生错误: {e!s}")
