import inspect
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio.session import AsyncSession
from telethon import errors

from core.telegram_message import TelegramMessage
from repositories.config_repo import ConfigRepository
from repositories.telegram_repo import TelegramRepository
from services.notification_renderer import (
    ScoreLeaderboardEntry,
    ScoreSettlementReportPayload,
)
from services.user_service import Result


@dataclass
class MessageTrackingState:
    last_user_id: int = 0
    consecutive_count: int = 0
    message_counts: dict[int, int] = field(default_factory=dict)


@dataclass
class SettlementResult:
    """结算结果"""

    total_score_settled: int
    user_score_changes: dict[int, int] = field(default_factory=dict)


class ScoreService:
    def __init__(self, session: AsyncSession, state: MessageTrackingState) -> None:
        self.state = state
        self.config_repo = ConfigRepository(session)
        self.telegram_repo = TelegramRepository(session)

    async def process_message(
        self, user_id: int, admin_ids: list | set, count_score: bool = True
    ) -> Result | None:
        """处理群聊用户消息，按需累计发言积分并检测连续刷屏行为。

        Args:
            user_id: 发送消息的 Telegram 用户 ID。
            admin_ids: 管理员 ID 列表或集合。
            count_score: 调用方判定的是否允许计分标记（如过滤水贴或已关闭群聊积分）。

        Returns:
            仅在触发连续刷屏警告门槛时返回带警告文案的 ``Result``，否则返回 ``None``。

        副作用：更新内存连续发言计数；超过刷屏阈值时调用仓储记录警告。
        """
        if self.state.last_user_id != user_id:
            self.state.last_user_id = user_id
            self.state.consecutive_count = 1

            # 即使直调服务，也需受总开关与群聊发言积分开关控制；非积分防刷屏计数已在上方保留
            chat_points_enabled = await self.config_repo.is_points_feature_enabled(
                ConfigRepository.KEY_ENABLE_POINTS_CHAT
            )
            if user_id in admin_ids or not count_score or not chat_points_enabled:
                return None

            self.state.message_counts[user_id] = (
                self.state.message_counts.get(user_id, 0) + 1
            )

            return None

        if user_id not in admin_ids:
            self.state.consecutive_count += 1

        points_cfg = await self.config_repo.get_points_config()
        spam_threshold = int(points_cfg["points_spam_threshold"])
        if spam_threshold > 0 and self.state.consecutive_count > spam_threshold:
            # 重置状态以防止重复处罚
            self.state.consecutive_count = 0

            # 防刷屏警告本身独立运作；仅在积分总开关与“刷屏扣分”子开关同时生效时才连带扣分并输出积分
            spam_penalty_enabled = await self.config_repo.is_points_feature_enabled(
                ConfigRepository.KEY_ENABLE_POINTS_SPAM
            )
            if spam_penalty_enabled:
                updated_user = await self.telegram_repo.update_warn_and_score(user_id)
            else:
                updated_user = await self.telegram_repo.update_warn_only(user_id)
            if updated_user:
                score_line = (
                    f"\n当前积分: **{int(updated_user.score)}**"
                    if spam_penalty_enabled
                    else ""
                )
                return Result(
                    success=True,
                    message=TelegramMessage.render_markdown(t"""\
                        用户 [{user_id}](tg://user?id={user_id}) 由于刷屏行为已被警告一次。

                        当前警告次数: **{updated_user.warning_count}**{score_line:raw}
                    """),
                )
            return Result(
                success=False,
                message=f"用户 {user_id} 刷屏警告失败，请管理员关注。",
            )

        return None

    def _calculate_distribution(self) -> tuple[dict[int, int], int]:
        """积分计算逻辑: 简单线性模型
        每条消息 = 1 积分
        单次结算每人上限 = 可配置的 points_message_daily_cap（默认 20 积分）
        """
        points_cfg = ConfigRepository.get_points_config_from_cache()
        daily_cap = int(points_cfg["points_message_daily_cap"])
        user_deltas = {}
        total_deltas = 0

        for user_id, count in self.state.message_counts.items():
            # 简单限流: 超过上限也只算上限积分
            score = min(count, daily_cap)
            if score > 0:
                user_deltas[user_id] = score
                total_deltas += score

        return user_deltas, total_deltas

    async def settle_and_clear_scores(self) -> SettlementResult | None:
        """结算每日群聊发言积分并清理统计队列。

        Returns:
            成功发放积分时返回 ``SettlementResult``；若无待结算计数或开关已关闭则返回 ``None``。

        副作用：开关放行时批量累加用户积分并清空计数队列；开关关闭时直接清空暂存计数不发分。
        """
        if not await self.config_repo.is_points_feature_enabled(
            ConfigRepository.KEY_ENABLE_POINTS_CHAT
        ):
            self.state.message_counts.clear()
            return None

        if not self.state.message_counts:
            return None

        score_deltas, total_score = self._calculate_distribution()
        if not score_deltas:
            return None

        await self.telegram_repo.batch_update_scores(score_deltas)

        self.state.message_counts.clear()

        return SettlementResult(
            total_score_settled=total_score, user_score_changes=score_deltas
        )


