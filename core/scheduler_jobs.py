import textwrap

from loguru import logger

from core.config import get_settings
from core.database import async_session
from core.telegram_manager import TelethonClientWarper
from repositories.emby_repo import EmbyRepository
from services.media_service import MediaService
from services.score_service import ScoreService

settings = get_settings()

async def ban_expired_users() -> None:
    """封禁过期用户
    检查所有用户的订阅状态，封禁那些订阅已过期的用户。
    """
    from main import app  # 避免循环导入
    async with async_session() as session:
        try:
            emby_repo = EmbyRepository(session)
            media_service: MediaService = app.state.media_client

            users = await emby_repo.find_expired_for_ban()
            if not users:
                logger.info("没有需要封禁的用户。")
                return

            for user in users:
                logger.info("封禁用户: {} (ID: {})", user.id, user.emby_id)
                await media_service.ban_or_unban(
                    user_id=user.emby_id,
                    is_ban=True
                )
        except Exception as e:
            logger.exception("封禁过期用户时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()

async def delete_expired_banned_users() -> None:
    """删除已封禁且过期的用户
    删除那些已经被封禁且订阅过期的用户，以释放系统资源。
    """
    from main import app  # 避免循环导入
    async with async_session() as session:
        try:
            emby_repo = EmbyRepository(session)
            media_service: MediaService = app.state.media_client

            users = await emby_repo.find_ban()
            if not users:
                logger.info("没有需要删除的封禁用户。")
                return

            for user in users:
                logger.info("删除封禁用户: {} (ID: {})", user.id, user.emby_id)
                await media_service.delete_by_id(user.emby_id)

        except Exception as e:
            logger.exception("删除封禁用户时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()

async def settle_scores() -> None:
    """结算用户积分
    根据用户的订阅状态和使用情况，调整他们的积分。
    """
    from main import app  # 避免循环导入
    async with async_session() as session:
        try:
            score_service = ScoreService(session, app.state.message_tracker)
            result = await score_service.settle_and_clear_scores()
            client: TelethonClientWarper = app.state.telethon_client

            if result is None:
                await client.send_message(settings.telegram_chat_id, "当前无积分可结算。")
                return

            summary = textwrap.dedent(f"""\
                ✅ 积分结算完成！
                共结算 **{result.total_score_settled}** 活跃度积分.
                本次结算详情:
                """)
            summary_msg = await client.send_message(settings.telegram_chat_id, summary)

            user_details = []
            for user_id, score_change in result.user_score_changes.items(): # type: ignore
                user_name = await client.get_user_name(user_id)
                user_details.append(f"- [{user_name}](tg://user?id={user_id}): `+{score_change}`")
            final_summary = summary + "\n".join(user_details)
            await client.client.edit_message(summary_msg, final_summary)

        except Exception as e:
            logger.exception("结算用户积分时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()
