import contextlib
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
    calculate_request_points_cost,
    calculate_traffic_balances,
    calculate_traffic_pack_points_cost,
    calculate_traffic_pack_unit_bytes,
    evaluate_request_access,
    evaluate_traffic_pack_access,
    is_auto_renew_open,
    is_traffic_effective,
    is_watch_time_effective,
)
from core.media_utils import get_account_lock
from core.scheduler_jobs import (
    remove_account_lifecycle_jobs,
    schedule_account_lifecycle_jobs,
)
from core.telegram_message import TelegramMessage
from models.orm import RegistrationMode, ServerType, TelegramUser
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
        self.media_clients: dict[int, MediaService] = app.state.media_clients or {}

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
        return await self._perform_lucky_checkin(user, username, is_consecutive)

    async def _perform_lucky_checkin(
        self,
        user: TelegramUser,
        username: str | Literal[False] | None,
        is_consecutive: bool,
    ) -> Result:
        """执行幸运签到抽奖并发放随机注册码/续期码、账户续期或双倍积分奖励

        抽中直接续期奖励时按账号锁串行延长关联媒体账号有效期，并在解除过期封禁前
        遵循组合状态（过期封禁 OR 超流停用）判定是否真正解封外部媒体账号；
        若奖励无法兑现则回退为固定签到积分。

        Args:
            user: 当前签到的 Telegram 用户 ORM 对象。
            username: 用于展示的用户名或显示名；无用户名时为 ``None``/``False``。
            is_consecutive: 本次签到是否计入连续签到周期。

        Returns:
            包含签到群消息文案及可选中奖私聊文案的 ``Result`` 对象。
        """
        # 提前提取纯标量 ID 与关联账号主键，避免后续循环内提交/回滚事务导致 ORM 属性过期
        user_id = int(user.id)
        target_pairs = [
            (int(mu.id), int(mu.server_id)) for mu in (user.media_users or [])
        ]
        options = ["fullcode", "halfcode", "weekcode", "daycode", "double"]
        probability = [0.01, 0.02, 0.12, 0.15, 0.7]
        result = choices(options, probability)[0]

        current_renew_score = await self.telegram_repo.get_renew_score()

        if result == "fullcode":
            # 获取所有已启用的服务器
            enabled_servers = await self.server_repo.get_all_enabled()

            # 筛选符合条件的服务器：
            candidate_servers = [
                s
                for s in enabled_servers
                if s.id in self.media_clients
                and s.server_type in (ServerType.EMBY, ServerType.JELLYFIN)
                and s.registration_mode == RegistrationMode.DEFAULT
            ]

            if not candidate_servers:
                await self.telegram_repo.update_checkin(
                    user_id, 5, is_consecutive=is_consecutive
                )
                return Result(
                    success=True,
                    message=TelegramMessage.render_markdown(
                        t"签到成功！[{username}](tg://user?id={user_id})获得 **5** 积分 (暂无符合发放奖励条件的服务器)。"
                    ),
                )

            target_server = choice(candidate_servers)
            if not target_server.id:
                await self.telegram_repo.update_checkin(
                    user_id, 5, is_consecutive=is_consecutive
                )
                return Result(
                    success=True,
                    message=TelegramMessage.render_markdown(
                        t"签到成功！[{username}](tg://user?id={user_id})获得 **5** 积分 (暂无符合发放奖励条件的服务器)。"
                    ),
                )

            code_type = choice(["renew", "signup"])
            code_name = "续期码" if code_type == "renew" else "注册码"

            code = await self.code_repo.create(
                code_type, target_server.code_expiry_days, target_server.id
            )
            await self.telegram_repo.update_checkin(
                user_id, 0, is_consecutive=is_consecutive
            )

            return Result(
                success=True,
                message=TelegramMessage.render_markdown(
                    t"🎉 **恭喜[{username}](tg://user?id={user_id})抽中大奖！** {target_server.name} 的 {code_name}已通过私信发送给您。"
                ),
                private_message=TelegramMessage.render_markdown(t"""\
                    🎁 签到大奖！
                    服务器: {target_server.name}
                    类型: {code_name}
                    代码: `{code.code}`
                    请妥善保管！"""),
            )

        if result in ["halfcode", "weekcode", "daycode"]:
            days = {"halfcode": 15, "weekcode": 7, "daycode": 1}[result]

            if target_pairs:
                extended_servers = []
                for uid, sid in target_pairs:
                    async with get_account_lock(uid, sid):
                        await self.media_repo.end_clean_read_tx()
                        fresh_mu = await self.media_repo.get_by_id(uid, sid, fresh=True)
                        if fresh_mu is None:
                            continue
                        client = self.media_clients.get(sid)
                        # 仅当清除过期封禁且账号不同时处于超流停用时才解封外部账户（遵循 ADR-0002）
                        keep_disabled = compute_external_disabled_state(
                            is_banned=False,
                            is_traffic_suspended=bool(fresh_mu.is_traffic_suspended),
                        )
                        if (
                            client is not None
                            and (fresh_mu.is_banned or fresh_mu.delete_at is not None)
                            and not keep_disabled
                        ):
                            await sync_external_disabled_state(
                                client,
                                media_id=fresh_mu.media_id,
                                target_disabled=False,
                                log_context="签到续期恢复媒体账号",
                                user_id=user_id,
                                server_id=sid,
                            )

                        updated_mu = await self.media_repo.extend_expiry(fresh_mu, days)
                        schedule_account_lifecycle_jobs(
                            getattr(
                                getattr(self.app, "state", None), "scheduler", None
                            ),
                            updated_mu,
                        )

                    server = await self.server_repo.get_by_id(sid)
                    extended_servers.append(server.name if server else str(sid))

                await self.telegram_repo.update_checkin(
                    user_id, 0, is_consecutive=is_consecutive
                )
                server_str = ", ".join(extended_servers)
                return Result(
                    success=True,
                    message=TelegramMessage.render_markdown(
                        t"🎉 **恭喜[{username}](tg://user?id={user_id})中奖！** 您的媒体账号 ({server_str}) 已自动延长 **{days}** 天有效期！"
                    ),
                )

            score = int(current_renew_score / 30 * days)
            await self.telegram_repo.update_checkin(
                user_id, score, is_consecutive=is_consecutive
            )
            return Result(
                success=True,
                message=TelegramMessage.render_markdown(
                    t"🎉 **恭喜[{username}](tg://user?id={user_id})中奖！** 获得 {days} 天时长奖励，因未绑定账户自动折算为 **{score}** 积分！"
                ),
            )

        # 检查连签 (lucky 同样享受连签加成，但如果是 flip/code 类奖励则不加积分)
        bonus = 2 if is_consecutive else 0

        if result == "double":
            base = abs(randint(2, 4)) * 2
            total = base + bonus
            await self.telegram_repo.update_checkin(
                user_id, total, is_consecutive=is_consecutive
            )
            msg = TelegramMessage.render_markdown(
                t"🎉 **恭喜！** [{username}](tg://user?id={user_id})签到积分翻倍，您获得了 **{total}** 积分。"
            )
            if is_consecutive:
                msg += f"\n(基础 {base} + 连签 {bonus})"
            return Result(success=True, message=msg)

        # 保底逻辑
        total = 1 + bonus
        await self.telegram_repo.update_checkin(
            user_id, total, is_consecutive=is_consecutive
        )
        msg = TelegramMessage.render_markdown(
            t"签到成功！[{username}](tg://user?id={user_id})获得了保底 **{total}** 积分。"
        )
        if is_consecutive:
            msg += f"\n(基础 1 + 连签 {bonus})"
        return Result(success=True, message=msg)

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

    async def warn_user(
        self, target_user_id: int, *, increment: int = 1
    ) -> TelegramUser:
        """为目标用户累计警告次数；仅在积分惩罚通道生效时连带扣除等额积分。

        Args:
            target_user_id: 目标用户 Telegram ID。
            increment: 本次新增警告次数（默认 ``1``）。

        Returns:
            更新并提交后的 ``TelegramUser`` 对象。
        """
        warn_penalty_enabled = await self.config_repo.is_points_feature_enabled(
            ConfigRepository.KEY_ENABLE_POINTS_WARN
        )
        if warn_penalty_enabled:
            return await self.telegram_repo.update_warn_and_score(
                target_user_id, increment=increment
            )
        return await self.telegram_repo.update_warn_only(
            target_user_id, increment=increment
        )

    async def change_user_score(self, target_user_id: int, score_change: int) -> Result:
        """管理员手动调整用户社区积分；全局积分总开关关闭时拒绝操作。

        Args:
            target_user_id: 目标用户 Telegram ID。
            score_change: 积分增减量（正数加分，负数扣分）。

        Returns:
            ``Result``，成功时 ``extra_data`` 携带更新后的 ``user`` 与 ``score``。
        """
        if not await self.config_repo.is_points_enabled():
            return Result(False, "全局积分总开关已关闭，无法修改积分。")
        user = await self.telegram_repo.update_score(target_user_id, score_change)
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
        self, user_id: int, account_type: Literal["media", "tg", "both"]
    ) -> Result:
        """删除账户"""
        user = await self.telegram_repo.get_by_id(user_id)
        if not user:
            return Result(True, "未找到该 Telegram 账户，无需删除。")

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
                    logs.append("Telegram 绑定记录已清除。")

            msg = "\n".join(logs) if logs else "无关联账户需要删除。"
            return Result(not has_media_failure, msg)

        except (SQLAlchemyError, HTTPError, ValueError) as e:
            logger.exception(t"删除用户 {user_id} 账户时出错: {e}")
            return Result(False, f"删除过程中发生错误: {e!s}")
