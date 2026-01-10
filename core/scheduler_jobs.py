import textwrap

from httpx import HTTPError
from loguru import logger

from core.config import get_settings
from core.database import async_session
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from repositories.media_repo import MediaRepository
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
            media_repo = MediaRepository(session)
            media_clients: dict[int, MediaService] = app.state.media_clients

            users = await media_repo.find_expired_for_ban()
            if not users:
                logger.info("没有需要封禁的用户。")
                return

            for user in users:
                client = media_clients.get(user.server_id)
                if not client:
                    logger.warning("未找到服务器实例(ID: {})，跳过封禁用户: {} (ID: {})", user.server_id, user.id, user.media_id)
                    continue
                logger.info("封禁用户: {} (ID: {}) Server: {}", user.id, user.media_id, user.server_id)
                await client.ban_or_unban(
                    user_id=user.media_id,
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
            media_repo = MediaRepository(session)
            media_clients: dict[int, MediaService] = app.state.media_clients

            users = await media_repo.find_ban()
            if not users:
                logger.info("没有需要删除的封禁用户。")
                return

            for user in users:
                client = media_clients.get(user.server_id)
                if not client:
                    logger.warning("未找到服务器实例(ID: {})，仅清理数据库记录: {} (ID: {})", user.server_id, user.id, user.media_id)
                    continue
                try:
                    await client.delete_user(user.media_id)
                except HTTPError:
                    logger.error("删除封禁用户失败: {} (ID: {})", user.id, user.media_id)
        except Exception as e:
            logger.exception("删除封禁用户时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()

async def settle_scores() -> None:
    """结算用户积分"""
    if ConfigRepository.cache.get(ConfigRepository.KEY_ENABLE_POINTS, "true") != "true":
        return

    from main import app  # 避免循环导入
    async with async_session() as session:
        try:
            score_service = ScoreService(session, app.state.message_tracker)
            result = await score_service.settle_and_clear_scores()
            client: TelethonClientWarper = app.state.telethon_client

            if result is None:
                await client.send_message(settings.telegram_chat_id, "📊 **每日积分结算报告**\n\n今日无活跃积分变动。")
                return

            # 构建今日榜单 (按 increment 倒序)
            sorted_changes = sorted(result.user_score_changes.items(), key=lambda x: x[1], reverse=True)[:10]

            daily_lines = ["📈 **今日积分飙升榜 (Top 10)**"]
            for idx, (user_id, score_change) in enumerate(sorted_changes, 1):
                user_name = await client.get_user_name(user_id)
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
                daily_lines.append(f"{medal} [{user_name}](tg://user?id={user_id}) — `+{score_change}`")

            # 构建总榜单
            top_users = await score_service.telegram_repo.get_top_users(10)

            total_lines = ["🏆 **积分总榜 (Top 10)**"]
            for idx, user in enumerate(top_users, 1):
                try:
                    user_name = await client.get_user_name(user.id)
                    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
                    total_lines.append(f"{medal} [{user_name}](tg://user?id={user.id}) — `{user.score}`")
                except Exception:
                    total_lines.append(f"{idx}. `Unknown` — `{user.score}`")

            # 合并消息
            msg = textwrap.dedent(f"""\
                📊 **每日积分结算报告**
                
                今日共发放 **{result.total_score_settled}** 非签到活跃积分。
                
                """)
            msg += "\n".join(daily_lines) + "\n\n"
            msg += "\n".join(total_lines)

            await client.send_message(settings.telegram_chat_id, msg)

        except Exception as e:
            logger.exception("结算用户积分时出错: {}", e)
            await session.rollback()
        finally:
            await session.close()