async def resolve_telegram_display_name(client: Any, user_id: int) -> str:
    """安全获取 Telegram 用户显示名；客户端缺失、未实现或查询异常时回退数字 ID。"""
    if client is None:
        return str(user_id)
    get_name = getattr(client, "get_user_name", None)
    if not callable(get_name):
        return str(user_id)
    try:
        name = get_name(user_id)
        if inspect.isawaitable(name):
            name = await name
        if isinstance(name, str) and name.strip():
            return name.strip()
        return str(user_id)
    except (errors.RPCError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("获取用户 {} 的显示名称失败: {}", user_id, exc)
        return str(user_id)


async def build_score_settlement_payload(
    score_service: ScoreService,
    client: Any,
    result: SettlementResult | None,
    *,
    is_manual: bool,
) -> ScoreSettlementReportPayload:
    """根据结算结果与总榜数据构造强类型社区积分结算播报载荷。

    Args:
        score_service: 积分服务实例（用于查询总榜前 10 名用户）。
        client: Telethon 客户端包装或测试替身（用于解析用户显示名称）。
        result: 本次结算结果；为 ``None`` 时生成“今日无活跃积分变动”空报。
        is_manual: 是否由管理员 ``/settle`` 手动触发。

    Returns:
        ``ScoreSettlementReportPayload`` 强类型载荷实例。
    """
    if result is None:
        return ScoreSettlementReportPayload(
            total_score_settled=0,
            daily_top=(),
            overall_top=(),
            is_manual=is_manual,
        )

    # 构建今日积分飙升榜 (Top 10)
    sorted_changes = sorted(
        result.user_score_changes.items(), key=lambda item: item[1], reverse=True
    )[:10]
    daily_top: list[ScoreLeaderboardEntry] = []
    for uid, score_change in sorted_changes:
        daily_top.append(
            ScoreLeaderboardEntry(
                user_id=uid,
                user_name=await resolve_telegram_display_name(client, uid),
                score=int(score_change),
            )
        )

    # 构建积分总榜 (Top 10)
    top_users = await score_service.telegram_repo.get_top_users(10)
    overall_top: list[ScoreLeaderboardEntry] = []
    for user in top_users:
        overall_top.append(
            ScoreLeaderboardEntry(
                user_id=user.id,
                user_name=await resolve_telegram_display_name(client, user.id),
                score=int(user.score),
            )
        )

    return ScoreSettlementReportPayload(
        total_score_settled=int(result.total_score_settled),
        daily_top=tuple(daily_top),
        overall_top=tuple(overall_top),
        is_manual=is_manual,
    )
