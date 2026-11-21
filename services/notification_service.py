import re
from typing import Any

from fastapi import FastAPI
from loguru import logger
from telethon.errors import RPCError

from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from core.telegram_message import TelegramMessage
from models.events import NotificationEvent

settings = get_settings()

# 收敛模板条件片段产生的多余空行
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """兼容字典与模型对象安全读取字段，避免字符串或缺失属性触发 AttributeError。"""
    if obj is None or isinstance(obj, (str, bytes, int, float, bool)):
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _truncate_text(value: Any, limit: int) -> str:
    """将文本安全裁剪至指定长度并附加省略号。"""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _collapse_blank_lines(text: str) -> str:
    """收敛连续三个及以上的换行符为单个空行并去除首尾空白。"""
    return _MULTI_NEWLINE_RE.sub("\n\n", text).strip()


def _render_custom_format_html(cf_info: Any) -> str:
    """将 Arr 自定义格式评分与命中名称列表安全渲染为 HTML 片段。"""
    if not cf_info:
        return ""
    lines: list[str] = []
    score = _get_field(cf_info, "customFormatScore")
    if score is not None and score != 0:
        lines.append(TelegramMessage.render_html(t"分数: <b>{score}</b>"))
    formats = _get_field(cf_info, "customFormats") or []
    if formats:
        chips = " ".join(
            TelegramMessage.render_html(t"<code>{_get_field(cf, 'name', cf)}</code>")
            for cf in formats
        )
        lines.append(f"格式: {chips}")
    return "\n".join(lines)


def _render_library_new_html(
    *,
    server_name: Any,
    series_name: Any,
    name: Any,
    year: Any,
    media_type: Any,
    overview: Any,
) -> str:
    """统一渲染 Emby / Jellyfin 媒体入库通知的 Telegram HTML。"""
    series_line = (
        TelegramMessage.render_html(t"剧集: <b>{series_name}</b>")
        if series_name
        else ""
    )
    year_part = TelegramMessage.render_html(t" ({year})") if year else ""
    title_line = (
        TelegramMessage.render_html(t"标题: <b>{name}</b>{year_part:raw}")
        if name
        else ""
    )
    type_line = TelegramMessage.render_html(t"类型: {media_type}") if media_type else ""
    overview_short = _truncate_text(overview, 120)
    overview_block = (
        TelegramMessage.render_html(t"\n描述:\n{overview_short}")
        if overview_short
        else ""
    )
    body = TelegramMessage.render_html(
        t"🆕 <b>媒体库更新</b>\n\n"
        t"{series_line:raw}\n"
        t"{title_line:raw}\n"
        t"{type_line:raw}\n"
        t"{overview_block:raw}\n\n"
        t"来源: <code>{server_name}</code>\n\n"
        t"#入库"
    )
    return _collapse_blank_lines(body)


