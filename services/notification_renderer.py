"""统一强类型通知载荷定义与 TelegramMessage t-string 双端统一渲染器"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from telethon import Button

from core.economy_policy import (
    evaluate_traffic_pack_access,
    is_traffic_auto_suspend_effective,
    is_traffic_effective,
    is_traffic_quota_limited,
    is_watch_time_effective,
    is_watch_time_renewal_open,
)
from core.telegram_message import TelegramMessage
from models.emby import BaseItemDto as EmbyApiBaseItemDto
from models.emby_webhook import BaseItemDto as EmbyWebhookBaseItemDto
from models.events import NotificationEvent
from models.jellyfin_webhook import ItemEvent as JellyfinItemEvent
from models.nginx import format_traffic_bytes
from models.radarr_webhook import (
    CustomFormatInfoDto,
    MediaCover,
    RadarrWebhookAddedPayload,
    RadarrWebhookDownloadPayload,
    RadarrWebhookGrabPayload,
    RadarrWebhookHealthPayload,
    RadarrWebhookManualInteractionPayload,
    RadarrWebhookMovieDeletePayload,
    RadarrWebhookMovieFileDeletePayload,
    RadarrWebhookRenamePayload,
    RadarrWebhookTestPayload,
)
from models.sonarr_webhook import (
    SonarrEpisode,
    SonarrWebhookDownloadPayload,
    SonarrWebhookEpisodeDeletePayload,
    SonarrWebhookGrabPayload,
    SonarrWebhookHealthPayload,
    SonarrWebhookManualInteractionPayload,
    SonarrWebhookRenamePayload,
    SonarrWebhookSeriesAddPayload,
    SonarrWebhookSeriesDeletePayload,
    SonarrWebhookTestPayload,
)
from repositories.config_repo import ConfigRepository

if TYPE_CHECKING:
    from models.orm import ServerInstance

# 仅匹配 Telegram HTML 支持的格式标签，避免将业务文本中已反转义的 `<...>` 误当成标签剥离
_TELEGRAM_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler|a)(?:\s+[^>]*)?>",
    re.IGNORECASE,
)


def _truncate_text(value: Any, limit: int) -> str:
    """将文本安全裁剪至指定长度并附加省略号。"""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _telegram_html_to_plain(html_text: str) -> str:
    """将安全的 Telegram HTML 文本转换为供站内收件箱与运维日志展示的纯文本摘要。"""
    stripped = _TELEGRAM_TAG_RE.sub("", html_text)
    return TelegramMessage.collapse_blank_lines(html.unescape(stripped))


@dataclass(frozen=True, slots=True)
class EffectiveEconomyContext:
    """遵循 ADR-0004 的三体系有效经济开关快照（供渲染器按有效态裁剪文案）。"""

    points_enabled: bool = True
    points_renew_enabled: bool = True
    points_request_enabled: bool = True
    points_traffic_pack_enabled: bool = True
    points_warn_enabled: bool = True
    points_spam_enabled: bool = True
    points_checkin_enabled: bool = True
    points_chat_enabled: bool = True
    points_register_enabled: bool = True
    points_generate_code_enabled: bool = True
    watch_time_enabled: bool = True
    watch_time_renewal_open: bool = False
    traffic_enabled: bool = False
    traffic_limited: bool | None = None
    traffic_auto_suspend_enabled: bool = False
    traffic_pack_open: bool = False

    @property
    def effective_points_renew(self) -> bool:
        """积分续期是否有效开放（受全局积分总开关一票否决）。"""
        return bool(self.points_enabled and self.points_renew_enabled)

    @property
    def effective_points_request(self) -> bool:
        """积分求片是否有效开放（受全局积分总开关一票否决）。"""
        return bool(self.points_enabled and self.points_request_enabled)

    @property
    def effective_points_traffic_pack(self) -> bool:
        """积分兑换流量包子开关是否有效开放（受全局积分总开关一票否决）。"""
        return bool(self.points_enabled and self.points_traffic_pack_enabled)

    @property
    def effective_points_warn(self) -> bool:
        """管理员警告连带扣分是否有效开放（受全局积分总开关一票否决）。"""
        return bool(self.points_enabled and self.points_warn_enabled)

    @property
    def effective_points_spam(self) -> bool:
        """刷屏连带扣分是否有效开放（受全局积分总开关一票否决）。"""
        return bool(self.points_enabled and self.points_spam_enabled)

    @property
    def effective_points_register(self) -> bool:
        """积分自注册是否有效开放（受全局积分总开关一票否决）。"""
        return bool(self.points_enabled and self.points_register_enabled)

    @property
    def effective_points_generate_code(self) -> bool:
        """积分生成激活码是否有效开放（受全局积分总开关一票否决）。"""
        return bool(self.points_enabled and self.points_generate_code_enabled)

    @property
    def effective_watch_time_renewal(self) -> bool:
        """时长保活续期是否有效开放（受有效播放时长总开关一票否决）。"""
        return bool(self.watch_time_enabled and self.watch_time_renewal_open)

    @property
    def effective_traffic_pack(self) -> bool:
        """当前服务器是否允许展示与兑换当期流量包（积分 ∩ 有限流 ∩ 单服买包）。"""
        limited_ok = True if self.traffic_limited is None else self.traffic_limited
        return bool(
            self.effective_points_traffic_pack
            and self.traffic_enabled
            and limited_ok
            and self.traffic_pack_open
        )

    @classmethod
    def from_cache(
        cls,
        server: ServerInstance | None = None,
    ) -> EffectiveEconomyContext:
        """从全局配置缓存与可选目标服务器实例构造遵循 ADR-0004 的有效经济上下文。

        Args:
            server: 可选的目标媒体服务器实例；缺省时仅依据全局开关构建基础视图。

        Returns:
            计算完成一票否决后的 ``EffectiveEconomyContext`` 实例。
        """
        points_enabled = ConfigRepository.is_points_enabled_from_cache()
        points_pack = ConfigRepository.is_points_feature_enabled_from_cache(
            ConfigRepository.KEY_ENABLE_POINTS_TRAFFIC_PACK
        )
        global_watch = ConfigRepository.is_watch_time_enabled_from_cache()
        global_traffic = ConfigRepository.is_traffic_enabled_from_cache()

        watch_time_effective = (
            is_watch_time_effective(global_watch, server)
            if server is not None
            else global_watch
        )
        watch_renewal_open = (
            is_watch_time_renewal_open(global_watch, server)
            if server is not None
            else False
        )
        traffic_effective = (
            is_traffic_effective(global_traffic, server)
            if server is not None
            else global_traffic
        )
        traffic_limited = (
            is_traffic_quota_limited(global_traffic, server)
            if server is not None
            else None
        )
        traffic_auto_suspend = (
            is_traffic_auto_suspend_effective(global_traffic, server)
            if server is not None
            else False
        )
        traffic_pack_open = (
            evaluate_traffic_pack_access(
                server,
                points_pack_allowed=points_pack,
                points_pack_reason="",
                global_traffic_enabled=global_traffic,
            )[0]
            if server is not None
            else False
        )

        return cls(
            points_enabled=points_enabled,
            points_renew_enabled=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_RENEW
            ),
            points_request_enabled=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_REQUEST
            ),
            points_traffic_pack_enabled=points_pack,
            points_warn_enabled=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_WARN
            ),
            points_spam_enabled=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_SPAM
            ),
            points_checkin_enabled=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_CHECKIN
            ),
            points_chat_enabled=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_CHAT
            ),
            points_register_enabled=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_REGISTER
            ),
            points_generate_code_enabled=ConfigRepository.is_points_feature_enabled_from_cache(
                ConfigRepository.KEY_ENABLE_POINTS_GENERATE_CODE
            ),
            watch_time_enabled=watch_time_effective,
            watch_time_renewal_open=watch_renewal_open,
            traffic_enabled=traffic_effective,
            traffic_limited=traffic_limited,
            traffic_auto_suspend_enabled=traffic_auto_suspend,
            traffic_pack_open=traffic_pack_open,
        )


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    """统一通知渲染产物，同时覆盖 Telegram 出站消息与站内/运维摘要展示。"""

    event: NotificationEvent
    title: str
    plain_summary: str
    telegram_html: str
    buttons: list[Any] | None = None
    image: Any = None
    action_url: str | None = None
    dedupe_hint: str | None = None


def _make_rendered(
    event: NotificationEvent,
    *,
    title: str,
    telegram_html: str,
    plain_summary: str | None = None,
    buttons: list[Any] | None = None,
    image: Any = None,
    action_url: str | None = None,
    dedupe_hint: str | None = None,
) -> RenderedNotification:
    """统一标准化换行、裁剪标题长度并生成安全双端通知输出。"""
    normalized_html = TelegramMessage.collapse_blank_lines(telegram_html)
    normalized_summary = (
        TelegramMessage.collapse_blank_lines(plain_summary)
        if plain_summary is not None
        else _telegram_html_to_plain(normalized_html)
    )
    safe_title = _truncate_text(title, 200) or event.value
    return RenderedNotification(
        event=event,
        title=safe_title,
        plain_summary=normalized_summary,
        telegram_html=normalized_html,
        buttons=buttons,
        image=image,
        action_url=action_url,
        dedupe_hint=dedupe_hint,
    )


def extract_arr_poster_url(
    images: Sequence[MediaCover | Mapping[str, Any]],
) -> str | None:
    """从 Sonarr/Radarr 图片列表中优先提取合法的远程海报 URL。"""
    for img in images:
        if isinstance(img, Mapping):
            cover_type = str(img.get("coverType") or "")
            remote_url = img.get("remoteUrl")
            local_url = img.get("url")
        else:
            cover_type = str(img.coverType or "")
            remote_url = img.remoteUrl
            local_url = img.url
        if cover_type.lower() != "poster":
            continue
        if isinstance(remote_url, str) and remote_url.strip():
            return remote_url.strip()
        if isinstance(local_url, str) and local_url.strip().startswith(
            ("https://", "http://")
        ):
            return local_url.strip()
    return None


@dataclass(frozen=True, slots=True)
class EpisodeDetail:
    """标准化剧集单集描述结构。"""

    season_number: int
    episode_number: int
    title: str = ""


def _normalize_episodes(
    episodes: Sequence[SonarrEpisode | EpisodeDetail | Mapping[str, Any]],
) -> list[EpisodeDetail]:
    """将 Sonarr 模型、字典或标准结构统一归一化为 ``EpisodeDetail`` 列表。"""
    normalized: list[EpisodeDetail] = []
    for ep in episodes:
        if isinstance(ep, EpisodeDetail):
            normalized.append(ep)
        elif isinstance(ep, SonarrEpisode):
            normalized.append(
                EpisodeDetail(
                    season_number=ep.seasonNumber,
                    episode_number=ep.episodeNumber,
                    title=ep.title or "",
                )
            )
        else:
            normalized.append(
                EpisodeDetail(
                    season_number=int(
                        ep.get("season_number") or ep.get("seasonNumber") or 0
                    ),
                    episode_number=int(
                        ep.get("episode_number") or ep.get("episodeNumber") or 0
                    ),
                    title=str(ep.get("title") or ""),
                )
            )
    return normalized


def _extract_custom_format_fields(
    cf_info: CustomFormatInfoDto | None,
) -> tuple[int | None, tuple[str, ...]]:
    """从 Arr 自定义格式模型提取评分与格式名称元组。"""
    if cf_info is None:
        return None, ()
    names = tuple(
        cf.name.strip() for cf in cf_info.customFormats if cf.name and cf.name.strip()
    )
    return cf_info.customFormatScore, names


def _format_year_part(year: int | str | None) -> str:
    """格式化有效年份括号后缀；为空或 0 时返回空字符串。"""
    if year is None:
        return ""
    text = str(year).strip()
    if not text or text == "0":
        return ""
    return f" ({text})"


def _format_episodes_html(episodes: list[EpisodeDetail], *, max_items: int = 8) -> str:
    """安全渲染剧集单集列表 HTML 片段，超过上限时附带汇总提示。"""
    if not episodes:
        return ""
    lines: list[str] = []
    for ep in episodes[:max_items]:
        code = f"S{ep.season_number:02d}E{ep.episode_number:02d}"
        if ep.title.strip():
            lines.append(
                TelegramMessage.render_html(t"单集: <code>{code}</code> - {ep.title}")
            )
        else:
            lines.append(TelegramMessage.render_html(t"单集: <code>{code}</code>"))
    if len(episodes) > max_items:
        lines.append(
            TelegramMessage.render_html(
                t"其余 {len(episodes) - max_items} 集已省略（共 {len(episodes)} 集）"
            )
        )
    return "\n".join(lines)


def _render_custom_format_html(score: int | None, formats: Sequence[str]) -> str:
    """将 Arr 自定义格式评分与命中名称安全渲染为 HTML 片段。"""
    lines: list[str] = []
    if score is not None and score != 0:
        lines.append(TelegramMessage.render_html(t"格式评分: <b>{score}</b>"))
    cleaned = [str(f).strip() for f in formats if str(f).strip()]
    if cleaned:
        chips = " ".join(
            TelegramMessage.render_html(t"<code>{name}</code>") for name in cleaned
        )
        lines.append(f"命中格式: {chips}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SonarrDownloadPayload:
    """Sonarr 剧集下载/导入完成载荷（SONARR_DOWNLOAD）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.SONARR_DOWNLOAD

    server_name: str
    series_title: str
    episodes: Sequence[SonarrEpisode | EpisodeDetail | Mapping[str, Any]] = ()
    quality: str = ""
    custom_format_score: int | None = None
    custom_formats: Sequence[str] = ()
    is_upgrade: bool = False
    size_bytes: int | None = None
    release_group: str = ""
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookDownloadPayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> SonarrDownloadPayload:
        """从 ``SonarrWebhookDownloadPayload`` 构造强类型渲染载荷。"""
        primary_file = payload.episodeFile or (
            payload.episodeFiles[0] if payload.episodeFiles else None
        )
        total_size = (
            sum(f.size for f in payload.episodeFiles)
            if payload.episodeFiles
            else (primary_file.size if primary_file is not None else None)
        )
        cf_score, cf_names = _extract_custom_format_fields(payload.customFormatInfo)
        return cls(
            server_name=server_name or payload.instanceName or "Sonarr",
            series_title=payload.series.title,
            episodes=payload.episodes,
            quality=primary_file.quality if primary_file is not None else "",
            custom_format_score=cf_score,
            custom_formats=cf_names,
            is_upgrade=bool(payload.isUpgrade),
            size_bytes=total_size,
            release_group=(primary_file.releaseGroup or "") if primary_file else "",
            images=payload.series.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class SonarrSeriesAddPayload:
    """Sonarr 新增订阅剧集载荷（SONARR_SERIES_ADD）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.SONARR_SERIES_ADD

    server_name: str
    title: str
    year: int | None = None
    genres: Sequence[str] = ()
    path: str = ""
    tvdb_id: int | None = None
    overview: str | None = None
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookSeriesAddPayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> SonarrSeriesAddPayload:
        """从 ``SonarrWebhookSeriesAddPayload`` 构造强类型渲染载荷。"""
        return cls(
            server_name=server_name or payload.instanceName or "Sonarr",
            title=payload.series.title,
            year=payload.series.year or None,
            genres=payload.series.genres,
            path=payload.series.path,
            tvdb_id=payload.series.tvdbId or None,
            overview=payload.series.overview,
            images=payload.series.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class SonarrGrabPayload:
    """Sonarr 剧集资源抓取载荷（SONARR_GRAB）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.SONARR_GRAB

    server_name: str
    series_title: str
    episodes: Sequence[SonarrEpisode | EpisodeDetail | Mapping[str, Any]] = ()
    release_title: str = ""
    quality: str = ""
    indexer: str = ""
    size_bytes: int | None = None
    download_client: str = ""
    custom_format_score: int | None = None
    custom_formats: Sequence[str] = ()
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookGrabPayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> SonarrGrabPayload:
        """从 ``SonarrWebhookGrabPayload`` 构造强类型渲染载荷。"""
        cf_score, cf_names = _extract_custom_format_fields(payload.customFormatInfo)
        return cls(
            server_name=server_name or payload.instanceName or "Sonarr",
            series_title=payload.series.title,
            episodes=payload.episodes,
            release_title=payload.release.releaseTitle or "",
            quality=payload.release.quality or "",
            indexer=payload.release.indexer or "",
            size_bytes=payload.release.size,
            download_client=payload.downloadClient or "",
            custom_format_score=cf_score
            if cf_score is not None
            else payload.release.customFormatScore,
            custom_formats=cf_names or tuple(payload.release.customFormats),
            images=payload.series.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class SonarrRenamePayload:
    """Sonarr 剧集文件重命名载荷（SONARR_RENAME）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.SONARR_RENAME

    server_name: str
    series_title: str
    renamed_count: int = 0
    renamed_files: Sequence[str] = ()
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookRenamePayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> SonarrRenamePayload:
        """从 ``SonarrWebhookRenamePayload`` 构造强类型渲染载荷。"""
        files = [f.relativePath for f in payload.renamedEpisodeFiles]
        return cls(
            server_name=server_name or payload.instanceName or "Sonarr",
            series_title=payload.series.title,
            renamed_count=len(files),
            renamed_files=files,
            images=payload.series.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class SonarrDeletePayload:
    """Sonarr 剧集或剧集文件删除载荷（SONARR_DELETE）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.SONARR_DELETE

    server_name: str
    series_title: str
    delete_scope: Literal["series", "episode_file"] = "series"
    episodes: Sequence[SonarrEpisode | EpisodeDetail | Mapping[str, Any]] = ()
    file_path: str = ""
    delete_reason: str = ""
    deleted_files: bool = False
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookSeriesDeletePayload | SonarrWebhookEpisodeDeletePayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> SonarrDeletePayload:
        """从 Sonarr 删除 Webhook 载荷构造强类型渲染载荷。"""
        base_server = server_name or payload.instanceName or "Sonarr"
        if isinstance(payload, SonarrWebhookEpisodeDeletePayload):
            return cls(
                server_name=base_server,
                series_title=payload.series.title,
                delete_scope="episode_file",
                episodes=payload.episodes,
                file_path=payload.episodeFile.relativePath or payload.episodeFile.path,
                delete_reason=payload.deleteReason,
                deleted_files=True,
                images=payload.series.images,
                image=image,
            )
        return cls(
            server_name=base_server,
            series_title=payload.series.title,
            delete_scope="series",
            deleted_files=payload.deletedFiles,
            images=payload.series.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class RadarrDownloadPayload:
    """Radarr 电影下载/升级入库载荷（RADARR_DOWNLOAD）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.RADARR_DOWNLOAD

    server_name: str
    title: str
    year: int | None = None
    quality: str = ""
    size_bytes: int | None = None
    custom_format_score: int | None = None
    custom_formats: Sequence[str] = ()
    is_upgrade: bool = False
    overview: str | None = None
    tmdb_id: int | None = None
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: RadarrWebhookDownloadPayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> RadarrDownloadPayload:
        """从 ``RadarrWebhookDownloadPayload`` 构造强类型渲染载荷。"""
        cf_score, cf_names = _extract_custom_format_fields(payload.customFormatInfo)
        return cls(
            server_name=server_name or payload.instanceName or "Radarr",
            title=payload.movie.title,
            year=payload.movie.year or None,
            quality=payload.movieFile.quality,
            size_bytes=payload.movieFile.size,
            custom_format_score=cf_score,
            custom_formats=cf_names,
            is_upgrade=bool(payload.isUpgrade),
            overview=payload.movie.overview,
            tmdb_id=payload.movie.tmdbId or None,
            images=payload.movie.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class RadarrMovieAddPayload:
    """Radarr 新增订阅电影载荷（RADARR_MOVIE_ADD）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.RADARR_MOVIE_ADD

    server_name: str
    title: str
    year: int | None = None
    genres: Sequence[str] = ()
    overview: str | None = None
    tmdb_id: int | None = None
    add_method: str = ""
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: RadarrWebhookAddedPayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> RadarrMovieAddPayload:
        """从 ``RadarrWebhookAddedPayload`` 构造强类型渲染载荷。"""
        return cls(
            server_name=server_name or payload.instanceName or "Radarr",
            title=payload.movie.title,
            year=payload.movie.year or None,
            genres=payload.movie.genres,
            overview=payload.movie.overview,
            tmdb_id=payload.movie.tmdbId or None,
            add_method=payload.addMethod,
            images=payload.movie.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class RadarrGrabPayload:
    """Radarr 电影资源抓取载荷（RADARR_GRAB）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.RADARR_GRAB

    server_name: str
    title: str
    year: int | None = None
    release_title: str = ""
    quality: str = ""
    indexer: str = ""
    size_bytes: int | None = None
    download_client: str = ""
    custom_format_score: int | None = None
    custom_formats: Sequence[str] = ()
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: RadarrWebhookGrabPayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> RadarrGrabPayload:
        """从 ``RadarrWebhookGrabPayload`` 构造强类型渲染载荷。"""
        cf_score, cf_names = _extract_custom_format_fields(payload.customFormatInfo)
        return cls(
            server_name=server_name or payload.instanceName or "Radarr",
            title=payload.movie.title,
            year=payload.movie.year or payload.remoteMovie.year or None,
            release_title=payload.release.releaseTitle or "",
            quality=payload.release.quality or "",
            indexer=payload.release.indexer or "",
            size_bytes=payload.release.size,
            download_client=payload.downloadClient or "",
            custom_format_score=cf_score
            if cf_score is not None
            else payload.release.customFormatScore,
            custom_formats=cf_names or tuple(payload.release.customFormats),
            images=payload.movie.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class RadarrRenamePayload:
    """Radarr 电影文件重命名载荷（RADARR_RENAME）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.RADARR_RENAME

    server_name: str
    title: str
    year: int | None = None
    renamed_count: int = 0
    renamed_files: Sequence[str] = ()
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: RadarrWebhookRenamePayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> RadarrRenamePayload:
        """从 ``RadarrWebhookRenamePayload`` 构造强类型渲染载荷。"""
        files = [f.relativePath for f in payload.renamedMovieFiles]
        return cls(
            server_name=server_name or payload.instanceName or "Radarr",
            title=payload.movie.title,
            year=payload.movie.year or None,
            renamed_count=len(files),
            renamed_files=files,
            images=payload.movie.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class RadarrDeletePayload:
    """Radarr 电影或电影文件删除载荷（RADARR_DELETE）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.RADARR_DELETE

    server_name: str
    title: str
    year: int | None = None
    delete_scope: Literal["movie", "movie_file"] = "movie"
    file_path: str = ""
    delete_reason: str = ""
    deleted_files: bool = False
    images: Sequence[MediaCover] = ()
    image: Any = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: RadarrWebhookMovieDeletePayload | RadarrWebhookMovieFileDeletePayload,
        *,
        server_name: str = "",
        image: Any = None,
    ) -> RadarrDeletePayload:
        """从 Radarr 删除 Webhook 载荷构造强类型渲染载荷。"""
        base_server = server_name or payload.instanceName or "Radarr"
        if isinstance(payload, RadarrWebhookMovieFileDeletePayload):
            return cls(
                server_name=base_server,
                title=payload.movie.title,
                year=payload.movie.year or None,
                delete_scope="movie_file",
                file_path=payload.movieFile.relativePath or payload.movieFile.path,
                delete_reason=payload.deleteReason,
                deleted_files=True,
                images=payload.movie.images,
                image=image,
            )
        return cls(
            server_name=base_server,
            title=payload.movie.title,
            year=payload.movie.year or None,
            delete_scope="movie",
            deleted_files=payload.deletedFiles,
            images=payload.movie.images,
            image=image,
        )


@dataclass(frozen=True, slots=True)
class EmbyLibraryNewPayload:
    """Emby 媒体库新入库通知载荷（EMBY_LIBRARY_NEW）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.EMBY_LIBRARY_NEW

    server_name: str
    item: EmbyWebhookBaseItemDto | EmbyApiBaseItemDto | None = None
    name: str = ""
    series_name: str | None = None
    year: int | None = None
    item_type: str = ""
    overview: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    tmdb_id: str | None = None
    image: Any = None
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class JellyfinLibraryNewPayload:
    """Jellyfin 媒体库新入库通知载荷（JELLYFIN_LIBRARY_NEW，完整支持 ItemEvent）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.JELLYFIN_LIBRARY_NEW

    server_name: str = ""
    item: JellyfinItemEvent | None = None
    name: str = ""
    series_name: str | None = None
    year: int | None = None
    item_type: str = ""
    overview: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    image: Any = None
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class MediaLibraryDeletedPayload:
    """Emby / Jellyfin 媒体项删除事件载荷（MEDIA_LIBRARY_DELETED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MEDIA_LIBRARY_DELETED

    server_name: str
    name: str
    series_name: str | None = None
    year: int | None = None
    item_type: str = ""
    item_id: str | None = None
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class RequestSubmitPayload:
    """管理员求片审核卡片载荷（REQUEST_SUBMIT）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.REQUEST_SUBMIT

    server_name: str
    user_id: int
    user_name: str
    media_title: str
    media_year: int | str | None = None
    media_type: str = "TMDB"
    media_id: int | str = ""
    overview: str | None = None
    request_id: int | None = None
    reserved_cost: int = 0
    image: Any = None
    poster_url: str | None = None
    buttons: list[Any] | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class RequestApprovedPayload:
    """求片审核通过通知载荷（REQUEST_APPROVED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.REQUEST_APPROVED

    server_name: str
    media_title: str
    media_year: int | str | None = None
    approver_name: str | None = None
    reserved_cost: int = 0
    request_id: int | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class RequestRejectedPayload:
    """求片审核拒绝通知载荷（REQUEST_REJECTED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.REQUEST_REJECTED

    server_name: str
    media_title: str
    media_year: int | str | None = None
    reason: str = ""
    approver_name: str | None = None
    reserved_cost: int = 0
    request_id: int | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class RequestCompletedPayload:
    """求片真入库完结通知载荷（REQUEST_COMPLETED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.REQUEST_COMPLETED

    server_name: str
    media_title: str
    media_year: int | str | None = None
    item_type: str | None = None
    reserved_cost: int = 0
    request_id: int | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class RequestArrFailedPayload:
    """求片同步 Arr 失败通知载荷（REQUEST_ARR_FAILED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.REQUEST_ARR_FAILED

    server_name: str
    media_title: str
    media_year: int | str | None = None
    fail_reason: str = ""
    approver_name: str | None = None
    reserved_cost: int = 0
    request_id: int | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class RequestExpiredPayload:
    """求片超时自动取消通知载荷（REQUEST_EXPIRED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.REQUEST_EXPIRED

    server_name: str
    media_title: str
    media_year: int | str | None = None
    expire_days: int = 7
    reserved_cost: int = 0
    request_id: int | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class PreExpiryReminderPayload:
    """媒体账号临近到期提醒载荷（PRE_EXPIRY_REMINDER）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.PRE_EXPIRY_REMINDER

    server_name: str
    media_name: str
    expires_at: datetime
    auto_renew_enabled: bool = False
    watch_eligible: bool = False
    watch_status_text: str = ""
    points_renew_score: int | None = None
    user_score: int | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class AutoRenewSuccessPayload:
    """媒体账号自动续期成功通知载荷（AUTO_RENEW_SUCCESS）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.AUTO_RENEW_SUCCESS

    server_name: str
    media_name: str
    expires_at: datetime
    renew_channel: Literal["watch_time", "points"] = "watch_time"
    cost_score: int = 0
    remaining_score: int | None = None
    is_traffic_suspended: bool = False
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class AutoRenewFailedPayload:
    """媒体账号自动续期失败通知载荷（AUTO_RENEW_FAILED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.AUTO_RENEW_FAILED

    server_name: str
    media_name: str
    expires_at: datetime
    reason: str = ""
    grace_days: int = 7
    required_score: int | None = None
    user_score: int | None = None
    watch_status_text: str = ""
    will_ban: bool = True
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class ExpiryBanPayload:
    """媒体账号过期封禁通知载荷（EXPIRY_BAN，遵循 ADR-0002）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.EXPIRY_BAN

    server_name: str
    media_name: str
    expires_at: datetime
    delete_at: datetime | None = None
    grace_days: int = 7
    points_renew_score: int | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class GraceDeleteReminderPayload:
    """宽限期删号前最终预警载荷（GRACE_DELETE_REMINDER，遵循 ADR-0002）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.GRACE_DELETE_REMINDER

    server_name: str
    media_name: str
    delete_at: datetime
    remaining_hours: int = 24
    grace_days: int = 7
    points_renew_score: int | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class AccountDeletedPayload:
    """媒体账号正式删除确认通知载荷（ACCOUNT_DELETED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.ACCOUNT_DELETED

    server_name: str
    media_name: str
    reason: str = "过期封禁宽限期届满未续期"
    deleted_at: datetime | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class WatchTimeRenewalEligiblePayload:
    """同周期首次达到时长保活续期门槛祝贺载荷（WATCH_TIME_RENEWAL_ELIGIBLE）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.WATCH_TIME_RENEWAL_ELIGIBLE

    server_name: str
    media_name: str
    watched_seconds: int
    threshold_seconds: int
    expires_at: datetime
    auto_renew_enabled: bool = False
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class BillingCycleResetPayload:
    """媒体账号锚定自然月周期重置通知载荷（BILLING_CYCLE_RESET）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.BILLING_CYCLE_RESET

    server_name: str
    media_name: str
    reset_at: datetime
    next_reset_at: datetime | None = None
    cleared_pack_bytes: int = 0
    cleared_adjustment_bytes: int = 0
    new_quota_bytes: int = 0
    unsuspended_traffic: bool = False
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class TrafficQuota80Payload:
    """流量配额 80% 预警通知载荷（TRAFFIC_QUOTA_80）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.TRAFFIC_QUOTA_80

    server_name: str
    media_name: str
    used_bytes: int
    quota_bytes: int
    pack_remaining_bytes: int = 0
    next_reset_at: datetime | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class TrafficQuota100Payload:
    """基础流量配额 100% 耗尽软告警通知载荷（TRAFFIC_QUOTA_100）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.TRAFFIC_QUOTA_100

    server_name: str
    media_name: str
    used_bytes: int
    quota_bytes: int
    pack_remaining_bytes: int = 0
    next_reset_at: datetime | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class TrafficAutoSuspendedPayload:
    """流量耗尽触发超流停用通知载荷（TRAFFIC_AUTO_SUSPENDED，遵循 ADR-0002）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.TRAFFIC_AUTO_SUSPENDED

    server_name: str
    media_name: str
    used_bytes: int
    quota_bytes: int
    pack_remaining_bytes: int = 0
    next_reset_at: datetime | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class TrafficUnsuspendedPayload:
    """超流停用解除通知载荷（TRAFFIC_UNSUSPENDED，覆盖四条恢复路径）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.TRAFFIC_UNSUSPENDED

    server_name: str
    media_name: str
    recovery_reason: Literal[
        "traffic_pack",
        "cycle_reset",
        "admin_adjust",
        "server_policy_change",
    ]
    added_bytes: int = 0
    total_pack_bytes: int = 0
    adjustment_bytes: int = 0
    reset_cycle_usage: bool = False
    next_reset_at: datetime | None = None
    still_expiry_banned: bool = False
    detail: str = ""
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class AdminUserActionPayload:
    """管理员对用户/媒体账号惩罚、调额或封删动作回执与审计载荷（ADMIN_USER_ACTION）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.ADMIN_USER_ACTION

    action_type: Literal[
        "warn",
        "score_adjust",
        "traffic_adjust",
        "ban",
        "unban",
        "kick",
        "delete",
        "other",
    ]
    target_user_id: int
    target_user_name: str = ""
    operator_name: str = "管理员"
    server_name: str | None = None
    media_name: str | None = None
    reason: str = ""
    warning_count: int | None = None
    score_delta: int | None = None
    current_score: int | None = None
    traffic_delta_bytes: int | None = None
    reset_cycle_usage: bool = False
    unsuspended_traffic: bool = False
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class SelfActionReceiptPayload:
    """用户自助操作脱敏回执载荷（SELF_ACTION_RECEIPT，严禁携带明文密码或激活码）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.SELF_ACTION_RECEIPT

    action_type: Literal[
        "register",
        "renew",
        "reset_password",
        "redeem_code",
        "toggle_auto_renew",
        "redeem_traffic_pack",
        "cancel_request",
        "other",
    ]
    server_name: str | None = None
    media_name: str | None = None
    summary: str = ""
    expires_at: datetime | None = None
    cost_score: int = 0
    added_traffic_bytes: int = 0
    auto_renew_enabled: bool | None = None
    code_kind: Literal["signup", "renew"] | None = None
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class ArrManualInteractionPayload:
    """Sonarr/Radarr 需要人工干预告警载荷（ARR_MANUAL_INTERACTION）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.ARR_MANUAL_INTERACTION

    server_name: str
    app_type: Literal["sonarr", "radarr", "arr"] = "arr"
    media_title: str = ""
    download_title: str = ""
    download_client: str = ""
    download_status: str = ""
    messages: Sequence[str] = ()
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookManualInteractionPayload
        | RadarrWebhookManualInteractionPayload,
        *,
        server_name: str = "",
    ) -> ArrManualInteractionPayload:
        """从 Sonarr/Radarr 人工干预 Webhook 载荷构造强类型告警载荷。"""
        is_sonarr = isinstance(payload, SonarrWebhookManualInteractionPayload)
        media_title = (
            (payload.series.title if payload.series else "")
            if is_sonarr
            else (payload.movie.title if payload.movie else "")
        )
        msgs: list[str] = []
        for item in payload.downloadStatusMessages:
            parts = [p for p in (item.title, "; ".join(item.messages)) if p]
            if parts:
                msgs.append("：".join(parts))
        return cls(
            server_name=server_name
            or payload.instanceName
            or ("Sonarr" if is_sonarr else "Radarr"),
            app_type="sonarr" if is_sonarr else "radarr",
            media_title=media_title,
            download_title=payload.downloadInfo.title or "",
            download_client=payload.downloadClient or "",
            download_status=payload.downloadStatus or "",
            messages=msgs,
        )


@dataclass(frozen=True, slots=True)
class ArrHealthIssuePayload:
    """Sonarr/Radarr 健康检查异常告警载荷（ARR_HEALTH_ISSUE）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.ARR_HEALTH_ISSUE

    server_name: str
    app_type: Literal["sonarr", "radarr", "arr"] = "arr"
    level: str = "warning"
    check_type: str = ""
    message: str = ""
    wiki_url: str | None = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookHealthPayload | RadarrWebhookHealthPayload,
        *,
        server_name: str = "",
    ) -> ArrHealthIssuePayload:
        """从 Sonarr/Radarr Health Webhook 构造告警载荷。"""
        is_sonarr = isinstance(payload, SonarrWebhookHealthPayload)
        return cls(
            server_name=server_name
            or payload.instanceName
            or ("Sonarr" if is_sonarr else "Radarr"),
            app_type="sonarr" if is_sonarr else "radarr",
            level=payload.level,
            check_type=payload.type,
            message=payload.message,
            wiki_url=payload.wikiUrl,
        )


@dataclass(frozen=True, slots=True)
class ArrHealthRestoredPayload:
    """Sonarr/Radarr 健康检查恢复通知载荷（ARR_HEALTH_RESTORED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.ARR_HEALTH_RESTORED

    server_name: str
    app_type: Literal["sonarr", "radarr", "arr"] = "arr"
    level: str = "ok"
    check_type: str = ""
    message: str = ""
    wiki_url: str | None = None
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookHealthPayload | RadarrWebhookHealthPayload,
        *,
        server_name: str = "",
    ) -> ArrHealthRestoredPayload:
        """从 Sonarr/Radarr HealthRestored Webhook 构造恢复载荷。"""
        is_sonarr = isinstance(payload, SonarrWebhookHealthPayload)
        return cls(
            server_name=server_name
            or payload.instanceName
            or ("Sonarr" if is_sonarr else "Radarr"),
            app_type="sonarr" if is_sonarr else "radarr",
            level=payload.level,
            check_type=payload.type,
            message=payload.message,
            wiki_url=payload.wikiUrl,
        )


@dataclass(frozen=True, slots=True)
class ArrTestPayload:
    """Sonarr/Radarr Webhook 测试事件载荷（ARR_TEST）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.ARR_TEST

    server_name: str
    app_type: Literal["sonarr", "radarr", "arr"] = "arr"
    instance_name: str = ""
    message: str = "Arr Webhook 测试连接成功"
    action_url: str | None = None

    @classmethod
    def from_webhook(
        cls,
        payload: SonarrWebhookTestPayload | RadarrWebhookTestPayload,
        *,
        server_name: str = "",
    ) -> ArrTestPayload:
        """从 Sonarr/Radarr Test Webhook 构造测试载荷。"""
        is_sonarr = isinstance(payload, SonarrWebhookTestPayload)
        return cls(
            server_name=server_name
            or payload.instanceName
            or ("Sonarr" if is_sonarr else "Radarr"),
            app_type="sonarr" if is_sonarr else "radarr",
            instance_name=payload.instanceName or "",
        )


@dataclass(frozen=True, slots=True)
class MediaAuthRiskPayload:
    """媒体服认证爆破或多次失败风控告警载荷（MEDIA_AUTH_RISK）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MEDIA_AUTH_RISK

    server_name: str
    username: str = ""
    client_name: str = ""
    device_name: str = ""
    remote_ip: str = ""
    failure_count: int = 1
    window_minutes: int = 5
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class MediaUserLockedOutPayload:
    """媒体服账号登录锁定告警载荷（MEDIA_USER_LOCKED_OUT）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MEDIA_USER_LOCKED_OUT

    server_name: str
    media_user_name: str
    media_user_id: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class MediaTaskFailedPayload:
    """媒体服计划任务失败告警载荷（MEDIA_TASK_FAILED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MEDIA_TASK_FAILED

    server_name: str
    task_name: str
    task_state: str = "Failed"
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class MediaSubtitleFailedPayload:
    """媒体服字幕自动下载失败告警载荷（MEDIA_SUBTITLE_FAILED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MEDIA_SUBTITLE_FAILED

    server_name: str
    item_name: str
    series_name: str | None = None
    item_type: str = ""
    language: str = ""
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class MediaSystemTestPayload:
    """媒体服系统测试 Webhook 载荷（MEDIA_SYSTEM_TEST）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MEDIA_SYSTEM_TEST

    server_name: str
    server_type: str = "media"
    title: str = "媒体服 Webhook 测试通知"
    description: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class MkvProcessFailedPayload:
    """MKV 后台混流清理失败告警载荷（MKV_PROCESS_FAILED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MKV_PROCESS_FAILED

    file_path: str
    error_message: str
    server_name: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class MkvPathUnmappedPayload:
    """MKV 路径未映射跳过处理告警载荷（MKV_PATH_UNMAPPED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MKV_PATH_UNMAPPED

    server_name: str
    remote_path: str
    diagnostic: str
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class NfoProcessFailedPayload:
    """NFO 元数据生成失败告警载荷（NFO_PROCESS_FAILED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.NFO_PROCESS_FAILED

    target_path: str
    nfo_kind: str = "nfo"
    error_message: str = ""
    server_name: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class TranslatorErrorPayload:
    """AI 元数据翻译或媒体写回失败告警载荷（TRANSLATOR_ERROR）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.TRANSLATOR_ERROR

    server_name: str
    item_id: str = ""
    item_name: str = ""
    stage: str = "translate"
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class QbShareLimitFailedPayload:
    """qBittorrent 做种分享率/时间限制设置失败告警载荷（QB_SHARE_LIMIT_FAILED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.QB_SHARE_LIMIT_FAILED

    server_name: str
    qb_server_name: str = ""
    download_id: str = ""
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalServerErrorPayload:
    """外部媒体/Arr/qB 服务连接或 5xx 异常告警载荷（EXTERNAL_SERVER_ERROR）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.EXTERNAL_SERVER_ERROR

    server_name: str
    server_type: str
    operation: str = ""
    status_code: int | None = None
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalServerRecoveredPayload:
    """外部服务异常恢复通知载荷（EXTERNAL_SERVER_RECOVERED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.EXTERNAL_SERVER_RECOVERED

    server_name: str
    server_type: str
    operation: str = ""
    detail: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleRetryAlertPayload:
    """账号生命周期异步任务进入重试告警载荷（LIFECYCLE_RETRY_ALERT）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.LIFECYCLE_RETRY_ALERT

    server_name: str
    media_name: str
    user_id: int
    job_kind: str
    retry_count: int = 1
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class NsfwRevokeRetryAlertPayload:
    """NSFW 权限撤销失败进入重试告警载荷（NSFW_REVOKE_RETRY_ALERT）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.NSFW_REVOKE_RETRY_ALERT

    server_name: str
    pending_count: int
    failed_count: int
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class DbBackupFailedPayload:
    """SQLite 数据库自动备份失败告警载荷（DB_BACKUP_FAILED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.DB_BACKUP_FAILED

    backup_path: str = ""
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupInactiveSummaryPayload:
    """定时清理离群非活跃用户非零变更摘要载荷（CLEANUP_INACTIVE_SUMMARY）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.CLEANUP_INACTIVE_SUMMARY

    checked_count: int = 0
    cleaned_count: int = 0
    failed_count: int = 0
    cleaned_users: Sequence[str] = ()
    failed_users: Sequence[str] = ()
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class StartupCalibrationSummaryPayload:
    """启动恢复与生命周期校准非零变更摘要载荷（STARTUP_CALIBRATION_SUMMARY）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.STARTUP_CALIBRATION_SUMMARY

    total_accounts: int = 0
    backfilled_count: int = 0
    reset_count: int = 0
    banned_count: int = 0
    deleted_count: int = 0
    rescheduled_count: int = 0
    error_count: int = 0
    details: Sequence[str] = ()
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class OrphanSessionsSummaryPayload:
    """孤立挂起播放会话截断清理摘要载荷（ORPHAN_SESSIONS_SUMMARY）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.ORPHAN_SESSIONS_SUMMARY

    closed_count: int
    affected_servers: Sequence[str] = ()
    details: Sequence[str] = ()
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class UserPolicySyncSummaryPayload:
    """单服默认 UserPolicy 批量同步结果摘要载荷（USER_POLICY_SYNC_SUMMARY）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.USER_POLICY_SYNC_SUMMARY

    server_name: str
    total_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    error_summary: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class BatchTrafficReleaseSummaryPayload:
    """策略变更或周期核对批量解除超流停用运维摘要载荷（BATCH_TRAFFIC_RELEASE_SUMMARY）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.BATCH_TRAFFIC_RELEASE_SUMMARY

    server_name: str
    trigger_reason: str
    released_count: int
    skipped_banned_count: int = 0
    failed_count: int = 0
    released_accounts: Sequence[str] = ()
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramDeliveryDegradedPayload:
    """Telegram 投递重试耗尽或通道降级运维告警载荷（TELEGRAM_DELIVERY_DEGRADED）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.TELEGRAM_DELIVERY_DEGRADED

    target_label: str
    event_type: str = ""
    failure_count: int = 1
    error_message: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreLeaderboardEntry:
    """积分结算榜单条目结构。"""

    user_id: int
    user_name: str
    score: int


@dataclass(frozen=True, slots=True)
class ScoreSettlementReportPayload:
    """社区活跃积分结算榜单播报载荷（SCORE_SETTLEMENT_REPORT）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.SCORE_SETTLEMENT_REPORT

    total_score_settled: int = 0
    daily_top: Sequence[ScoreLeaderboardEntry] = ()
    overall_top: Sequence[ScoreLeaderboardEntry] = ()
    is_manual: bool = False
    economy: EffectiveEconomyContext = field(default_factory=EffectiveEconomyContext)
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationRejectAuditPayload:
    """入群验证超时或管理员拒审踢封审计载荷（VERIFICATION_REJECT_AUDIT）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.VERIFICATION_REJECT_AUDIT

    user_id: int
    user_name: str = ""
    action: Literal["kicked", "banned"] = "kicked"
    reason: str = "入群验证超时或未通过"
    cleaned_accounts_summary: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class MemberLeftCleanupAuditPayload:
    """群成员退群自动清理联动审计载荷（MEMBER_LEFT_CLEANUP_AUDIT）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.MEMBER_LEFT_CLEANUP_AUDIT

    user_id: int
    user_name: str = ""
    cleaned_servers: Sequence[str] = ()
    success: bool = True
    detail: str = ""
    action_url: str | None = None


@dataclass(frozen=True, slots=True)
class SystemTestPayload:
    """系统通知通路测试载荷（SYSTEM_TEST）。"""

    event: ClassVar[NotificationEvent] = NotificationEvent.SYSTEM_TEST

    server_name: str = "系统全局"
    category: str = ""
    target_description: str = ""
    operator_name: str = ""
    triggered_at: datetime | None = None
    message: str = "这是一条系统通知通路测试消息。"
    action_url: str | None = None


NotificationPayload = (
    SonarrDownloadPayload
    | SonarrSeriesAddPayload
    | SonarrGrabPayload
    | SonarrRenamePayload
    | SonarrDeletePayload
    | RadarrDownloadPayload
    | RadarrMovieAddPayload
    | RadarrGrabPayload
    | RadarrRenamePayload
    | RadarrDeletePayload
    | EmbyLibraryNewPayload
    | JellyfinLibraryNewPayload
    | MediaLibraryDeletedPayload
    | RequestSubmitPayload
    | RequestApprovedPayload
    | RequestRejectedPayload
    | RequestCompletedPayload
    | RequestArrFailedPayload
    | RequestExpiredPayload
    | PreExpiryReminderPayload
    | AutoRenewSuccessPayload
    | AutoRenewFailedPayload
    | ExpiryBanPayload
    | GraceDeleteReminderPayload
    | AccountDeletedPayload
    | WatchTimeRenewalEligiblePayload
    | BillingCycleResetPayload
    | TrafficQuota80Payload
    | TrafficQuota100Payload
    | TrafficAutoSuspendedPayload
    | TrafficUnsuspendedPayload
    | AdminUserActionPayload
    | SelfActionReceiptPayload
    | ArrManualInteractionPayload
    | ArrHealthIssuePayload
    | ArrHealthRestoredPayload
    | ArrTestPayload
    | MediaAuthRiskPayload
    | MediaUserLockedOutPayload
    | MediaTaskFailedPayload
    | MediaSubtitleFailedPayload
    | MediaSystemTestPayload
    | MkvProcessFailedPayload
    | MkvPathUnmappedPayload
    | NfoProcessFailedPayload
    | TranslatorErrorPayload
    | QbShareLimitFailedPayload
    | ExternalServerErrorPayload
    | ExternalServerRecoveredPayload
    | LifecycleRetryAlertPayload
    | NsfwRevokeRetryAlertPayload
    | DbBackupFailedPayload
    | CleanupInactiveSummaryPayload
    | StartupCalibrationSummaryPayload
    | OrphanSessionsSummaryPayload
    | UserPolicySyncSummaryPayload
    | BatchTrafficReleaseSummaryPayload
    | TelegramDeliveryDegradedPayload
    | ScoreSettlementReportPayload
    | VerificationRejectAuditPayload
    | MemberLeftCleanupAuditPayload
    | SystemTestPayload
)


def _render_sonarr_download(payload: SonarrDownloadPayload) -> RenderedNotification:
    """渲染 SONARR_DOWNLOAD 剧集下载完成通知。"""
    episodes = _normalize_episodes(payload.episodes)
    ep_block = _format_episodes_html(episodes)
    quality_line = (
        TelegramMessage.render_html(t"质量: <code>{payload.quality}</code>")
        if payload.quality
        else ""
    )
    size_line = (
        TelegramMessage.render_html(
            t"大小: <code>{format_traffic_bytes(payload.size_bytes)}</code>"
        )
        if payload.size_bytes and payload.size_bytes > 0
        else ""
    )
    cf_block = _render_custom_format_html(
        payload.custom_format_score, payload.custom_formats
    )
    upgrade_line = "✨ <b>画质洗版升级</b>" if payload.is_upgrade else ""
    html_text = TelegramMessage.render_html(
        t"""\
        📥 <b>剧集下载完成</b>

        剧集: <b>{payload.series_title}</b>
        {ep_block:raw}
        {quality_line:raw}
        {size_line:raw}

        {cf_block:raw}

        {upgrade_line:raw}
        来源: <code>{payload.server_name}</code>

        #Sonarr #下载完成""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.SONARR_DOWNLOAD,
        title=f"剧集下载完成：{payload.series_title}",
        telegram_html=html_text,
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_sonarr_series_add(
    payload: SonarrSeriesAddPayload,
) -> RenderedNotification:
    """渲染 SONARR_SERIES_ADD 新增订阅剧集通知。"""
    year_part = _format_year_part(payload.year)
    genres_text = ", ".join(str(g) for g in payload.genres if str(g).strip())
    genres_line = (
        TelegramMessage.render_html(t"类型: {genres_text}") if genres_text else ""
    )
    path_line = (
        TelegramMessage.render_html(t"路径: <code>{payload.path}</code>")
        if payload.path
        else ""
    )
    tvdb_line = (
        TelegramMessage.render_html(t"TVDB ID: <code>{payload.tvdb_id}</code>")
        if payload.tvdb_id
        else ""
    )
    overview_short = _truncate_text(payload.overview, 100)
    overview_line = (
        TelegramMessage.render_html(t"简介: {overview_short}") if overview_short else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        📺 <b>新增订阅剧集</b>

        标题: <b>{payload.title}</b>{year_part}
        {genres_line:raw}
        {overview_line:raw}
        {path_line:raw}
        {tvdb_line:raw}
        来源: <code>{payload.server_name}</code>

        #Sonarr #新订阅""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.SONARR_SERIES_ADD,
        title=f"新增订阅剧集：{payload.title}{year_part}",
        telegram_html=html_text,
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_arr_grab_card(
    event: NotificationEvent,
    *,
    header: str,
    title_label: str,
    subject_title: str,
    year_part: str,
    server_name: str,
    hashtag: str,
    episodes: list[EpisodeDetail] | None = None,
    release_title: str = "",
    quality: str = "",
    indexer: str = "",
    size_bytes: int | None = None,
    download_client: str = "",
    cf_score: int | None = None,
    cf_formats: Sequence[str] = (),
    image: Any = None,
    action_url: str | None = None,
) -> RenderedNotification:
    """共享渲染 Sonarr/Radarr 资源抓取通知卡片。"""
    ep_block = _format_episodes_html(episodes) if episodes else ""
    release_line = (
        TelegramMessage.render_html(
            t"种子: <code>{_truncate_text(release_title, 140)}</code>"
        )
        if release_title
        else ""
    )
    quality_line = (
        TelegramMessage.render_html(t"质量: <code>{quality}</code>") if quality else ""
    )
    indexer_line = (
        TelegramMessage.render_html(t"站点: <code>{indexer}</code>") if indexer else ""
    )
    size_line = (
        TelegramMessage.render_html(
            t"大小: <code>{format_traffic_bytes(size_bytes)}</code>"
        )
        if size_bytes and size_bytes > 0
        else ""
    )
    client_line = (
        TelegramMessage.render_html(t"下载器: <code>{download_client}</code>")
        if download_client
        else ""
    )
    cf_block = _render_custom_format_html(cf_score, cf_formats)
    html_text = TelegramMessage.render_html(
        t"""\
        🧲 <b>{header}</b>

        {title_label}: <b>{subject_title}</b>{year_part}
        {ep_block:raw}
        {release_line:raw}
        {quality_line:raw}
        {indexer_line:raw}
        {size_line:raw}
        {client_line:raw}

        {cf_block:raw}

        来源: <code>{server_name}</code>

        {hashtag}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        event,
        title=f"{header}：{subject_title}{year_part}",
        telegram_html=html_text,
        image=image,
        action_url=action_url,
    )


def _render_arr_rename_card(
    event: NotificationEvent,
    *,
    subject_label: str,
    subject_title: str,
    year_part: str,
    renamed_count: int,
    renamed_files: Sequence[str],
    server_name: str,
    hashtag: str,
    image: Any = None,
    action_url: str | None = None,
) -> RenderedNotification:
    """共享渲染 Sonarr/Radarr 文件重命名通知卡片。"""
    preview = [
        TelegramMessage.render_html(t"• <code>{_truncate_text(f, 120)}</code>")
        for f in list(renamed_files)[:5]
        if str(f).strip()
    ]
    if len(renamed_files) > 5:
        preview.append(
            TelegramMessage.render_html(t"其余 {len(renamed_files) - 5} 个文件已省略")
        )
    files_block = "\n".join(preview)
    count_value = renamed_count or len(renamed_files)
    html_text = TelegramMessage.render_html(
        t"""\
        ✏️ <b>媒体文件已重命名</b>

        {subject_label}: <b>{subject_title}</b>{year_part}
        变更文件数: <code>{count_value}</code>
        {files_block:raw}

        来源: <code>{server_name}</code>

        {hashtag}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        event,
        title=f"文件重命名：{subject_title}{year_part}",
        telegram_html=html_text,
        image=image,
        action_url=action_url,
    )


def _render_arr_delete_card(
    event: NotificationEvent,
    *,
    header: str,
    subject_label: str,
    subject_title: str,
    year_part: str,
    server_name: str,
    hashtag: str,
    episodes: list[EpisodeDetail] | None = None,
    file_path: str = "",
    delete_reason: str = "",
    deleted_files: bool = False,
    image: Any = None,
    action_url: str | None = None,
) -> RenderedNotification:
    """共享渲染 Sonarr/Radarr 媒体或文件删除通知卡片。"""
    ep_block = _format_episodes_html(episodes) if episodes else ""
    path_line = (
        TelegramMessage.render_html(
            t"文件: <code>{_truncate_text(file_path, 160)}</code>"
        )
        if file_path
        else ""
    )
    reason_line = (
        TelegramMessage.render_html(t"原因: <code>{delete_reason}</code>")
        if delete_reason
        else ""
    )
    disk_line = (
        "磁盘文件: <code>已同步删除</code>"
        if deleted_files
        else "磁盘文件: <code>保留磁盘文件</code>"
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🗑️ <b>{header}</b>

        {subject_label}: <b>{subject_title}</b>{year_part}
        {ep_block:raw}
        {path_line:raw}
        {reason_line:raw}
        {disk_line:raw}
        来源: <code>{server_name}</code>

        {hashtag}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        event,
        title=f"{header}：{subject_title}{year_part}",
        telegram_html=html_text,
        image=image,
        action_url=action_url,
    )


def _render_sonarr_grab(payload: SonarrGrabPayload) -> RenderedNotification:
    """渲染 SONARR_GRAB 剧集抓取通知。"""
    return _render_arr_grab_card(
        NotificationEvent.SONARR_GRAB,
        header="剧集资源已抓取",
        title_label="剧集",
        subject_title=payload.series_title,
        year_part="",
        server_name=payload.server_name,
        hashtag="#Sonarr #抓取",
        episodes=_normalize_episodes(payload.episodes),
        release_title=payload.release_title,
        quality=payload.quality,
        indexer=payload.indexer,
        size_bytes=payload.size_bytes,
        download_client=payload.download_client,
        cf_score=payload.custom_format_score,
        cf_formats=payload.custom_formats,
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_sonarr_rename(payload: SonarrRenamePayload) -> RenderedNotification:
    """渲染 SONARR_RENAME 剧集重命名通知。"""
    return _render_arr_rename_card(
        NotificationEvent.SONARR_RENAME,
        subject_label="剧集",
        subject_title=payload.series_title,
        year_part="",
        renamed_count=payload.renamed_count,
        renamed_files=payload.renamed_files,
        server_name=payload.server_name,
        hashtag="#Sonarr #重命名",
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_sonarr_delete(payload: SonarrDeletePayload) -> RenderedNotification:
    """渲染 SONARR_DELETE 剧集或单集文件删除通知。"""
    header = (
        "剧集文件已删除" if payload.delete_scope == "episode_file" else "订阅剧集已删除"
    )
    return _render_arr_delete_card(
        NotificationEvent.SONARR_DELETE,
        header=header,
        subject_label="剧集",
        subject_title=payload.series_title,
        year_part="",
        server_name=payload.server_name,
        hashtag="#Sonarr #删除",
        episodes=_normalize_episodes(payload.episodes),
        file_path=payload.file_path,
        delete_reason=payload.delete_reason,
        deleted_files=payload.deleted_files,
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_radarr_download(payload: RadarrDownloadPayload) -> RenderedNotification:
    """渲染 RADARR_DOWNLOAD 电影下载完成通知。"""
    year_part = _format_year_part(payload.year)
    quality_line = (
        TelegramMessage.render_html(t"质量: <code>{payload.quality}</code>")
        if payload.quality
        else ""
    )
    size_line = (
        TelegramMessage.render_html(
            t"大小: <code>{format_traffic_bytes(payload.size_bytes)}</code>"
        )
        if payload.size_bytes and payload.size_bytes > 0
        else ""
    )
    cf_block = _render_custom_format_html(
        payload.custom_format_score, payload.custom_formats
    )
    upgrade_line = "✨ <b>画质洗版升级</b>" if payload.is_upgrade else ""
    overview_short = _truncate_text(payload.overview, 80)
    overview_line = (
        TelegramMessage.render_html(t"简介: {overview_short}") if overview_short else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        📥 <b>电影下载完成</b>

        标题: <b>{payload.title}</b>{year_part}
        {quality_line:raw}
        {size_line:raw}

        {cf_block:raw}

        {upgrade_line:raw}
        {overview_line:raw}
        来源: <code>{payload.server_name}</code>

        #Radarr #下载完成""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.RADARR_DOWNLOAD,
        title=f"电影下载完成：{payload.title}{year_part}",
        telegram_html=html_text,
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_radarr_movie_add(payload: RadarrMovieAddPayload) -> RenderedNotification:
    """渲染 RADARR_MOVIE_ADD 新增订阅电影通知。"""
    year_part = _format_year_part(payload.year)
    genres_text = ", ".join(str(g) for g in payload.genres if str(g).strip())
    genres_line = (
        TelegramMessage.render_html(t"类型: {genres_text}") if genres_text else ""
    )
    overview_short = _truncate_text(payload.overview, 100)
    overview_line = (
        TelegramMessage.render_html(t"简介: {overview_short}") if overview_short else ""
    )
    tmdb_line = (
        TelegramMessage.render_html(t"TMDB ID: <code>{payload.tmdb_id}</code>")
        if payload.tmdb_id
        else ""
    )
    method_line = (
        TelegramMessage.render_html(t"添加方式: <code>{payload.add_method}</code>")
        if payload.add_method
        else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🎬 <b>新增订阅电影</b>

        标题: <b>{payload.title}</b>{year_part}
        {genres_line:raw}
        {overview_line:raw}
        {tmdb_line:raw}
        {method_line:raw}
        来源: <code>{payload.server_name}</code>

        #Radarr #新增订阅""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.RADARR_MOVIE_ADD,
        title=f"新增订阅电影：{payload.title}{year_part}",
        telegram_html=html_text,
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_radarr_grab(payload: RadarrGrabPayload) -> RenderedNotification:
    """渲染 RADARR_GRAB 电影资源抓取通知。"""
    year_part = _format_year_part(payload.year)
    return _render_arr_grab_card(
        NotificationEvent.RADARR_GRAB,
        header="电影资源已抓取",
        title_label="标题",
        subject_title=payload.title,
        year_part=year_part,
        server_name=payload.server_name,
        hashtag="#Radarr #抓取",
        release_title=payload.release_title,
        quality=payload.quality,
        indexer=payload.indexer,
        size_bytes=payload.size_bytes,
        download_client=payload.download_client,
        cf_score=payload.custom_format_score,
        cf_formats=payload.custom_formats,
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_radarr_rename(payload: RadarrRenamePayload) -> RenderedNotification:
    """渲染 RADARR_RENAME 电影重命名通知。"""
    return _render_arr_rename_card(
        NotificationEvent.RADARR_RENAME,
        subject_label="电影",
        subject_title=payload.title,
        year_part=_format_year_part(payload.year),
        renamed_count=payload.renamed_count,
        renamed_files=payload.renamed_files,
        server_name=payload.server_name,
        hashtag="#Radarr #重命名",
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_radarr_delete(payload: RadarrDeletePayload) -> RenderedNotification:
    """渲染 RADARR_DELETE 电影或文件删除通知。"""
    header = (
        "电影文件已删除" if payload.delete_scope == "movie_file" else "订阅电影已删除"
    )
    return _render_arr_delete_card(
        NotificationEvent.RADARR_DELETE,
        header=header,
        subject_label="电影",
        subject_title=payload.title,
        year_part=_format_year_part(payload.year),
        server_name=payload.server_name,
        hashtag="#Radarr #删除",
        file_path=payload.file_path,
        delete_reason=payload.delete_reason,
        deleted_files=payload.deleted_files,
        image=payload.image or extract_arr_poster_url(payload.images),
        action_url=payload.action_url,
    )


def _render_library_new_card(
    event: NotificationEvent,
    *,
    server_name: str,
    series_name: str | None,
    name: str,
    year: int | None,
    item_type: str,
    overview: str | None,
    season_number: int | None = None,
    episode_number: int | None = None,
    image: Any = None,
    action_url: str | None = None,
) -> RenderedNotification:
    """共享渲染 Emby / Jellyfin 媒体库新入库卡片。"""
    series_line = (
        TelegramMessage.render_html(t"剧集: <b>{series_name}</b>")
        if series_name
        else ""
    )
    ep_code = (
        f"S{season_number:02d}E{episode_number:02d} "
        if season_number is not None and episode_number is not None
        else ""
    )
    year_part = _format_year_part(year)
    display_name = name or series_name or "未命名媒体"
    title_line = TelegramMessage.render_html(
        t"标题: <b>{ep_code}{display_name}</b>{year_part}"
    )
    type_line = (
        TelegramMessage.render_html(t"类型: <code>{item_type}</code>")
        if item_type
        else ""
    )
    overview_short = _truncate_text(overview, 120)
    overview_block = (
        TelegramMessage.render_html(t"简介: {overview_short}") if overview_short else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🆕 <b>媒体库更新</b>

        {series_line:raw}
        {title_line:raw}
        {type_line:raw}
        {overview_block:raw}

        来源: <code>{server_name}</code>

        #入库""",
        collapse_blank_lines=True,
    )
    summary_title = (
        f"{series_name} - {ep_code}{display_name}{year_part}"
        if series_name
        else f"{display_name}{year_part}"
    )
    return _make_rendered(
        event,
        title=f"媒体库入库：{summary_title}",
        telegram_html=html_text,
        image=image,
        action_url=action_url,
    )


def _render_emby_library_new(payload: EmbyLibraryNewPayload) -> RenderedNotification:
    """渲染 EMBY_LIBRARY_NEW 入库通知（兼容 Webhook 与 REST API Item 模型）。"""
    item = payload.item
    name = payload.name
    series_name = payload.series_name
    year = payload.year
    item_type = payload.item_type
    overview = payload.overview
    season_num = payload.season_number
    episode_num = payload.episode_number

    if isinstance(item, EmbyWebhookBaseItemDto):
        name = name or item.name
        series_name = series_name or item.series_name
        year = year if year is not None else item.production_year
        item_type = item_type or item.type
        overview = overview if overview is not None else item.overview
        season_num = season_num if season_num is not None else item.parent_index_number
        episode_num = episode_num if episode_num is not None else item.index_number
    elif isinstance(item, EmbyApiBaseItemDto):
        name = name or (item.Name or "")
        series_name = series_name or item.SeriesName
        year = year if year is not None else item.ProductionYear
        item_type = item_type or (item.Type or "")
        overview = overview if overview is not None else item.Overview
        season_num = season_num if season_num is not None else item.ParentIndexNumber
        episode_num = episode_num if episode_num is not None else item.IndexNumber

    return _render_library_new_card(
        NotificationEvent.EMBY_LIBRARY_NEW,
        server_name=payload.server_name,
        series_name=series_name,
        name=name,
        year=year,
        item_type=item_type,
        overview=overview,
        season_number=season_num,
        episode_number=episode_num,
        image=payload.image,
        action_url=payload.action_url,
    )


def _render_jellyfin_library_new(
    payload: JellyfinLibraryNewPayload,
) -> RenderedNotification:
    """渲染 JELLYFIN_LIBRARY_NEW 入库通知，完整消费 Jellyfin ``ItemEvent``。"""
    item = payload.item
    server_name = payload.server_name or (item.server_name if item is not None else "")
    return _render_library_new_card(
        NotificationEvent.JELLYFIN_LIBRARY_NEW,
        server_name=server_name or "Jellyfin",
        series_name=payload.series_name
        or (item.series_name if item is not None else None),
        name=payload.name or (item.name if item is not None else ""),
        year=payload.year
        if payload.year is not None
        else (item.year if item is not None else None),
        item_type=payload.item_type or (item.item_type if item is not None else ""),
        overview=payload.overview
        if payload.overview is not None
        else (item.overview if item is not None else None),
        season_number=payload.season_number
        if payload.season_number is not None
        else (item.season_number if item is not None else None),
        episode_number=payload.episode_number
        if payload.episode_number is not None
        else (item.episode_number if item is not None else None),
        image=payload.image,
        action_url=payload.action_url,
    )


def _render_media_library_deleted(
    payload: MediaLibraryDeletedPayload,
) -> RenderedNotification:
    """渲染 MEDIA_LIBRARY_DELETED 媒体项删除通知。"""
    series_line = (
        TelegramMessage.render_html(t"剧集: <b>{payload.series_name}</b>")
        if payload.series_name
        else ""
    )
    year_part = _format_year_part(payload.year)
    type_line = (
        TelegramMessage.render_html(t"类型: <code>{payload.item_type}</code>")
        if payload.item_type
        else ""
    )
    item_id_line = (
        TelegramMessage.render_html(t"媒体项 ID: <code>{payload.item_id}</code>")
        if payload.item_id
        else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🗑️ <b>媒体库内容已移除</b>

        {series_line:raw}
        标题: <b>{payload.name}</b>{year_part}
        {type_line:raw}
        {item_id_line:raw}

        来源: <code>{payload.server_name}</code>

        #媒体库 #删除""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.MEDIA_LIBRARY_DELETED,
        title=f"媒体库内容已移除：{payload.name}{year_part}",
        telegram_html=html_text,
        action_url=payload.action_url,
    )


def _format_request_cost_line(
    label: str,
    reserved_cost: int,
    economy: EffectiveEconomyContext,
) -> str:
    """按 ADR-0004 条件格式化求片积分行；0 分免费或关闭积分求片时返回空串。"""
    if reserved_cost <= 0 or not economy.effective_points_request:
        return ""
    return TelegramMessage.render_html(t"{label}: <code>{reserved_cost}</code> 积分")


_REQUEST_TOPIC_STAGE_META: dict[str, tuple[str, str, str, str]] = {
    "approved": ("✅", "求片已批准", "💰 <b>已结算</b>", "#已批准"),
    "rejected": ("❌", "求片已拒绝", "💰 <b>已退还</b>", "#已拒绝"),
    "failed": ("❌", "求片添加失败", "💰 <b>已退还</b>", "#添加失败"),
    "expired": ("⏰", "求片已超时自动取消", "💰 <b>已退还</b>", "#已超时"),
    "canceled": ("🚫", "求片已由申请人撤销", "💰 <b>已退还</b>", "#已撤销"),
    "completed": ("🎉", "求片已入库就绪", "💰 <b>已结算</b>", "#已入库"),
}


def render_request_topic_card_status(
    *,
    stage: Literal[
        "approved", "rejected", "failed", "expired", "canceled", "completed"
    ],
    request_id: int,
    user_id: int,
    server_name: str,
    media_title: str,
    media_type: str,
    media_id: int | str,
    reserved_cost: int = 0,
    approver_name: str | None = None,
    detail_text: str | None = None,
    user_name: str | None = None,
    economy: EffectiveEconomyContext | None = None,
) -> str:
    """渲染求片状态终结/入库后回写原管理员审核卡片的 Telegram HTML 文本。

    Args:
        stage: 目标状态阶段（``approved`` / ``rejected`` / ``failed`` / ``expired`` / ``canceled`` / ``completed``）。
        request_id: 求片记录 ID。
        user_id: 申请人 Telegram 用户 ID。
        server_name: 目标媒体服务器名称。
        media_title: 媒体标题。
        media_type: 媒体外部标识类型（如 ``tmdb`` / ``tvdb``）。
        media_id: 媒体外部标识 ID。
        reserved_cost: 提交时预留积分数量。
        approver_name: 可选审核管理员名称。
        detail_text: 可选失败/拒绝/超时说明详情。
        user_name: 可选申请人展示名称，默认使用用户 ID 字符串。
        economy: 遵循 ADR-0004 的有效经济开关快照。

    Returns:
        已完成 HTML 转义并收敛多余空行的 Telegram HTML 字符串。
    """
    emoji, header, cost_label, tag = _REQUEST_TOPIC_STAGE_META[stage]
    effective_economy = economy or EffectiveEconomyContext()
    display_user = (user_name or "").strip() or str(user_id)
    id_line = (
        TelegramMessage.render_html(t"{media_type.upper()}: <code>{media_id}</code>")
        if str(media_id).strip()
        else ""
    )
    approver_line = (
        TelegramMessage.render_html(t"👮 <b>审核人</b>: <code>{approver_name}</code>")
        if approver_name and approver_name.strip()
        else ""
    )
    detail_line = (
        TelegramMessage.render_html(
            t"📝 <b>说明</b>: {_truncate_text(detail_text, 240)}"
        )
        if detail_text and detail_text.strip()
        else ""
    )
    cost_line = _format_request_cost_line(
        cost_label, max(0, int(reserved_cost)), effective_economy
    )
    return TelegramMessage.render_html(
        t"""\
        {emoji} <b>{header}</b> (#{request_id})

        👤 <b>申请人</b>: <a href="tg://user?id={user_id}">{display_user}</a>
        🎬 <b>标题</b>: <b>{media_title}</b>
        🖥️ <b>服务器</b>: <code>{server_name}</code>
        {id_line:raw}
        {approver_line:raw}
        {detail_line:raw}
        {cost_line:raw}

        #求片 {tag}""",
        collapse_blank_lines=True,
    )


def _render_request_submit(payload: RequestSubmitPayload) -> RenderedNotification:
    """渲染 REQUEST_SUBMIT 审核卡片并按需自动构造批准/拒绝内联按钮。"""
    year_part = _format_year_part(payload.media_year)
    overview_short = _truncate_text(payload.overview, 100)
    overview_line = (
        TelegramMessage.render_html(t"📝 <b>简介</b>: {overview_short}")
        if overview_short
        else ""
    )
    id_line = (
        TelegramMessage.render_html(
            t"{payload.media_type.upper()}: <code>{payload.media_id}</code>"
        )
        if str(payload.media_id).strip()
        else ""
    )
    cost_line = _format_request_cost_line(
        "💰 <b>预留</b>", payload.reserved_cost, payload.economy
    )
    buttons = payload.buttons
    if buttons is None and payload.request_id is not None:
        buttons = [
            [
                Button.inline("✅ 批准", data=f"req_ap_{payload.request_id}".encode()),
                Button.inline(
                    "❌ 拒绝", data=f"req_deny_{payload.request_id}".encode()
                ),
            ]
        ]

    html_text = TelegramMessage.render_html(
        t"""\
        🎬 <b>收到新的求片请求</b>

        👤 <b>申请人</b>: <a href="tg://user?id={payload.user_id}">{payload.user_name}</a>
        🎬 <b>标题</b>: <b>{payload.media_title}</b>{year_part}
        🖥️ <b>服务器</b>: <code>{payload.server_name}</code>
        {overview_line:raw}
        {id_line:raw}
        {cost_line:raw}

        #求片 #待审核""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.REQUEST_SUBMIT,
        title=f"收到求片：{payload.media_title}{year_part}",
        telegram_html=html_text,
        buttons=buttons,
        image=payload.image if payload.image is not None else payload.poster_url,
        action_url=payload.action_url,
        dedupe_hint=(
            f"request_submit:{payload.request_id}"
            if payload.request_id is not None
            else None
        ),
    )


def _render_request_dm_card(
    event: NotificationEvent,
    *,
    emoji: str,
    header: str,
    summary_title: str,
    media_title: str,
    media_year: int | str | None,
    server_name: str,
    approver_name: str | None = None,
    detail_label: str = "",
    detail_text: str = "",
    cost_label: str = "",
    reserved_cost: int = 0,
    economy: EffectiveEconomyContext,
    footer: str,
    request_id: int | None = None,
    action_url: str | None = None,
) -> RenderedNotification:
    """共享渲染求片状态变更私聊回执卡片（批准/拒绝/入库/失败/超时）。"""
    year_part = _format_year_part(media_year)
    approver_line = (
        TelegramMessage.render_html(t"审核人: <code>{approver_name}</code>")
        if approver_name
        else ""
    )
    detail_line = (
        TelegramMessage.render_html(
            t"<b>{detail_label}</b>: {_truncate_text(detail_text, 400)}"
        )
        if detail_label and detail_text.strip()
        else ""
    )
    cost_line = _format_request_cost_line(cost_label, reserved_cost, economy)
    html_text = TelegramMessage.render_html(
        t"""\
        {emoji} <b>{header}</b>

        片名: <b>{media_title}</b>{year_part}
        服务器: <code>{server_name}</code>
        {approver_line:raw}
        {detail_line:raw}
        {cost_line:raw}

        {footer}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        event,
        title=f"{summary_title}：{media_title}{year_part}",
        telegram_html=html_text,
        action_url=action_url or "/miniapp.html#requests",
        dedupe_hint=(f"{event.value}:{request_id}" if request_id is not None else None),
    )


def _render_request_approved(payload: RequestApprovedPayload) -> RenderedNotification:
    """渲染 REQUEST_APPROVED 求片批准通知。"""
    return _render_request_dm_card(
        NotificationEvent.REQUEST_APPROVED,
        emoji="✅",
        header="求片已批准",
        summary_title="求片已批准",
        media_title=payload.media_title,
        media_year=payload.media_year,
        server_name=payload.server_name,
        approver_name=payload.approver_name,
        cost_label="结算",
        reserved_cost=payload.reserved_cost,
        economy=payload.economy,
        footer="系统已自动将其加入下载队列，请耐心等待入库完成。",
        request_id=payload.request_id,
        action_url=payload.action_url,
    )


def _render_request_rejected(payload: RequestRejectedPayload) -> RenderedNotification:
    """渲染 REQUEST_REJECTED 求片拒绝通知。"""
    return _render_request_dm_card(
        NotificationEvent.REQUEST_REJECTED,
        emoji="❌",
        header="求片被拒绝",
        summary_title="求片被拒绝",
        media_title=payload.media_title,
        media_year=payload.media_year,
        server_name=payload.server_name,
        approver_name=payload.approver_name,
        detail_label="拒绝原因",
        detail_text=payload.reason or "未通过审核",
        cost_label="已全额释放预留",
        reserved_cost=payload.reserved_cost,
        economy=payload.economy,
        footer="如有疑问，请联系管理员。",
        request_id=payload.request_id,
        action_url=payload.action_url,
    )


def _render_request_completed(payload: RequestCompletedPayload) -> RenderedNotification:
    """渲染 REQUEST_COMPLETED 求片真入库完成通知。"""
    type_detail = f"媒体类型: {payload.item_type}" if payload.item_type else ""
    return _render_request_dm_card(
        NotificationEvent.REQUEST_COMPLETED,
        emoji="🎉",
        header="求片影片已入库",
        summary_title="求片已入库",
        media_title=payload.media_title,
        media_year=payload.media_year,
        server_name=payload.server_name,
        detail_label="详情" if type_detail else "",
        detail_text=type_detail,
        cost_label="已结算",
        reserved_cost=payload.reserved_cost,
        economy=payload.economy,
        footer="所求影片已在目标媒体服务器就绪，祝您观影愉快！",
        request_id=payload.request_id,
        action_url=payload.action_url,
    )


def _render_request_arr_failed(
    payload: RequestArrFailedPayload,
) -> RenderedNotification:
    """渲染 REQUEST_ARR_FAILED 求片提交 Arr 失败通知。"""
    return _render_request_dm_card(
        NotificationEvent.REQUEST_ARR_FAILED,
        emoji="❌",
        header="求片添加失败",
        summary_title="求片添加失败",
        media_title=payload.media_title,
        media_year=payload.media_year,
        server_name=payload.server_name,
        approver_name=payload.approver_name,
        detail_label="失败原因",
        detail_text=payload.fail_reason or "外部媒体下载服务调用失败",
        cost_label="已全额释放预留",
        reserved_cost=payload.reserved_cost,
        economy=payload.economy,
        footer="您可稍后重试提交或联系管理员排查 Arr 实例状态。",
        request_id=payload.request_id,
        action_url=payload.action_url,
    )


def _render_request_expired(payload: RequestExpiredPayload) -> RenderedNotification:
    """渲染 REQUEST_EXPIRED 求片超时自动取消通知。"""
    return _render_request_dm_card(
        NotificationEvent.REQUEST_EXPIRED,
        emoji="⏰",
        header="求片已超时自动取消",
        summary_title="求片超时取消",
        media_title=payload.media_title,
        media_year=payload.media_year,
        server_name=payload.server_name,
        detail_label="说明",
        detail_text=f"待审核已满 {max(1, int(payload.expire_days))} 天未处理，系统已自动取消",
        cost_label="已全额释放预留",
        reserved_cost=payload.reserved_cost,
        economy=payload.economy,
        footer="如需继续求片可重新发起申请。",
        request_id=payload.request_id,
        action_url=payload.action_url,
    )


def _format_duration_seconds(seconds: int) -> str:
    """将播放时长秒数格式化为可读的中文时长字符串。"""
    secs = max(0, int(seconds))
    hours, rem = divmod(secs, 3600)
    minutes = rem // 60
    if hours > 0 and minutes > 0:
        return f"{hours} 小时 {minutes} 分钟"
    if hours > 0:
        return f"{hours} 小时"
    if minutes > 0:
        return f"{minutes} 分钟"
    return f"{secs} 秒"


def _build_paid_renew_methods(
    economy: EffectiveEconomyContext,
    *,
    points_renew_score: int | None = None,
) -> str:
    """按 ADR-0004 构建当前允许的手动续期方式描述，关闭积分时绝不暴露积分字样。"""
    if economy.effective_points_renew:
        if points_renew_score is not None and points_renew_score > 0:
            return f"积分（需 {points_renew_score} 积分）或激活码"
        return "积分或激活码"
    return "激活码"


def _build_traffic_pack_guidance(economy: EffectiveEconomyContext) -> str:
    """按 ADR-0004 构建超流恢复操作提示；买包未同时开放时仅引导管理员调额或等待重置。"""
    if economy.effective_traffic_pack:
        return "可在 Mini App 使用积分兑换流量包立即恢复，或等待下次账期重置/联系管理员临时调额后解除。"
    return "请联系管理员临时调额恢复，或等待下一次账期重置后自动解除。"


def _render_pre_expiry_reminder(
    payload: PreExpiryReminderPayload,
) -> RenderedNotification:
    """渲染 PRE_EXPIRY_REMINDER 临近到期提醒，严格遵守 ADR-0004 动态隐藏关闭体系。"""
    exp_str = payload.expires_at.strftime("%Y-%m-%d %H:%M:%S")
    watch_open = payload.economy.effective_watch_time_renewal
    points_open = payload.economy.effective_points_renew
    paid_methods = _build_paid_renew_methods(
        payload.economy, points_renew_score=payload.points_renew_score
    )

    if payload.auto_renew_enabled:
        if watch_open and payload.watch_eligible:
            hint_line = "已开启自动续期：当前观看度已达 100%，到期将自动免费续期。"
        elif watch_open:
            status_part = (
                f"当前{payload.watch_status_text}；"
                if payload.watch_status_text
                else ""
            )
            if points_open:
                if (
                    payload.points_renew_score is not None
                    and payload.user_score is not None
                ):
                    if payload.user_score >= payload.points_renew_score:
                        score_clause = (
                            f"否则将自动消耗 {payload.points_renew_score} 积分续期"
                            f"（当前积分 {payload.user_score}，充足）。"
                        )
                    else:
                        score_clause = (
                            f"且当前积分不足（需 {payload.points_renew_score} 积分，当前 {payload.user_score}）；"
                            "请在到期前继续观影达标、补充积分或使用激活码续期，以免过期封禁。"
                        )
                else:
                    score_clause = "未达标时将尝试使用积分自动续期。"
                hint_line = f"已开启自动续期：{status_part}若到期前观看度达 100% 将免费续期，{score_clause}"
            else:
                hint_line = f"已开启自动续期：{status_part}请在到期前继续观影达 100%（到期后自动免费续期）或使用激活码手动续期。"
        elif points_open:
            if (
                payload.points_renew_score is not None
                and payload.user_score is not None
            ):
                if payload.user_score >= payload.points_renew_score:
                    hint_line = (
                        f"已开启自动续期：到期时将自动消耗 {payload.points_renew_score} 积分续期"
                        f"（当前积分 {payload.user_score}，充足）。"
                    )
                else:
                    hint_line = (
                        f"已开启自动续期，但当前积分不足（需 {payload.points_renew_score} 积分，当前 {payload.user_score}），"
                        "请在到期前补充积分或使用激活码续期，以免过期封禁。"
                    )
            else:
                hint_line = "已开启自动续期：到期时将尝试使用积分自动续期。"
        else:
            hint_line = "已开启自动续期，但当前服务器暂未开放可自动触发的续期条件，请及时使用激活码手动续期，以免过期封禁。"
    elif watch_open and payload.watch_eligible:
        hint_line = "当前观看度已达 100%，可前往 Mini App 免费续期。"
    elif watch_open:
        status_part = (
            f"保活进度: {payload.watch_status_text}，"
            if payload.watch_status_text
            else ""
        )
        hint_line = f"{status_part}可在到期前继续观影达标或使用{paid_methods}续期。"
    else:
        hint_line = f"请前往 Mini App 使用{paid_methods}及时续期，以免过期封禁。"

    html_text = TelegramMessage.render_html(
        t"""\
        ⏰ <b>媒体账号临近到期提醒</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        到期时间: <code>{exp_str}</code>
        {hint_line}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.PRE_EXPIRY_REMINDER,
        title=f"临近到期提醒：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=f"pre_expiry:{payload.server_name}:{payload.media_name}",
    )


def _render_auto_renew_success(
    payload: AutoRenewSuccessPayload,
) -> RenderedNotification:
    """渲染 AUTO_RENEW_SUCCESS 自动续期成功回执，遵循 ADR-0002 与 ADR-0004。"""
    exp_str = payload.expires_at.strftime("%Y-%m-%d %H:%M:%S")
    if payload.renew_channel == "watch_time":
        if payload.economy.effective_watch_time_renewal:
            if payload.economy.points_enabled:
                method_line = "续期方式: <code>时长保活免积分续期（消耗积分: 0）</code>"
            else:
                method_line = "续期方式: <code>时长保活自动免费续期</code>"
        else:
            method_line = "续期方式: <code>自动免费续期</code>"
    elif payload.economy.effective_points_renew:
        remaining_part = (
            f"（剩余 {payload.remaining_score} 积分）"
            if payload.remaining_score is not None
            else ""
        )
        method_line = TelegramMessage.render_html(
            t"消耗积分: <code>{payload.cost_score}</code>（积分自动续期）{remaining_part}"
        )
    else:
        method_line = "续期方式: <code>自动续期完成</code>"

    traffic_note = ""
    if payload.is_traffic_suspended and payload.economy.traffic_enabled:
        traffic_note = TelegramMessage.render_html(
            t"提示: 账号当前仍处于超流停用状态（不会触发删号），{_build_traffic_pack_guidance(payload.economy)}"
        )

    html_text = TelegramMessage.render_html(
        t"""\
        ✅ <b>自动续期成功</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        {method_line:raw}
        有效期延长至: <code>{exp_str}</code>
        {traffic_note:raw}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.AUTO_RENEW_SUCCESS,
        title=f"自动续期成功：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=f"auto_renew_success:{payload.server_name}:{payload.media_name}",
    )


def _render_auto_renew_failed(payload: AutoRenewFailedPayload) -> RenderedNotification:
    """渲染 AUTO_RENEW_FAILED 自动续期失败通知（遵循 ADR-0002 与 ADR-0004）。"""
    exp_str = payload.expires_at.strftime("%Y-%m-%d %H:%M:%S")
    watch_open = payload.economy.effective_watch_time_renewal
    points_open = payload.economy.effective_points_renew

    detail_lines: list[str] = []
    if payload.reason.strip():
        detail_lines.append(
            TelegramMessage.render_html(
                t"失败原因: {_truncate_text(payload.reason, 240)}"
            )
        )
    else:
        if watch_open and payload.watch_status_text:
            detail_lines.append(
                TelegramMessage.render_html(t"保活进度: {payload.watch_status_text}")
            )
        if points_open and payload.required_score is not None:
            if payload.user_score is not None:
                detail_lines.append(
                    TelegramMessage.render_html(
                        t"续期条件: 需 <code>{payload.required_score}</code> 积分（当前 <code>{payload.user_score}</code> 积分）"
                    )
                )
            else:
                detail_lines.append(
                    TelegramMessage.render_html(
                        t"续期条件: 需 <code>{payload.required_score}</code> 积分"
                    )
                )
        if not detail_lines:
            detail_lines.append("失败原因: 暂未满足当前服务器开放的自动续期条件")

    paid_methods = _build_paid_renew_methods(
        payload.economy, points_renew_score=payload.required_score
    )
    if payload.will_ban:
        action_hint = f"账号到期后将进入过期封禁与 {max(0, int(payload.grace_days))} 天删除宽限期，请及时使用{paid_methods}续期。"
    else:
        action_hint = f"请及时使用{paid_methods}完成续期，以免过期封禁。"

    detail_block = "\n".join(detail_lines)
    html_text = TelegramMessage.render_html(
        t"""\
        ⚠️ <b>自动续期失败</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        到期时间: <code>{exp_str}</code>
        {detail_block:raw}

        {action_hint}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.AUTO_RENEW_FAILED,
        title=f"自动续期失败：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=f"auto_renew_failed:{payload.server_name}:{payload.media_name}",
    )


def _render_expiry_ban(payload: ExpiryBanPayload) -> RenderedNotification:
    """渲染 EXPIRY_BAN 过期封禁通知（严格遵守 ADR-0002：绝不提及流量/超流）。"""
    exp_str = payload.expires_at.strftime("%Y-%m-%d %H:%M:%S")
    grace_days = max(0, int(payload.grace_days))
    delete_line = (
        TelegramMessage.render_html(
            t"预计清理时间: <code>{payload.delete_at.strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )
        if payload.delete_at is not None
        else ""
    )
    paid_methods = _build_paid_renew_methods(
        payload.economy, points_renew_score=payload.points_renew_score
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🚫 <b>媒体账号已过期封禁</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        到期时间: <code>{exp_str}</code>
        删除宽限期: <code>{grace_days} 天</code>
        {delete_line:raw}

        您的媒体账号因有效期到期未续期已被禁用并进入删号宽限期。请在宽限期内前往 Mini App 使用{paid_methods}续期，续期成功后将自动恢复访问权限。""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.EXPIRY_BAN,
        title=f"账号已过期封禁：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=f"expiry_ban:{payload.server_name}:{payload.media_name}",
    )


def _render_grace_delete_reminder(
    payload: GraceDeleteReminderPayload,
) -> RenderedNotification:
    """渲染 GRACE_DELETE_REMINDER 宽限期删号前最后提醒（严格遵守 ADR-0002 与 ADR-0004）。"""
    delete_str = payload.delete_at.strftime("%Y-%m-%d %H:%M:%S")
    hours = max(1, int(payload.remaining_hours))
    grace_days = max(0, int(payload.grace_days))
    paid_methods = _build_paid_renew_methods(
        payload.economy, points_renew_score=payload.points_renew_score
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🚨 <b>过期账号即将永久删除（最后提醒）</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        宽限期长度: <code>{grace_days} 天</code>
        预计删除时间: <code>{delete_str}</code>（约 {hours} 小时后）

        您的媒体账号处于过期封禁宽限期，若未在删除前完成续期将被永久清理且无法恢复。请尽快前往 Mini App 使用{paid_methods}续期。""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.GRACE_DELETE_REMINDER,
        title=f"删号最后提醒：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=f"grace_delete:{payload.server_name}:{payload.media_name}",
    )


def _render_account_deleted(payload: AccountDeletedPayload) -> RenderedNotification:
    """渲染 ACCOUNT_DELETED 媒体账号永久删除确认通知。"""
    deleted_line = (
        TelegramMessage.render_html(
            t"清理时间: <code>{payload.deleted_at.strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )
        if payload.deleted_at is not None
        else ""
    )
    reason_text = payload.reason.strip() or "过期封禁宽限期届满未续期"
    html_text = TelegramMessage.render_html(
        t"""\
        🗑️ <b>媒体账号已删除</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        删除原因: <code>{reason_text}</code>
        {deleted_line:raw}

        对应媒体服务器账户及本地绑定记录已清理。如需继续使用，可在服务器开放注册时重新注册。""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.ACCOUNT_DELETED,
        title=f"媒体账号已删除：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=f"account_deleted:{payload.server_name}:{payload.media_name}",
    )


def _render_watch_time_renewal_eligible(
    payload: WatchTimeRenewalEligiblePayload,
) -> RenderedNotification:
    """渲染 WATCH_TIME_RENEWAL_ELIGIBLE 保活达标祝贺通知（遵守 ADR-0004）。"""
    watched_str = _format_duration_seconds(payload.watched_seconds)
    threshold_str = _format_duration_seconds(payload.threshold_seconds)
    exp_str = payload.expires_at.strftime("%Y-%m-%d %H:%M:%S")
    free_phrase = "免积分续期" if payload.economy.points_enabled else "免费续期"
    action_hint = (
        f"您已开启自动续期，系统将在到期时自动为您执行{free_phrase}；也可随时前往 Mini App 提前确认续期。"
        if payload.auto_renew_enabled
        else f"您可前往 Mini App 立即使用{free_phrase}延长账号有效期，也可开启自动续期。"
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🎉 <b>保活续期条件已达标</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        当前有效观看: <code>{watched_str}</code> / <code>{threshold_str}</code>（100%）
        当前有效期至: <code>{exp_str}</code>

        {action_hint}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.WATCH_TIME_RENEWAL_ELIGIBLE,
        title=f"保活续期达标：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=f"wt_eligible:{payload.server_name}:{payload.media_name}",
    )


def _render_billing_cycle_reset(
    payload: BillingCycleResetPayload,
) -> RenderedNotification:
    """渲染 BILLING_CYCLE_RESET 账期重置通知（严格按 ADR-0004 裁剪流量/积分/时长文案）。"""
    reset_str = payload.reset_at.strftime("%Y-%m-%d %H:%M:%S")
    next_line = (
        TelegramMessage.render_html(
            t"下次重置时间: <code>{payload.next_reset_at.strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )
        if payload.next_reset_at is not None
        else ""
    )

    extra_lines: list[str] = []
    if payload.economy.traffic_enabled:
        quota_label = (
            format_traffic_bytes(payload.new_quota_bytes)
            if payload.new_quota_bytes > 0
            else "不限流"
        )
        extra_lines.append(
            TelegramMessage.render_html(
                t"新周期基础流量配额: <code>{quota_label}</code>"
            )
        )
        if payload.cleared_pack_bytes > 0 and payload.economy.points_enabled:
            extra_lines.append(
                TelegramMessage.render_html(
                    t"当期流量包清零: <code>{format_traffic_bytes(payload.cleared_pack_bytes)}</code>"
                )
            )
        if payload.cleared_adjustment_bytes != 0:
            extra_lines.append(
                TelegramMessage.render_html(
                    t"当期临时调额已归零: <code>{format_traffic_bytes(abs(payload.cleared_adjustment_bytes))}</code>"
                )
            )
        if payload.unsuspended_traffic:
            extra_lines.append("状态恢复: <code>超流停用已随新周期自动解除</code>")

    extra_block = "\n".join(extra_lines)
    html_text = TelegramMessage.render_html(
        t"""\
        🔄 <b>账期重置通知</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        本次重置时间: <code>{reset_str}</code>
        {next_line:raw}
        {extra_block:raw}

        新的统计周期已开始，祝您观影愉快！""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.BILLING_CYCLE_RESET,
        title=f"账期已重置：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=f"cycle_reset:{payload.server_name}:{payload.media_name}:{reset_str}",
    )


def _render_traffic_quota_alert_card(
    event: NotificationEvent,
    *,
    header_html: str,
    summary_title: str,
    server_name: str,
    media_name: str,
    used_bytes: int,
    quota_bytes: int,
    pack_remaining_bytes: int,
    next_reset_at: datetime | None,
    footer_text: str,
    economy: EffectiveEconomyContext,
    action_url: str | None,
) -> RenderedNotification:
    """共享渲染流量配额 80%/100% 预警与超流自动停用通知卡片。"""
    used_str = format_traffic_bytes(used_bytes)
    quota_str = format_traffic_bytes(quota_bytes) if quota_bytes > 0 else "不限"
    pack_line = (
        TelegramMessage.render_html(
            t"\n当期流量包余量: <code>{format_traffic_bytes(pack_remaining_bytes)}</code>"
        )
        if pack_remaining_bytes > 0 and economy.effective_traffic_pack
        else ""
    )
    reset_str = (
        next_reset_at.strftime("%Y-%m-%d %H:%M")
        if next_reset_at is not None
        else "待下个账期同步"
    )
    html_text = TelegramMessage.render_html(
        t"""\
        {header_html:raw}

        服务器: <code>{server_name}</code>
        媒体账号: <code>{media_name}</code>
        本周期已用: <code>{used_str}</code> / <code>{quota_str}</code>{pack_line:raw}
        下次重置时间: <code>{reset_str}</code>

        {footer_text}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        event,
        title=f"{summary_title}：{server_name} / {media_name}",
        telegram_html=html_text,
        action_url=action_url or "/miniapp.html#accounts",
        dedupe_hint=f"{event.value}:{server_name}:{media_name}",
    )


def _render_traffic_quota_80(payload: TrafficQuota80Payload) -> RenderedNotification:
    """渲染 TRAFFIC_QUOTA_80 流量预警通知。"""
    pack_hint = (
        "若需补充额度可前往 Mini App 使用积分兑换流量包。"
        if payload.economy.effective_traffic_pack
        else "请注意合理规划观影清晰度。"
    )
    return _render_traffic_quota_alert_card(
        NotificationEvent.TRAFFIC_QUOTA_80,
        header_html="⚠️ <b>流量配额预警（已用达 80%）</b>",
        summary_title="流量配额预警 (80%)",
        server_name=payload.server_name,
        media_name=payload.media_name,
        used_bytes=payload.used_bytes,
        quota_bytes=payload.quota_bytes,
        pack_remaining_bytes=payload.pack_remaining_bytes,
        next_reset_at=payload.next_reset_at,
        footer_text=pack_hint,
        economy=payload.economy,
        action_url=payload.action_url,
    )


def _render_traffic_quota_100(payload: TrafficQuota100Payload) -> RenderedNotification:
    """渲染 TRAFFIC_QUOTA_100 基础流量耗尽软告警通知。"""
    pack_hint = (
        "您可在 Mini App 使用积分兑换当期流量包补充额度。"
        if payload.economy.effective_traffic_pack
        else "请注意控制播放码率，等待账期重置后将自动清零。"
    )
    return _render_traffic_quota_alert_card(
        NotificationEvent.TRAFFIC_QUOTA_100,
        header_html="🚨 <b>基础流量配额已耗尽（100%）</b>",
        summary_title="流量配额耗尽 (100%)",
        server_name=payload.server_name,
        media_name=payload.media_name,
        used_bytes=payload.used_bytes,
        quota_bytes=payload.quota_bytes,
        pack_remaining_bytes=payload.pack_remaining_bytes,
        next_reset_at=payload.next_reset_at,
        footer_text=f"当前处于软告警模式，暂不会触发停用与删号。{pack_hint}",
        economy=payload.economy,
        action_url=payload.action_url,
    )


def _render_traffic_auto_suspended(
    payload: TrafficAutoSuspendedPayload,
) -> RenderedNotification:
    """渲染 TRAFFIC_AUTO_SUSPENDED 超流自动停用通知（严格遵循 ADR-0002 与 ADR-0004）。"""
    guidance = _build_traffic_pack_guidance(payload.economy)
    return _render_traffic_quota_alert_card(
        NotificationEvent.TRAFFIC_AUTO_SUSPENDED,
        header_html="⛔ <b>流量已耗尽（100%），账号已超流停用</b>",
        summary_title="账号已超流停用",
        server_name=payload.server_name,
        media_name=payload.media_name,
        used_bytes=payload.used_bytes,
        quota_bytes=payload.quota_bytes,
        pack_remaining_bytes=payload.pack_remaining_bytes,
        next_reset_at=payload.next_reset_at,
        footer_text=(
            "本周期可用流量额度已耗尽，播放权限已临时停用（仅限制继续播放，不会触发删号）。"
            f"{guidance}"
        ),
        economy=payload.economy,
        action_url=payload.action_url,
    )


_UNSUSPEND_REASON_META: dict[str, tuple[str, str]] = {
    "traffic_pack": ("✅ 流量包已生效，超流停用已解除", "流量包兑换解封"),
    "cycle_reset": ("✅ 账期重置完成，超流停用已解除", "账期重置自动解封"),
    "admin_adjust": ("✅ 管理员调额生效，超流停用已解除", "管理员当期临时调额解封"),
    "server_policy_change": (
        "✅ 服务器限流策略变更，超流停用已解除",
        "服务器限流策略变更自动解封",
    ),
}


def _render_traffic_unsuspended(
    payload: TrafficUnsuspendedPayload,
) -> RenderedNotification:
    """渲染 TRAFFIC_UNSUSPENDED 超流停用解除通知，覆盖 4 类恢复路径与双状态隔离。"""
    header_title, reason_label = _UNSUSPEND_REASON_META.get(
        payload.recovery_reason,
        ("✅ 超流停用已解除", "流量额度恢复"),
    )
    reset_note = (
        f"{payload.next_reset_at.strftime('%Y-%m-%d %H:%M')} 账期重置时清零"
        if payload.next_reset_at is not None
        else "本周期重置时清零"
    )
    detail_lines: list[str] = [
        TelegramMessage.render_html(t"解除原因: <code>{reason_label}</code>")
    ]
    if payload.recovery_reason == "traffic_pack":
        pack_noun = "流量包" if payload.economy.points_enabled else "当期额外流量"
        if payload.added_bytes > 0:
            detail_lines.append(
                TelegramMessage.render_html(
                    t"本次增加: <code>+{format_traffic_bytes(payload.added_bytes)}</code>"
                )
            )
        if payload.total_pack_bytes > 0:
            detail_lines.append(
                TelegramMessage.render_html(
                    t"当期{pack_noun}余额: <code>{format_traffic_bytes(payload.total_pack_bytes)}</code>"
                )
            )
        detail_lines.append(
            TelegramMessage.render_html(
                t"有效说明: <code>仅限当前账期使用，将于 {reset_note}</code>"
            )
        )
    elif payload.recovery_reason == "cycle_reset":
        detail_lines.append(
            TelegramMessage.render_html(
                t"周期说明: <code>已进入新账期，当期流量用量已清零（下次重置: {reset_note}）</code>"
            )
        )
    elif payload.recovery_reason == "admin_adjust":
        if payload.adjustment_bytes != 0:
            sign = "+" if payload.adjustment_bytes > 0 else "-"
            detail_lines.append(
                TelegramMessage.render_html(
                    t"当期临时调额: <code>{sign}{format_traffic_bytes(abs(payload.adjustment_bytes))}</code>"
                )
            )
        if payload.reset_cycle_usage:
            detail_lines.append("当期用量: <code>已由管理员清零</code>")
        detail_lines.append(
            TelegramMessage.render_html(
                t"有效说明: <code>仅当期生效，将于 {reset_note}</code>"
            )
        )
    elif payload.recovery_reason == "server_policy_change":
        detail_lines.append(
            "策略说明: <code>管理员提高了配额或关闭了超流自动停用/限流开关</code>"
        )

    if payload.detail.strip():
        detail_lines.append(
            TelegramMessage.render_html(
                t"补充说明: {_truncate_text(payload.detail, 200)}"
            )
        )

    status_footer = (
        "超流停用标记已清除（超流停用本身不会触发删号）；但该账号当前仍处于过期封禁状态，需续期后才能恢复播放。"
        if payload.still_expiry_banned
        else "超流停用不会触发删号，您的播放权限已恢复，祝观影愉快！"
    )
    detail_block = "\n".join(detail_lines)
    html_text = TelegramMessage.render_html(
        t"""\
        <b>{header_title}</b>

        服务器: <code>{payload.server_name}</code>
        媒体账号: <code>{payload.media_name}</code>
        {detail_block:raw}

        {status_footer}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.TRAFFIC_UNSUSPENDED,
        title=f"超流停用已解除：{payload.server_name} / {payload.media_name}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=(
            f"traffic_unsuspended:{payload.server_name}:{payload.media_name}:{payload.recovery_reason}"
        ),
    )


_ADMIN_ACTION_LABELS: dict[str, tuple[str, str]] = {
    "warn": ("⚠️", "管理员警告通知"),
    "score_adjust": ("💠", "管理员积分调整通知"),
    "traffic_adjust": ("📶", "管理员当期流量调额通知"),
    "ban": ("🚫", "管理员账号封禁通知"),
    "unban": ("✅", "管理员账号解封通知"),
    "kick": ("👢", "管理员移出群组通知"),
    "delete": ("🗑️", "管理员删除账号通知"),
    "other": ("🛡️", "管理员账号管理回执"),
}


def _render_admin_user_action(payload: AdminUserActionPayload) -> RenderedNotification:
    """渲染 ADMIN_USER_ACTION 管理员操作回执与审计（遵守 ADR-0004 与零明文凭据）。"""
    emoji, header = _ADMIN_ACTION_LABELS.get(
        payload.action_type, ("🛡️", "管理员账号管理回执")
    )
    if (
        payload.action_type == "score_adjust" and not payload.economy.points_enabled
    ) or (
        payload.action_type == "traffic_adjust" and not payload.economy.traffic_enabled
    ):
        header = "管理员账号状态调整通知"

    user_display = payload.target_user_name or str(payload.target_user_id)
    lines: list[str] = [
        TelegramMessage.render_html(
            t'目标用户: <a href="tg://user?id={payload.target_user_id}">{user_display}</a> (<code>{payload.target_user_id}</code>)'
        ),
        TelegramMessage.render_html(
            t"操作管理员: <code>{payload.operator_name}</code>"
        ),
    ]
    if payload.server_name:
        lines.append(
            TelegramMessage.render_html(t"服务器: <code>{payload.server_name}</code>")
        )
    if payload.media_name:
        lines.append(
            TelegramMessage.render_html(t"媒体账号: <code>{payload.media_name}</code>")
        )
    if payload.warning_count is not None:
        lines.append(
            TelegramMessage.render_html(
                t"累计警告次数: <code>{payload.warning_count}</code>"
            )
        )

    show_score = (
        payload.economy.effective_points_warn
        if payload.action_type == "warn"
        else payload.economy.points_enabled
    )
    if show_score:
        if payload.score_delta is not None and payload.score_delta != 0:
            sign = "+" if payload.score_delta > 0 else ""
            lines.append(
                TelegramMessage.render_html(
                    t"积分变动: <code>{sign}{payload.score_delta}</code>"
                )
            )
        if payload.current_score is not None:
            lines.append(
                TelegramMessage.render_html(
                    t"当前积分余额: <code>{payload.current_score}</code>"
                )
            )

    if payload.economy.traffic_enabled:
        if payload.traffic_delta_bytes is not None and payload.traffic_delta_bytes != 0:
            sign = "+" if payload.traffic_delta_bytes > 0 else "-"
            lines.append(
                TelegramMessage.render_html(
                    t"当期流量临时调额: <code>{sign}{format_traffic_bytes(abs(payload.traffic_delta_bytes))}</code>"
                )
            )
        if payload.reset_cycle_usage:
            lines.append("当期已用流量: <code>已清零</code>")
        if payload.unsuspended_traffic:
            lines.append("超流状态: <code>已同步解除超流停用</code>")

    if payload.reason.strip():
        lines.append(
            TelegramMessage.render_html(
                t"操作说明: {_truncate_text(payload.reason, 240)}"
            )
        )

    detail_block = "\n".join(lines)
    html_text = TelegramMessage.render_html(
        t"""\
        {emoji} <b>{header}</b>

        {detail_block:raw}""",
        collapse_blank_lines=True,
    )
    scope_suffix = (
        f"（{payload.server_name} / {payload.media_name}）"
        if payload.server_name and payload.media_name
        else ""
    )
    return _make_rendered(
        NotificationEvent.ADMIN_USER_ACTION,
        title=f"{header}{scope_suffix}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=(
            f"admin_action:{payload.action_type}:{payload.target_user_id}:{payload.server_name or 'global'}"
        ),
    )


_SELF_ACTION_LABELS: dict[str, str] = {
    "register": "自助注册媒体账号回执",
    "renew": "账号续期成功回执",
    "reset_password": "重置密码安全回执",
    "redeem_code": "激活码核销回执",
    "toggle_auto_renew": "自动续期状态变更回执",
    "redeem_traffic_pack": "当期流量包兑换回执",
    "cancel_request": "求片撤销回执",
    "other": "自助操作回执",
}


def _render_self_action_receipt(
    payload: SelfActionReceiptPayload,
) -> RenderedNotification:
    """渲染 SELF_ACTION_RECEIPT 自助操作脱敏回执（严禁渲染或落库明文密码与激活码）。"""
    header = _SELF_ACTION_LABELS.get(payload.action_type, "自助操作回执")
    lines: list[str] = []
    if payload.server_name:
        lines.append(
            TelegramMessage.render_html(t"服务器: <code>{payload.server_name}</code>")
        )
    if payload.media_name:
        lines.append(
            TelegramMessage.render_html(t"媒体账号: <code>{payload.media_name}</code>")
        )

    if payload.action_type == "reset_password":
        lines.append(
            "安全说明: <code>密码已重置（安全凭据仅当场展示，系统不留存明文密码）</code>"
        )
    elif payload.code_kind is not None:
        kind_label = "注册码" if payload.code_kind == "signup" else "续期码"
        lines.append(
            TelegramMessage.render_html(
                t"凭据核销: <code>已使用{kind_label}（已脱敏，系统不留存明文）</code>"
            )
        )

    if payload.expires_at is not None:
        lines.append(
            TelegramMessage.render_html(
                t"当前有效期至: <code>{payload.expires_at.strftime('%Y-%m-%d %H:%M:%S')}</code>"
            )
        )
    if payload.cost_score > 0 and payload.economy.points_enabled:
        lines.append(
            TelegramMessage.render_html(t"消耗积分: <code>{payload.cost_score}</code>")
        )
    if payload.added_traffic_bytes > 0 and payload.economy.traffic_enabled:
        lines.append(
            TelegramMessage.render_html(
                t"新增当期流量: <code>+{format_traffic_bytes(payload.added_traffic_bytes)}</code>"
            )
        )
    if payload.auto_renew_enabled is not None and (
        payload.economy.effective_watch_time_renewal
        or payload.economy.effective_points_renew
    ):
        state_str = "已开启" if payload.auto_renew_enabled else "已关闭"
        lines.append(
            TelegramMessage.render_html(t"自动续期状态: <code>{state_str}</code>")
        )
    if payload.summary.strip():
        lines.append(
            TelegramMessage.render_html(t"摘要: {_truncate_text(payload.summary, 240)}")
        )

    body_block = "\n".join(lines) if lines else "操作已完成。"
    html_text = TelegramMessage.render_html(
        t"""\
        🧾 <b>{header}</b>

        {body_block:raw}""",
        collapse_blank_lines=True,
    )
    title_scope = (
        f"：{payload.server_name} / {payload.media_name}"
        if payload.server_name and payload.media_name
        else ""
    )
    return _make_rendered(
        NotificationEvent.SELF_ACTION_RECEIPT,
        title=f"{header}{title_scope}",
        telegram_html=html_text,
        action_url=payload.action_url or "/miniapp.html#accounts",
        dedupe_hint=(
            f"self_receipt:{payload.action_type}:{payload.server_name or 'global'}:{payload.media_name or ''}"
        ),
    )


def _safe_http_url(url: str | None) -> str | None:
    """校验并过滤不安全协议链接，仅放行 ``http://`` 与 ``https://`` URL。"""
    if not url:
        return None
    cleaned = url.strip()
    if cleaned.startswith(("https://", "http://")):
        return cleaned
    return None


def _render_labeled_text_block(
    label: str, text: str | None, *, limit: int = 360
) -> str:
    """将可选长文本安全渲染为带标签的单行 HTML 片段，为空时返回空串。"""
    cleaned = _truncate_text(text, limit)
    if not cleaned:
        return ""
    return TelegramMessage.render_html(t"{label}: {cleaned}")


def _format_bullet_preview(
    items: Sequence[str], *, label: str = "", max_items: int = 8
) -> str:
    """将文本列表格式化为安全转义的多行项目符号片段，超过上限时附汇总提示。"""
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return ""
    lines: list[str] = []
    if label:
        lines.append(TelegramMessage.render_html(t"<b>{label}:</b>"))
    for entry in cleaned[:max_items]:
        lines.append(TelegramMessage.render_html(t"• {_truncate_text(entry, 160)}"))
    if len(cleaned) > max_items:
        lines.append(
            TelegramMessage.render_html(
                t"• 其余 {len(cleaned) - max_items} 项已省略（共 {len(cleaned)} 项）"
            )
        )
    return "\n".join(lines)


def _render_ops_card(
    event: NotificationEvent,
    *,
    emoji: str,
    header: str,
    title: str,
    fields: Sequence[tuple[str, Any]],
    blocks: Sequence[str] = (),
    hashtag: str = "#运维告警",
    dedupe_hint: str | None = None,
    action_url: str | None = None,
) -> RenderedNotification:
    """共享渲染系统运维告警与定时摘要卡片，统一转义键值并折叠空白行。"""
    kv_lines: list[str] = []
    for label, raw_val in fields:
        if raw_val is None:
            continue
        text = str(raw_val).strip()
        if not text:
            continue
        kv_lines.append(TelegramMessage.render_html(t"{label}: <code>{text}</code>"))
    kv_block = "\n".join(kv_lines)
    extra_block = "\n\n".join(b for b in blocks if b and b.strip())
    html_text = TelegramMessage.render_html(
        t"""\
        {emoji} <b>{header}</b>

        {kv_block:raw}

        {extra_block:raw}

        {hashtag}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        event,
        title=title,
        telegram_html=html_text,
        action_url=action_url,
        dedupe_hint=dedupe_hint,
    )


def _render_arr_manual_interaction(
    payload: ArrManualInteractionPayload,
) -> RenderedNotification:
    """渲染 ARR_MANUAL_INTERACTION 告警。"""
    msg_block = _format_bullet_preview(payload.messages, label="状态明细")
    return _render_ops_card(
        NotificationEvent.ARR_MANUAL_INTERACTION,
        emoji="🛠️",
        header="Arr 需要人工干预",
        title=f"Arr 人工干预：{payload.server_name}",
        fields=[
            ("来源", payload.server_name),
            ("组件", payload.app_type.upper()),
            ("关联媒体", payload.media_title),
            ("任务标题", _truncate_text(payload.download_title, 140)),
            ("下载器", payload.download_client),
            ("状态", payload.download_status),
        ],
        blocks=[msg_block],
        hashtag="#运维告警 #Arr",
        dedupe_hint=f"arr_manual:{payload.server_name}:{payload.download_title or payload.media_title}",
        action_url=payload.action_url,
    )


def _render_arr_health_card(
    event: NotificationEvent,
    *,
    payload: ArrHealthIssuePayload | ArrHealthRestoredPayload,
    emoji: str,
    header: str,
) -> RenderedNotification:
    """共享渲染 Arr 健康检查异常或恢复通知。"""
    safe_wiki = _safe_http_url(payload.wiki_url)
    wiki_block = (
        TelegramMessage.render_html(t'参考文档: <a href="{safe_wiki}">Wiki 说明</a>')
        if safe_wiki
        else ""
    )
    msg_block = (
        TelegramMessage.render_html(t"详情: {_truncate_text(payload.message, 400)}")
        if payload.message.strip()
        else ""
    )
    return _render_ops_card(
        event,
        emoji=emoji,
        header=header,
        title=f"{header}：{payload.server_name} ({payload.check_type or payload.level})",
        fields=[
            ("来源", payload.server_name),
            ("组件", payload.app_type.upper()),
            ("级别", payload.level),
            ("检查项", payload.check_type),
        ],
        blocks=[msg_block, wiki_block],
        hashtag="#运维告警 #ArrHealth",
        dedupe_hint=f"arr_health:{payload.server_name}:{payload.check_type}",
        action_url=payload.action_url,
    )


def _render_arr_health_issue(payload: ArrHealthIssuePayload) -> RenderedNotification:
    """渲染 ARR_HEALTH_ISSUE 告警。"""
    return _render_arr_health_card(
        NotificationEvent.ARR_HEALTH_ISSUE,
        payload=payload,
        emoji="🩺",
        header="Arr 健康检查告警",
    )


def _render_arr_health_restored(
    payload: ArrHealthRestoredPayload,
) -> RenderedNotification:
    """渲染 ARR_HEALTH_RESTORED 恢复通知。"""
    return _render_arr_health_card(
        NotificationEvent.ARR_HEALTH_RESTORED,
        payload=payload,
        emoji="💚",
        header="Arr 健康检查已恢复",
    )


def _render_arr_test(payload: ArrTestPayload) -> RenderedNotification:
    """渲染 ARR_TEST Webhook 测试事件。"""
    msg_block = (
        TelegramMessage.render_html(t"说明: {_truncate_text(payload.message, 200)}")
        if payload.message.strip()
        else ""
    )
    return _render_ops_card(
        NotificationEvent.ARR_TEST,
        emoji="🧪",
        header="Arr Webhook 测试通知",
        title=f"Arr 测试：{payload.server_name}",
        fields=[
            ("来源", payload.server_name),
            ("组件", payload.app_type.upper()),
            ("实例名", payload.instance_name),
        ],
        blocks=[msg_block],
        hashtag="#运维测试 #Arr",
        dedupe_hint=f"arr_test:{payload.server_name}",
        action_url=payload.action_url,
    )


def _render_media_auth_risk(payload: MediaAuthRiskPayload) -> RenderedNotification:
    """渲染 MEDIA_AUTH_RISK 登录认证风险告警。"""
    return _render_ops_card(
        NotificationEvent.MEDIA_AUTH_RISK,
        emoji="🔐",
        header="媒体服登录异常风控告警",
        title=f"认证风控告警：{payload.server_name}",
        fields=[
            ("服务器", payload.server_name),
            ("尝试账号", payload.username or "未知"),
            (
                "失败次数",
                f"{max(1, payload.failure_count)} 次 / {max(1, payload.window_minutes)} 分钟",
            ),
            ("来源 IP", payload.remote_ip),
            ("客户端", payload.client_name),
            ("设备", payload.device_name),
        ],
        hashtag="#运维告警 #安全风控",
        dedupe_hint=f"media_auth_risk:{payload.server_name}:{payload.remote_ip or payload.username}",
        action_url=payload.action_url,
    )


def _render_media_user_locked_out(
    payload: MediaUserLockedOutPayload,
) -> RenderedNotification:
    """渲染 MEDIA_USER_LOCKED_OUT 媒体服用户锁定告警。"""
    return _render_ops_card(
        NotificationEvent.MEDIA_USER_LOCKED_OUT,
        emoji="🔒",
        header="媒体账号已被服务器锁定",
        title=f"账号锁定告警：{payload.server_name} / {payload.media_user_name}",
        fields=[
            ("服务器", payload.server_name),
            ("媒体账号", payload.media_user_name),
            ("外部 ID", payload.media_user_id),
        ],
        hashtag="#运维告警 #账号锁定",
        dedupe_hint=f"media_lockout:{payload.server_name}:{payload.media_user_name}",
        action_url=payload.action_url,
    )


def _render_media_task_failed(payload: MediaTaskFailedPayload) -> RenderedNotification:
    """渲染 MEDIA_TASK_FAILED 媒体服计划任务失败告警。"""
    return _render_ops_card(
        NotificationEvent.MEDIA_TASK_FAILED,
        emoji="⚠️",
        header="媒体服计划任务执行失败",
        title=f"任务失败：{payload.server_name} / {payload.task_name}",
        fields=[
            ("服务器", payload.server_name),
            ("任务名称", payload.task_name),
            ("状态", payload.task_state),
        ],
        blocks=[_render_labeled_text_block("错误详情", payload.error_message)],
        hashtag="#运维告警 #媒体服任务",
        dedupe_hint=f"media_task_failed:{payload.server_name}:{payload.task_name}",
        action_url=payload.action_url,
    )


def _render_media_subtitle_failed(
    payload: MediaSubtitleFailedPayload,
) -> RenderedNotification:
    """渲染 MEDIA_SUBTITLE_FAILED 字幕下载失败告警。"""
    return _render_ops_card(
        NotificationEvent.MEDIA_SUBTITLE_FAILED,
        emoji="💬",
        header="媒体字幕自动下载失败",
        title=f"字幕下载失败：{payload.item_name}",
        fields=[
            ("服务器", payload.server_name),
            ("剧集", payload.series_name),
            ("媒体项", payload.item_name),
            ("类型", payload.item_type),
            ("语言", payload.language),
        ],
        blocks=[
            _render_labeled_text_block("失败原因", payload.error_message, limit=320)
        ],
        hashtag="#运维告警 #字幕",
        dedupe_hint=f"media_sub_failed:{payload.server_name}:{payload.item_name}",
        action_url=payload.action_url,
    )


def _render_media_system_test(payload: MediaSystemTestPayload) -> RenderedNotification:
    """渲染 MEDIA_SYSTEM_TEST 媒体服 Webhook 测试通知。"""
    return _render_ops_card(
        NotificationEvent.MEDIA_SYSTEM_TEST,
        emoji="🧪",
        header="媒体服 Webhook 测试通知",
        title=f"媒体服测试：{payload.server_name}",
        fields=[
            ("服务器", payload.server_name),
            ("类型", payload.server_type),
            ("标题", payload.title),
        ],
        blocks=[_render_labeled_text_block("内容", payload.description, limit=300)],
        hashtag="#运维测试 #媒体服",
        dedupe_hint=f"media_test:{payload.server_name}",
        action_url=payload.action_url,
    )


def _render_mkv_process_failed(
    payload: MkvProcessFailedPayload,
) -> RenderedNotification:
    """渲染 MKV_PROCESS_FAILED 后台 MKV 处理失败告警。"""
    return _render_ops_card(
        NotificationEvent.MKV_PROCESS_FAILED,
        emoji="⚠️",
        header="MKV 自动清理失败（原文件保持完整）",
        title=f"MKV 处理失败：{_truncate_text(payload.file_path, 80)}",
        fields=[
            ("来源", payload.server_name),
            ("文件路径", _truncate_text(payload.file_path, 180)),
        ],
        blocks=[_render_labeled_text_block("诊断信息", payload.error_message)],
        hashtag="#运维告警 #MKV",
        dedupe_hint=f"mkv_failed:{payload.file_path}",
        action_url=payload.action_url,
    )


def _render_mkv_path_unmapped(payload: MkvPathUnmappedPayload) -> RenderedNotification:
    """渲染 MKV_PATH_UNMAPPED 路径映射缺失跳过告警。"""
    return _render_ops_card(
        NotificationEvent.MKV_PATH_UNMAPPED,
        emoji="⚠️",
        header="MKV 处理跳过（路径未映射）",
        title=f"MKV 路径未映射：{payload.server_name}",
        fields=[
            ("来源", payload.server_name),
            ("远端路径", _truncate_text(payload.remote_path, 180)),
        ],
        blocks=[_render_labeled_text_block("诊断说明", payload.diagnostic)],
        hashtag="#运维告警 #MKV",
        dedupe_hint=f"mkv_unmapped:{payload.server_name}:{payload.remote_path}",
        action_url=payload.action_url,
    )


def _render_nfo_process_failed(
    payload: NfoProcessFailedPayload,
) -> RenderedNotification:
    """渲染 NFO_PROCESS_FAILED NFO 元数据生成失败告警。"""
    return _render_ops_card(
        NotificationEvent.NFO_PROCESS_FAILED,
        emoji="📄",
        header="NFO 元数据生成失败",
        title=f"NFO 生成失败：{payload.nfo_kind}",
        fields=[
            ("来源", payload.server_name),
            ("NFO 类型", payload.nfo_kind),
            ("目标路径", _truncate_text(payload.target_path, 180)),
        ],
        blocks=[_render_labeled_text_block("异常详情", payload.error_message)],
        hashtag="#运维告警 #NFO",
        dedupe_hint=f"nfo_failed:{payload.target_path}",
        action_url=payload.action_url,
    )


def _render_translator_error(payload: TranslatorErrorPayload) -> RenderedNotification:
    """渲染 TRANSLATOR_ERROR AI 翻译/写回异常告警。"""
    return _render_ops_card(
        NotificationEvent.TRANSLATOR_ERROR,
        emoji="🤖",
        header="AI 元数据翻译异常",
        title=f"AI 翻译失败：{payload.server_name}",
        fields=[
            ("服务器", payload.server_name),
            ("媒体名称", payload.item_name),
            ("媒体 ID", payload.item_id),
            ("阶段", payload.stage),
        ],
        blocks=[_render_labeled_text_block("异常信息", payload.error_message)],
        hashtag="#运维告警 #Translator",
        dedupe_hint=f"translator_error:{payload.server_name}:{payload.stage}",
        action_url=payload.action_url,
    )


def _render_qb_share_limit_failed(
    payload: QbShareLimitFailedPayload,
) -> RenderedNotification:
    """渲染 QB_SHARE_LIMIT_FAILED 做种限制下发失败告警。"""
    return _render_ops_card(
        NotificationEvent.QB_SHARE_LIMIT_FAILED,
        emoji="🌱",
        header="qBittorrent 做种限制设置失败",
        title=f"qB 设种失败：{payload.qb_server_name or payload.server_name}",
        fields=[
            ("Arr 来源", payload.server_name),
            ("qB 实例", payload.qb_server_name),
            ("任务 Hash", payload.download_id),
        ],
        blocks=[_render_labeled_text_block("错误详情", payload.error_message)],
        hashtag="#运维告警 #qBittorrent",
        dedupe_hint=f"qb_share_limit:{payload.qb_server_name or payload.server_name}",
        action_url=payload.action_url,
    )


def _render_external_server_error(
    payload: ExternalServerErrorPayload,
) -> RenderedNotification:
    """渲染 EXTERNAL_SERVER_ERROR 外部服务离线或 5xx 告警。"""
    return _render_ops_card(
        NotificationEvent.EXTERNAL_SERVER_ERROR,
        emoji="🔴",
        header="外部服务调用异常",
        title=f"外部服务异常：{payload.server_name}",
        fields=[
            ("服务器", payload.server_name),
            ("类型", payload.server_type),
            ("操作", payload.operation),
            ("HTTP 状态码", payload.status_code),
        ],
        blocks=[_render_labeled_text_block("错误详情", payload.error_message)],
        hashtag="#运维告警 #服务异常",
        dedupe_hint=f"ext_error:{payload.server_type}:{payload.server_name}",
        action_url=payload.action_url,
    )


def _render_external_server_recovered(
    payload: ExternalServerRecoveredPayload,
) -> RenderedNotification:
    """渲染 EXTERNAL_SERVER_RECOVERED 外部服务恢复通知。"""
    return _render_ops_card(
        NotificationEvent.EXTERNAL_SERVER_RECOVERED,
        emoji="🟢",
        header="外部服务已恢复正常",
        title=f"外部服务恢复：{payload.server_name}",
        fields=[
            ("服务器", payload.server_name),
            ("类型", payload.server_type),
            ("操作", payload.operation),
        ],
        blocks=[_render_labeled_text_block("恢复说明", payload.detail, limit=260)],
        hashtag="#运维恢复 #服务状态",
        dedupe_hint=f"ext_recovered:{payload.server_type}:{payload.server_name}",
        action_url=payload.action_url,
    )


def _render_lifecycle_retry_alert(
    payload: LifecycleRetryAlertPayload,
) -> RenderedNotification:
    """渲染 LIFECYCLE_RETRY_ALERT 生命周期任务重试告警。"""
    return _render_ops_card(
        NotificationEvent.LIFECYCLE_RETRY_ALERT,
        emoji="🔁",
        header="账号生命周期任务进入重试",
        title=f"生命周期任务重试：{payload.server_name} / {payload.media_name}",
        fields=[
            ("服务器", payload.server_name),
            ("媒体账号", payload.media_name),
            ("Telegram ID", payload.user_id),
            ("任务类型", payload.job_kind),
            ("重试次数", payload.retry_count),
        ],
        blocks=[_render_labeled_text_block("错误详情", payload.error_message)],
        hashtag="#运维告警 #生命周期",
        dedupe_hint=f"lifecycle_retry:{payload.server_name}:{payload.user_id}:{payload.job_kind}",
        action_url=payload.action_url,
    )


def _render_nsfw_revoke_retry_alert(
    payload: NsfwRevokeRetryAlertPayload,
) -> RenderedNotification:
    """渲染 NSFW_REVOKE_RETRY_ALERT NSFW 撤权失败重试告警。"""
    return _render_ops_card(
        NotificationEvent.NSFW_REVOKE_RETRY_ALERT,
        emoji="🔞",
        header="NSFW 权限撤销失败，已转入自动重试",
        title=f"NSFW 撤权重试：{payload.server_name}",
        fields=[
            ("服务器", payload.server_name),
            ("待撤权数", payload.pending_count),
            ("失败数", payload.failed_count),
        ],
        blocks=[_render_labeled_text_block("异常详情", payload.error_message)],
        hashtag="#运维告警 #NSFW",
        dedupe_hint=f"nsfw_retry:{payload.server_name}",
        action_url=payload.action_url,
    )


def _render_db_backup_failed(payload: DbBackupFailedPayload) -> RenderedNotification:
    """渲染 DB_BACKUP_FAILED 数据库自动备份失败告警。"""
    return _render_ops_card(
        NotificationEvent.DB_BACKUP_FAILED,
        emoji="💾",
        header="数据库自动备份失败",
        title="数据库自动备份失败",
        fields=[("备份路径", _truncate_text(payload.backup_path, 180))],
        blocks=[_render_labeled_text_block("错误详情", payload.error_message)],
        hashtag="#运维告警 #数据库备份",
        dedupe_hint="db_backup_failed",
        action_url=payload.action_url,
    )


def _render_cleanup_inactive_summary(
    payload: CleanupInactiveSummaryPayload,
) -> RenderedNotification:
    """渲染 CLEANUP_INACTIVE_SUMMARY 非群成员清理汇总摘要。"""
    cleaned_block = _format_bullet_preview(payload.cleaned_users, label="已清理名单")
    failed_block = _format_bullet_preview(payload.failed_users, label="清理失败名单")
    return _render_ops_card(
        NotificationEvent.CLEANUP_INACTIVE_SUMMARY,
        emoji="🧹",
        header="离群非活跃成员清理摘要",
        title=f"离群清理完成：清理 {payload.cleaned_count} 名",
        fields=[
            ("检查总数", payload.checked_count),
            ("成功清理", payload.cleaned_count),
            ("失败数量", payload.failed_count),
        ],
        blocks=[cleaned_block, failed_block],
        hashtag="#运维摘要 #成员清理",
        dedupe_hint="cleanup_inactive_summary",
        action_url=payload.action_url,
    )


def _render_startup_calibration_summary(
    payload: StartupCalibrationSummaryPayload,
) -> RenderedNotification:
    """渲染 STARTUP_CALIBRATION_SUMMARY 启动校准非零变更摘要。"""
    detail_block = _format_bullet_preview(payload.details, label="校准明细")
    return _render_ops_card(
        NotificationEvent.STARTUP_CALIBRATION_SUMMARY,
        emoji="🧭",
        header="启动生命周期调度校准摘要",
        title=f"启动校准摘要：核验 {payload.total_accounts} 个账号",
        fields=[
            ("总核验账号", payload.total_accounts),
            ("补全删号时间", payload.backfilled_count),
            ("补跑账期重置", payload.reset_count),
            ("补跑过期封禁", payload.banned_count),
            ("补跑宽限删号", payload.deleted_count),
            ("重调度任务", payload.rescheduled_count),
            ("异常数", payload.error_count),
        ],
        blocks=[detail_block],
        hashtag="#运维摘要 #启动校准",
        dedupe_hint="startup_calibration_summary",
        action_url=payload.action_url,
    )


def _render_orphan_sessions_summary(
    payload: OrphanSessionsSummaryPayload,
) -> RenderedNotification:
    """渲染 ORPHAN_SESSIONS_SUMMARY 孤立挂起播放会话清理摘要。"""
    servers_str = ", ".join(s for s in payload.affected_servers if s.strip())
    detail_block = _format_bullet_preview(payload.details, label="截断明细")
    return _render_ops_card(
        NotificationEvent.ORPHAN_SESSIONS_SUMMARY,
        emoji="🎞️",
        header="孤立挂起播放会话清理摘要",
        title=f"孤立会话清理：截断 {payload.closed_count} 条",
        fields=[
            ("截断会话数", payload.closed_count),
            ("涉及服务器", servers_str),
        ],
        blocks=[detail_block],
        hashtag="#运维摘要 #会话清理",
        dedupe_hint="orphan_sessions_summary",
        action_url=payload.action_url,
    )


def _render_user_policy_sync_summary(
    payload: UserPolicySyncSummaryPayload,
) -> RenderedNotification:
    """渲染 USER_POLICY_SYNC_SUMMARY 批量策略同步摘要。"""
    return _render_ops_card(
        NotificationEvent.USER_POLICY_SYNC_SUMMARY,
        emoji="🛡️",
        header="默认 UserPolicy 批量同步摘要",
        title=f"策略同步完成：{payload.server_name}",
        fields=[
            ("服务器", payload.server_name),
            ("处理总数", payload.total_count),
            ("成功同步", payload.updated_count),
            ("跳过数量", payload.skipped_count),
            ("失败数量", payload.failed_count),
        ],
        blocks=[
            _render_labeled_text_block("失败摘要", payload.error_summary, limit=300)
        ],
        hashtag="#运维摘要 #UserPolicy",
        dedupe_hint=f"policy_sync:{payload.server_name}",
        action_url=payload.action_url,
    )


def _render_batch_traffic_release_summary(
    payload: BatchTrafficReleaseSummaryPayload,
) -> RenderedNotification:
    """渲染 BATCH_TRAFFIC_RELEASE_SUMMARY 批量解除超流停用运维摘要。"""
    accounts_block = _format_bullet_preview(
        payload.released_accounts, label="已解除账号"
    )
    return _render_ops_card(
        NotificationEvent.BATCH_TRAFFIC_RELEASE_SUMMARY,
        emoji="📶",
        header="批量解除超流停用摘要",
        title=f"批量解除超流：{payload.server_name}（{payload.released_count} 个）",
        fields=[
            ("服务器", payload.server_name),
            ("触发原因", payload.trigger_reason),
            ("已解除停用", payload.released_count),
            ("保持过期封禁", payload.skipped_banned_count),
            ("失败数量", payload.failed_count),
        ],
        blocks=[accounts_block],
        hashtag="#运维摘要 #超流解除",
        dedupe_hint=f"batch_traffic_release:{payload.server_name}:{payload.trigger_reason}",
        action_url=payload.action_url,
    )


def _render_telegram_delivery_degraded(
    payload: TelegramDeliveryDegradedPayload,
) -> RenderedNotification:
    """渲染 TELEGRAM_DELIVERY_DEGRADED Telegram 投递降级告警。"""
    return _render_ops_card(
        NotificationEvent.TELEGRAM_DELIVERY_DEGRADED,
        emoji="📡",
        header="Telegram 通知投递降级告警",
        title=f"投递降级：{payload.target_label}",
        fields=[
            ("目标通道", payload.target_label),
            ("事件类型", payload.event_type),
            ("失败次数", payload.failure_count),
        ],
        blocks=[_render_labeled_text_block("最后错误", payload.error_message)],
        hashtag="#运维告警 #Telegram投递",
        dedupe_hint=f"tg_degraded:{payload.target_label}",
        action_url=payload.action_url,
    )


def _format_leaderboard_html(entries: Sequence[ScoreLeaderboardEntry]) -> str:
    """安全渲染积分排行榜条目列表。"""
    lines: list[str] = []
    for idx, entry in enumerate(entries[:10], 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(idx, f"{idx}.")
        display_name = entry.user_name or str(entry.user_id)
        lines.append(
            TelegramMessage.render_html(
                t'{medal} <a href="tg://user?id={entry.user_id}">{display_name}</a> — <code>{entry.score}</code>'
            )
        )
    return "\n".join(lines)


def _render_score_settlement_report(
    payload: ScoreSettlementReportPayload,
) -> RenderedNotification:
    """渲染 SCORE_SETTLEMENT_REPORT 社区活跃积分结算榜单播报。"""
    mode_tag = "手动结算" if payload.is_manual else "每日定时结算"
    if payload.total_score_settled <= 0 and not payload.daily_top:
        html_text = TelegramMessage.render_html(
            t"""\
            📊 <b>每日积分结算报告</b>（{mode_tag}）

            今日无活跃积分变动。""",
            collapse_blank_lines=True,
        )
        return _make_rendered(
            NotificationEvent.SCORE_SETTLEMENT_REPORT,
            title="每日积分结算报告：今日无活跃积分变动",
            telegram_html=html_text,
            action_url=payload.action_url,
        )

    daily_block = (
        f"📈 <b>今日积分飙升榜 (Top 10)</b>\n{_format_leaderboard_html(payload.daily_top)}"
        if payload.daily_top
        else ""
    )
    overall_block = (
        f"🏆 <b>积分总榜 (Top 10)</b>\n{_format_leaderboard_html(payload.overall_top)}"
        if payload.overall_top
        else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        📊 <b>每日积分结算报告</b>（{mode_tag}）

        本次共结算 <b>{payload.total_score_settled}</b> 活跃度积分。

        {daily_block:raw}

        {overall_block:raw}""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.SCORE_SETTLEMENT_REPORT,
        title=f"每日积分结算报告：共结算 {payload.total_score_settled} 积分",
        telegram_html=html_text,
        action_url=payload.action_url,
    )


def _render_verification_reject_audit(
    payload: VerificationRejectAuditPayload,
) -> RenderedNotification:
    """渲染 VERIFICATION_REJECT_AUDIT 入群验证拒审/超时踢封审计。"""
    action_label = "封禁移出" if payload.action == "banned" else "踢出群组"
    display_name = payload.user_name or str(payload.user_id)
    cleaned_line = (
        TelegramMessage.render_html(
            t"关联账号清理: {_truncate_text(payload.cleaned_accounts_summary, 200)}"
        )
        if payload.cleaned_accounts_summary.strip()
        else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🛡️ <b>入群验证未通过清理审计</b>

        目标用户: <a href="tg://user?id={payload.user_id}">{display_name}</a> (<code>{payload.user_id}</code>)
        执行动作: <code>{action_label}</code>
        原因: <code>{payload.reason}</code>
        {cleaned_line:raw}

        #社区治理 #入群验证""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.VERIFICATION_REJECT_AUDIT,
        title=f"入群验证清理审计：{display_name} ({action_label})",
        telegram_html=html_text,
        action_url=payload.action_url,
        dedupe_hint=f"verify_reject:{payload.user_id}",
    )


def _render_member_left_cleanup_audit(
    payload: MemberLeftCleanupAuditPayload,
) -> RenderedNotification:
    """渲染 MEMBER_LEFT_CLEANUP_AUDIT 群成员退群联动清理审计。"""
    display_name = payload.user_name or str(payload.user_id)
    servers_str = ", ".join(s for s in payload.cleaned_servers if s.strip())
    servers_line = (
        TelegramMessage.render_html(t"已清理媒体服: <code>{servers_str}</code>")
        if servers_str
        else "已清理媒体服: <code>无绑定媒体账号</code>"
    )
    status_label = "清理完成" if payload.success else "部分失败待重试"
    detail_line = (
        TelegramMessage.render_html(t"详情: {_truncate_text(payload.detail, 240)}")
        if payload.detail.strip()
        else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        👋 <b>群成员退群自动清理审计</b>

        退群用户: <a href="tg://user?id={payload.user_id}">{display_name}</a> (<code>{payload.user_id}</code>)
        清理状态: <code>{status_label}</code>
        {servers_line:raw}
        {detail_line:raw}

        #社区治理 #退群清理""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.MEMBER_LEFT_CLEANUP_AUDIT,
        title=f"退群清理审计：{display_name} ({status_label})",
        telegram_html=html_text,
        action_url=payload.action_url,
        dedupe_hint=f"member_left:{payload.user_id}",
    )


def _render_system_test(payload: SystemTestPayload) -> RenderedNotification:
    """渲染 SYSTEM_TEST 通路实测通知。"""
    category_line = (
        TelegramMessage.render_html(t"路由类别: <code>{payload.category}</code>")
        if payload.category
        else ""
    )
    target_line = (
        TelegramMessage.render_html(
            t"目标路由: <code>{payload.target_description}</code>"
        )
        if payload.target_description
        else ""
    )
    operator_line = (
        TelegramMessage.render_html(t"触发人: <code>{payload.operator_name}</code>")
        if payload.operator_name
        else ""
    )
    time_line = (
        TelegramMessage.render_html(
            t"测试时间: <code>{payload.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}</code>"
        )
        if payload.triggered_at is not None
        else ""
    )
    message_line = (
        TelegramMessage.render_html(t"测试说明: {_truncate_text(payload.message, 300)}")
        if payload.message
        else ""
    )
    html_text = TelegramMessage.render_html(
        t"""\
        🔔 <b>系统通知通路测试</b>

        来源: <code>{payload.server_name}</code>
        {category_line:raw}
        {target_line:raw}
        {operator_line:raw}
        {time_line:raw}
        {message_line:raw}

        #系统测试""",
        collapse_blank_lines=True,
    )
    return _make_rendered(
        NotificationEvent.SYSTEM_TEST,
        title=f"系统通知通路测试：{payload.server_name}",
        telegram_html=html_text,
        action_url=payload.action_url,
        dedupe_hint=f"system_test:{payload.server_name}:{payload.category}",
    )


_RENDERERS: dict[NotificationEvent, Callable[[Any], RenderedNotification]] = {
    NotificationEvent.SONARR_DOWNLOAD: _render_sonarr_download,
    NotificationEvent.SONARR_SERIES_ADD: _render_sonarr_series_add,
    NotificationEvent.SONARR_GRAB: _render_sonarr_grab,
    NotificationEvent.SONARR_RENAME: _render_sonarr_rename,
    NotificationEvent.SONARR_DELETE: _render_sonarr_delete,
    NotificationEvent.RADARR_DOWNLOAD: _render_radarr_download,
    NotificationEvent.RADARR_MOVIE_ADD: _render_radarr_movie_add,
    NotificationEvent.RADARR_GRAB: _render_radarr_grab,
    NotificationEvent.RADARR_RENAME: _render_radarr_rename,
    NotificationEvent.RADARR_DELETE: _render_radarr_delete,
    NotificationEvent.EMBY_LIBRARY_NEW: _render_emby_library_new,
    NotificationEvent.JELLYFIN_LIBRARY_NEW: _render_jellyfin_library_new,
    NotificationEvent.MEDIA_LIBRARY_DELETED: _render_media_library_deleted,
    NotificationEvent.REQUEST_SUBMIT: _render_request_submit,
    NotificationEvent.REQUEST_APPROVED: _render_request_approved,
    NotificationEvent.REQUEST_REJECTED: _render_request_rejected,
    NotificationEvent.REQUEST_COMPLETED: _render_request_completed,
    NotificationEvent.REQUEST_ARR_FAILED: _render_request_arr_failed,
    NotificationEvent.REQUEST_EXPIRED: _render_request_expired,
    NotificationEvent.PRE_EXPIRY_REMINDER: _render_pre_expiry_reminder,
    NotificationEvent.AUTO_RENEW_SUCCESS: _render_auto_renew_success,
    NotificationEvent.AUTO_RENEW_FAILED: _render_auto_renew_failed,
    NotificationEvent.EXPIRY_BAN: _render_expiry_ban,
    NotificationEvent.GRACE_DELETE_REMINDER: _render_grace_delete_reminder,
    NotificationEvent.ACCOUNT_DELETED: _render_account_deleted,
    NotificationEvent.WATCH_TIME_RENEWAL_ELIGIBLE: _render_watch_time_renewal_eligible,
    NotificationEvent.BILLING_CYCLE_RESET: _render_billing_cycle_reset,
    NotificationEvent.TRAFFIC_QUOTA_80: _render_traffic_quota_80,
    NotificationEvent.TRAFFIC_QUOTA_100: _render_traffic_quota_100,
    NotificationEvent.TRAFFIC_AUTO_SUSPENDED: _render_traffic_auto_suspended,
    NotificationEvent.TRAFFIC_UNSUSPENDED: _render_traffic_unsuspended,
    NotificationEvent.ADMIN_USER_ACTION: _render_admin_user_action,
    NotificationEvent.SELF_ACTION_RECEIPT: _render_self_action_receipt,
    NotificationEvent.ARR_MANUAL_INTERACTION: _render_arr_manual_interaction,
    NotificationEvent.ARR_HEALTH_ISSUE: _render_arr_health_issue,
    NotificationEvent.ARR_HEALTH_RESTORED: _render_arr_health_restored,
    NotificationEvent.ARR_TEST: _render_arr_test,
    NotificationEvent.MEDIA_AUTH_RISK: _render_media_auth_risk,
    NotificationEvent.MEDIA_USER_LOCKED_OUT: _render_media_user_locked_out,
    NotificationEvent.MEDIA_TASK_FAILED: _render_media_task_failed,
    NotificationEvent.MEDIA_SUBTITLE_FAILED: _render_media_subtitle_failed,
    NotificationEvent.MEDIA_SYSTEM_TEST: _render_media_system_test,
    NotificationEvent.MKV_PROCESS_FAILED: _render_mkv_process_failed,
    NotificationEvent.MKV_PATH_UNMAPPED: _render_mkv_path_unmapped,
    NotificationEvent.NFO_PROCESS_FAILED: _render_nfo_process_failed,
    NotificationEvent.TRANSLATOR_ERROR: _render_translator_error,
    NotificationEvent.QB_SHARE_LIMIT_FAILED: _render_qb_share_limit_failed,
    NotificationEvent.EXTERNAL_SERVER_ERROR: _render_external_server_error,
    NotificationEvent.EXTERNAL_SERVER_RECOVERED: _render_external_server_recovered,
    NotificationEvent.LIFECYCLE_RETRY_ALERT: _render_lifecycle_retry_alert,
    NotificationEvent.NSFW_REVOKE_RETRY_ALERT: _render_nsfw_revoke_retry_alert,
    NotificationEvent.DB_BACKUP_FAILED: _render_db_backup_failed,
    NotificationEvent.CLEANUP_INACTIVE_SUMMARY: _render_cleanup_inactive_summary,
    NotificationEvent.STARTUP_CALIBRATION_SUMMARY: _render_startup_calibration_summary,
    NotificationEvent.ORPHAN_SESSIONS_SUMMARY: _render_orphan_sessions_summary,
    NotificationEvent.USER_POLICY_SYNC_SUMMARY: _render_user_policy_sync_summary,
    NotificationEvent.BATCH_TRAFFIC_RELEASE_SUMMARY: _render_batch_traffic_release_summary,
    NotificationEvent.TELEGRAM_DELIVERY_DEGRADED: _render_telegram_delivery_degraded,
    NotificationEvent.SCORE_SETTLEMENT_REPORT: _render_score_settlement_report,
    NotificationEvent.VERIFICATION_REJECT_AUDIT: _render_verification_reject_audit,
    NotificationEvent.MEMBER_LEFT_CLEANUP_AUDIT: _render_member_left_cleanup_audit,
    NotificationEvent.SYSTEM_TEST: _render_system_test,
}

# 模块加载期强制校验所有 62 个 NotificationEvent 均已注册对应强类型渲染器
assert len(_RENDERERS) == len(NotificationEvent) and set(_RENDERERS) == set(
    NotificationEvent
), f"通知渲染器未覆盖全部事件: {set(NotificationEvent) - set(_RENDERERS)}"


class NotificationRenderer:
    """强类型通知渲染器入口类。"""

    @staticmethod
    def registered_events() -> tuple[NotificationEvent, ...]:
        """返回当前渲染器已注册处理的通知事件元组。"""
        return tuple(_RENDERERS.keys())

    @staticmethod
    def render(payload: NotificationPayload) -> RenderedNotification:
        """将强类型通知载荷渲染为 Telegram HTML 与站内摘要产物。

        Args:
            payload: 与 ``NotificationEvent`` 对应的强类型载荷实例。

        Returns:
            包含标题、纯文本摘要、转义后 Telegram HTML 及可选附件/按钮的 ``RenderedNotification``。

        Raises:
            ValueError: 当载荷对应的通知事件尚未注册渲染器时抛出。
        """
        renderer = _RENDERERS.get(payload.event)
        if renderer is None:
            raise ValueError(f"未注册通知事件渲染器: {payload.event.value}")
        return renderer(payload)


def render_notification(payload: NotificationPayload) -> RenderedNotification:
    """渲染强类型通知载荷为统一的 Telegram HTML 与纯文本摘要结果。"""
    return NotificationRenderer.render(payload)
