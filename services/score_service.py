import textwrap
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio.session import AsyncSession

from repositories.telegram_repo import TelegramRepository
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
        self.telegram_repo = TelegramRepository(session)

    async def process_message(self, user_id: int) -> None | Result:
        """处理用户消息，检测是否为刷屏行为。
        
        Args:
            user_id (int): 发送消息的用户ID。
        
        Returns:
            bool: 如果检测到刷屏行为则返回True，否则返回False。
        """
        if self.state.last_user_id != user_id:
            self.state.last_user_id = user_id
            self.state.consecutive_count = 1

            self.state.message_counts[user_id] = self.state.message_counts.get(user_id, 0) + 1

            return None

        self.state.consecutive_count += 1

        if self.state.consecutive_count > 5:
            # 重置状态以防止重复处罚
            self.state.consecutive_count = 0

            updated_user = await self.telegram_repo.update_warn_and_score(user_id)
            if updated_user:
                return Result(
                    success=True,
                    message=textwrap.dedent(f"""\
                        用户 [{user_id}](tg://user?id={user_id}) 由于刷屏行为已被警告一次。
                        
                        当前警告次数: **{updated_user.warning_count}**
                        当前积分: **{updated_user.score}**
                    """)
                )
            else:
                return Result(success=False, message=f"用户 {user_id} 刷屏警告失败，请管理员关注。")

    def _calculate_distribution(self) -> tuple[dict[int, int], int]:
        """积分计算逻辑: 简单线性模型
        每条消息 = 1 积分
        单次结算每人上限 = 20 积分
        """
        user_deltas = {}
        total_deltas = 0

        for user_id, count in self.state.message_counts.items():
            # 简单限流: 超过20条也只算20分
            score = min(count, 20)
            if score > 0:
                user_deltas[user_id] = score
                total_deltas += score

        return user_deltas, total_deltas

    async def settle_and_clear_scores(self):
        """结算积分并清理状态"""
        if not self.state.message_counts:
            return

        score_deltas, total_score = self._calculate_distribution()
        if not score_deltas:
            return

        await self.telegram_repo.batch_update_scores(score_deltas)

        self.state.message_counts.clear()

        return SettlementResult(
            total_score_settled=total_score,
            user_score_changes=score_deltas
        )