class NotificationService:
    def __init__(self, app: FastAPI):
        """初始化通知服务"""
        self.app = app
        self.client: TelethonClientWarper = app.state.telethon_client

    async def _render(
        self, event_type: NotificationEvent, context: dict[str, Any]
    ) -> str | None:
        """内部方法：使用标准库 t-string 渲染器将通知事件转换为安全的 Telegram HTML。"""
        server_name = context.get("server_name", "")

        if event_type == NotificationEvent.REQUEST_SUBMIT:
            user_id = context.get("user_id", "")
            user_name = context.get("user_name", "")
            media_title = context.get("media_title", "")
            media_year = context.get("media_year", "")
            overview_short = _truncate_text(context.get("overview", ""), 100)
            prefix = context.get("prefix", "")
            tmdb_id = context.get("tmdb_id", "")
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"🎬 <b>收到新的求片请求</b>\n\n"
                    t'👤 <b>申请人</b>: <code>(<a href="tg://user?id={user_id}">{user_name}</a>)</code>\n'
                    t"🎬 <b>标题</b>: {media_title} ({media_year})\n"
                    t"🖥️ <b>服务器</b>: {server_name}\n"
                    t"📝 <b>简介</b>: {overview_short}\n\n"
                    t"{prefix}: {tmdb_id}\n\n"
                    t"#求片 #待审核"
                )
            )

        if event_type == NotificationEvent.REQUEST_APPROVED:
            media_title = context.get("media_title", "")
            media_year = context.get("media_year", "")
            return TelegramMessage.render_html(
                t"✅ <b>求片已批准</b>\n\n"
                t"您请求的 <b>{media_title}</b> ({media_year}) 已通过审核。\n"
                t"服务器: <code>{server_name}</code>\n\n"
                t"系统已自动将其加入下载队列，请耐心等待下载完成。"
            )

        if event_type == NotificationEvent.REQUEST_REJECTED:
            media_title = context.get("media_title", "")
            media_year = context.get("media_year", "")
            reason = context.get("reason", "")
            return TelegramMessage.render_html(
                t"❌ <b>求片被拒绝</b>\n\n"
                t"很抱歉，您请求的 <b>{media_title}</b> ({media_year}) 未能通过审核。\n"
                t"服务器: <code>{server_name}</code>\n\n"
                t"<b>拒绝原因:</b>\n"
                t"{reason}\n\n"
                t"如有疑问，请联系管理员。"
            )

        if event_type == NotificationEvent.REQUEST_COMPLETED:
            media_title = context.get("media_title", "")
            media_year = context.get("media_year", "")
            return TelegramMessage.render_html(
                t"🎉 <b>影片已入库！</b>\n\n"
                t"<b>{media_title}</b> ({media_year}) 现已就绪。\n"
                t"服务器: <code>{server_name}</code>\n\n"
                t"祝您观影愉快！"
            )

        if event_type == NotificationEvent.SONARR_DOWNLOAD:
            series = context.get("series")
            series_title = _get_field(series, "title", "")
            episodes = context.get("episodes") or []
            ep_lines = "\n".join(
                TelegramMessage.render_html(
                    t"单集: S{_get_field(ep, 'seasonNumber', '')}E{_get_field(ep, 'episodeNumber', '')} - {_get_field(ep, 'title', '')}"
                )
                for ep in episodes
            )
            quality = _get_field(context.get("episodeFile"), "quality", "")
            cf_block = _render_custom_format_html(context.get("customFormatInfo"))
            upgrade_line = "✨ <b>画质洗版升级</b>" if context.get("isUpgrade") else ""
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"📥 <b>剧集下载完成</b>\n\n"
                    t"剧集: <b>{series_title}</b>\n"
                    t"{ep_lines:raw}\n"
                    t"质量: {quality}\n\n"
                    t"{cf_block:raw}\n\n"
                    t"{upgrade_line:raw}\n"
                    t"来源: <code>{server_name}</code>\n\n"
                    t"#Sonarr #下载完成"
                )
            )

        if event_type == NotificationEvent.SONARR_SERIES_ADD:
            series = context.get("series")
            title = _get_field(series, "title", "")
            year = _get_field(series, "year", "")
            genres = _get_field(series, "genres") or []
            genres_line = (
                TelegramMessage.render_html(
                    t"类型: {', '.join(str(g) for g in genres)}"
                )
                if genres
                else ""
            )
            series_path = _get_field(series, "path", "")
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"📺 <b>新增订阅剧集</b>\n\n"
                    t"标题: <b>{title}</b> ({year})\n"
                    t"{genres_line:raw}\n"
                    t"路径: <code>{series_path}</code>\n"
                    t"来源: <code>{server_name}</code>\n\n"
                    t"#Sonarr #新订阅"
                )
            )

        if event_type == NotificationEvent.RADARR_DOWNLOAD:
            movie = context.get("movie")
            title = _get_field(movie, "title", "")
            year = _get_field(movie, "year", "")
            quality = _get_field(context.get("movieFile"), "quality", "")
            cf_block = _render_custom_format_html(context.get("customFormatInfo"))
            upgrade_line = "✨ <b>画质洗版升级</b>" if context.get("isUpgrade") else ""
            overview_short = _truncate_text(_get_field(movie, "overview"), 80)
            overview_line = (
                TelegramMessage.render_html(t"简介: {overview_short}")
                if overview_short
                else ""
            )
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"📥 <b>电影下载完成</b>\n\n"
                    t"标题: <b>{title}</b> ({year})\n"
                    t"质量: {quality}\n\n"
                    t"{cf_block:raw}\n\n"
                    t"{upgrade_line:raw}\n"
                    t"{overview_line:raw}\n"
                    t"来源: <code>{server_name}</code>\n\n"
                    t"#Radarr #下载完成"
                )
            )

        if event_type == NotificationEvent.RADARR_MOVIE_ADD:
            movie = context.get("movie")
            title = _get_field(movie, "title", "")
            year = _get_field(movie, "year", "")
            genres = _get_field(movie, "genres") or []
            genres_line = (
                TelegramMessage.render_html(
                    t"类型: {', '.join(str(g) for g in genres)}"
                )
                if genres
                else ""
            )
            overview_short = _truncate_text(_get_field(movie, "overview"), 100)
            overview_line = (
                TelegramMessage.render_html(t"简介: {overview_short}")
                if overview_short
                else ""
            )
            tmdb_id = _get_field(movie, "tmdbId", "")
            return _collapse_blank_lines(
                TelegramMessage.render_html(
                    t"🎬 <b>新增订阅电影</b>\n\n"
                    t"标题: <b>{title}</b> ({year})\n"
                    t"{genres_line:raw}\n"
                    t"{overview_line:raw}\n"
                    t"TMDB ID: <code>{tmdb_id}</code>\n"
                    t"来源: <code>{server_name}</code>\n\n"
                    t"#Radarr #新增订阅"
                )
            )

        if event_type == NotificationEvent.EMBY_LIBRARY_NEW:
            item = context.get("item")
            return _render_library_new_html(
                server_name=server_name,
                series_name=_get_field(item, "series_name"),
                name=_get_field(item, "name", ""),
                year=_get_field(item, "production_year"),
                media_type=_get_field(item, "type", ""),
                overview=_get_field(item, "overview"),
            )

        if event_type == NotificationEvent.JELLYFIN_LIBRARY_NEW:
            item = context.get("item")
            # 兼容当前 webhooks.py 传入 item_id 字符串的历史调用方式
            fallback_name = item if isinstance(item, str) else ""
            return _render_library_new_html(
                server_name=server_name,
                series_name=_get_field(item, "series_name"),
                name=_get_field(item, "name", fallback_name),
                year=_get_field(item, "year"),
                media_type=_get_field(item, "item_type", ""),
                overview=_get_field(item, "overview"),
            )

        if event_type == NotificationEvent.SYSTEM_TEST:
            return TelegramMessage.render_html(
                t"🔔 <b>系统通知测试</b>\n\n来源: <code>{server_name}</code>"
            )

        return None

    async def send_to_topic(
        self,
        topic_id: int,
        event_type: NotificationEvent,
        image: Any = None,
        buttons: list | None = None,
        **kwargs,
    ) -> Any:
        """
        发送通知到指定的 Topic 或 Channel (通用)

        Args:
            topic_id: 目标 ID (从 Server 配置中读取传入)
                      < 0: 直接发送到该 ID (作为 Channel/Group ID)
                      > 0: 发送到主群 (settings.telegram_chat_id) 并 Reply 该 Topic ID
            event_type: 事件类型 (决定渲染格式)
            image: 图片/海报
            buttons: 按钮
            **kwargs: 模板变量 (需包含 server_name 等)
        """
        if not topic_id:
            return None

        context = kwargs.copy()
        context["event_type"] = event_type.value

        message_text = await self._render(event_type, context)
        if not message_text:
            return None

        target_chat = settings.telegram_chat_id
        target_reply = None

        if topic_id < 0:
            target_chat = topic_id
            target_reply = None
        elif topic_id > 0:
            target_chat = settings.telegram_chat_id
            target_reply = topic_id

        try:
            sent_msg = await self.client.send_message(
                chat_id=target_chat,
                message=message_text,
                file=image,
                buttons=buttons,
                reply_to=target_reply,
                parse_mode="html",
                link_preview=False,
            )
            logger.info("已发送通知：{}（目标：{}）", event_type.value, topic_id)
            return sent_msg
        except RPCError as e:
            logger.error("Telegram API 错误: {}", e)
            return None
        except (ValueError, TypeError, OSError) as e:
            logger.error("无法发送通知 ({})：{}", event_type.value, e)
            return None

    async def send_to_user(
        self, user_id: int, event_type: NotificationEvent, image: Any = None, **kwargs
    ) -> bool:
        """发送模板通知给特定用户 (私聊)，返回是否发送成功"""
        message_text = await self._render(event_type, kwargs)
        if not message_text:
            logger.warning(
                "用户私聊通知模板渲染为空: user_id={}, event={}",
                user_id,
                event_type.value,
            )
            return False

        try:
            await self.client.send_message(
                chat_id=user_id, message=message_text, file=image, parse_mode="html"
            )
            logger.debug("发送的用户通知：{} -> 用户 {}", event_type.value, user_id)
            return True
        except (RPCError, ValueError, TypeError, OSError) as e:
            logger.error(
                "无法向 {} 发送用户通知 ({})：{}", user_id, event_type.value, e
            )
            return False

    async def send_text_to_user(
        self, user_id: int, message_text: str, image: Any = None
    ) -> bool:
        """发送自定义文本通知给特定用户 (私聊)，返回是否发送成功"""
        if not message_text:
            return False

        try:
            await self.client.send_message(
                chat_id=user_id, message=message_text, file=image, parse_mode="html"
            )
            logger.debug("发送的用户文本通知 -> 用户 {}", user_id)
            return True
        except (RPCError, ValueError, TypeError, OSError) as e:
            logger.error("无法向 {} 发送用户文本通知：{}", user_id, e)
            return False
