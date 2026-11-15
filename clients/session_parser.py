"""Emby 与 Jellyfin `/Sessions` 结构化活跃会话解析辅助模块"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import TypeAdapter

from services.media_service import ActiveSession

_SESSION_TOKEN_KEYS: tuple[str, ...] = (
    "AccessToken",
    "ApiKey",
    "api_key",
    "Token",
    "X-Emby-Token",
    "X-MediaBrowser-Token",
)
_PLAY_SESSION_ID_KEYS: tuple[str, ...] = ("PlaySessionId", "playSessionId")
_URL_TOKEN_PARAM_NAMES: tuple[str, ...] = ("api_key", "apikey", "token")
_PLAY_SESSION_PARAM_NAMES: tuple[str, ...] = ("playsessionid",)
_MEDIA_SOURCE_URL_FIELDS: tuple[str, ...] = ("DirectStreamUrl", "TranscodingUrl")
# 匹配 Emby/Jellyfin Authorization 请求头中的 Token 字段（group(1) 为双引号包裹值，group(2) 为未加引号值）
_AUTH_TOKEN_RE = re.compile(
    r'(?:^|[,\s])Token\s*=\s*(?:"([^"]*)"|([^,\s]+))',
    re.IGNORECASE,
)


class _PlayStateLike(Protocol):
    """兼容 Emby 与 Jellyfin 播放状态字段的只读协议"""

    @property
    def IsPaused(self) -> bool | None: ...

    @property
    def PositionTicks(self) -> int | None: ...


class _NowPlayingItemLike(Protocol):
    """兼容 Emby 与 Jellyfin 正在播放媒体项的只读协议"""

    @property
    def Id(self) -> str | UUID | None: ...


class _SessionLike(Protocol):
    """兼容 Emby ``SessionInfo`` 与 Jellyfin ``SessionInfoDto`` 的只读协议"""

    Id: str | None
    DeviceId: str | None

    @property
    def UserId(self) -> str | UUID | None: ...

    @property
    def NowPlayingItem(self) -> _NowPlayingItemLike | None: ...

    @property
    def PlayState(self) -> _PlayStateLike | None: ...


def _clean_str(value: Any) -> str | None:
    """将字符串或 UUID 值归一化为非空字符串，空白字符串转为 ``None``

    Args:
        value: 待归一化的字段值。

    Returns:
        去除首尾空白后的非空字符串；为 ``None`` 或全空白时返回 ``None``。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _get_field(source: Any, name: str) -> Any:
    """统一从原始映射或模型对象读取指定字段，不存在时返回 ``None``

    Args:
        source: 原始 JSON 字典或 Pydantic 模型实例。
        name: 目标字段名。

    Returns:
        字段对应的值；当对象为 ``None`` 或字段缺失时返回 ``None``。
    """
    if source is None:
        return None
    if isinstance(source, Mapping):
        return cast("Mapping[str, Any]", source).get(name)
    return getattr(source, name, None)


def _first_non_empty_field(sources: Sequence[Any], keys: tuple[str, ...]) -> str | None:
    """按候选源与字段名顺序提取首个非空字符串值

    Args:
        sources: 优先序排列的原始字典或模型对象集合。
        keys: 按优先级排列的字段名元组。

    Returns:
        命中字段后的首个非空字符串；均缺失或为空白时返回 ``None``。
    """
    for source in sources:
        if source is None:
            continue
        for key in keys:
            cleaned = _clean_str(_get_field(source, key))
            if cleaned is not None:
                return cleaned
    return None


def _collect_media_sources(
    raw_session: Mapping[str, Any],
    session: _SessionLike,
) -> list[Any]:
    """按当前播放源优先顺序收集 ``MediaSource`` 候选对象或原始字典

    优先保留原始 JSON 字典，避免 Pydantic 默认 ``extra="ignore"``
    丢弃 Jellyfin ``DirectStreamUrl`` 等未列入 OpenAPI 模型的字段。

    Args:
        raw_session: 单条会话对应的原始 JSON 映射。
        session: 通过 ``TypeAdapter`` 校验后的会话模型实例。

    Returns:
        先包含 ``PlayState.MediaSource``、再包含 ``NowPlayingItem.MediaSources`` 元素的列表。
    """
    media_sources: list[Any] = []

    raw_play_state = raw_session.get("PlayState")
    play_state_source = _get_field(raw_play_state, "MediaSource") or _get_field(
        session.PlayState, "MediaSource"
    )
    if play_state_source is not None:
        media_sources.append(play_state_source)

    raw_now_playing = raw_session.get("NowPlayingItem")
    item_sources = _get_field(raw_now_playing, "MediaSources") or _get_field(
        session.NowPlayingItem, "MediaSources"
    )
    if isinstance(item_sources, Sequence) and not isinstance(item_sources, str | bytes):
        for item_source in item_sources:
            if item_source is not None:
                media_sources.append(item_source)

    return media_sources


def _extract_query_param(url: str, lowered_names: tuple[str, ...]) -> str | None:
    """从播放或转码 URL 的查询参数中按优先级提取指定参数值

    Args:
        url: 媒体流相对路径或完整 URL。
        lowered_names: 按优先级排列的小写参数名列表（查询键会统一转小写后匹配）。

    Returns:
        匹配到的首个非空参数值，未命中返回 ``None``。
    """
    query = urlsplit(url).query
    if not query:
        return None
    params = parse_qs(query, keep_blank_values=False)
    lowered_params: dict[str, list[str]] = {
        key.lower(): values for key, values in params.items()
    }
    for name in lowered_names:
        for raw_val in lowered_params.get(name, ()):
            cleaned = _clean_str(raw_val)
            if cleaned is not None:
                return cleaned

    return None


def _extract_param_from_media_source_urls(
    media_source: Any,
    lowered_param_names: tuple[str, ...],
) -> str | None:
    """从单个 ``MediaSource`` 的直链或转码 URL 查询参数中提取指定字段

    按 ``DirectStreamUrl`` 优先、``TranscodingUrl`` 其次的顺序检索流 URL 查询参数。

    Args:
        media_source: 单个 ``MediaSource`` 字典或模型对象。
        lowered_param_names: 按优先级排列的小写查询参数名元组。

    Returns:
        提取到的首个非空参数值，未命中则返回 ``None``。
    """
    for url_field in _MEDIA_SOURCE_URL_FIELDS:
        url = _clean_str(_get_field(media_source, url_field))
        if url is None:
            continue
        extracted = _extract_query_param(url, lowered_param_names)
        if extracted is not None:
            return extracted
    return None


def _extract_token_from_auth_header(header_value: str) -> str | None:
    """解析 ``Authorization`` 请求头中的 ``MediaBrowser/Emby Token`` 令牌

    Args:
        header_value: ``RequiredHttpHeaders`` 中的 ``Authorization`` 请求头原始文本。

    Returns:
        提取并归一化后的令牌字符串；未匹配有效 ``Token`` 字段时返回 ``None``。
    """
    match = _AUTH_TOKEN_RE.search(header_value)
    if match is None:
        return None
    # 兼容双引号包裹 Token="..." 与未加引号 Token=... 两种写法
    return _clean_str(match.group(1) or match.group(2))


def _extract_token_from_headers(headers: Any) -> str | None:
    """从 ``RequiredHttpHeaders`` 字典提取 Emby/Jellyfin 客户端鉴权令牌

    Args:
        headers: ``MediaSource.RequiredHttpHeaders`` 映射或 ``None``。

    Returns:
        按 ``X-Emby-Token`` > ``X-MediaBrowser-Token`` > ``Authorization`` 顺序
        提取的首个非空令牌；无有效令牌返回 ``None``。
    """
    if not isinstance(headers, Mapping):
        return None
    typed_headers = cast("Mapping[Any, Any]", headers)
    lowered: dict[str, str] = {}
    for key, value in typed_headers.items():
        cleaned = _clean_str(value)
        if cleaned is not None:
            lowered[str(key).lower()] = cleaned

    for header_name in ("x-emby-token", "x-mediabrowser-token"):
        if header_name in lowered:
            return lowered[header_name]

    auth_header = lowered.get("authorization")
    if auth_header is not None:
        return _extract_token_from_auth_header(auth_header)
    return None


def _extract_auth_token(
    direct_sources: Sequence[Any],
    media_sources: Sequence[Any],
) -> str | None:
    """提取会话关联的客户端鉴权令牌或 API Key

    Args:
        direct_sources: 会话顶层原始字典与校验模型候选列表（优先级高于媒体源）。
        media_sources: 当前播放媒体源与条目媒体源候选列表（Header 优先于流 URL 参数）。

    Returns:
        首个可用的非空客户端令牌；均缺失时返回 ``None``。
    """
    top_level_token = _first_non_empty_field(direct_sources, _SESSION_TOKEN_KEYS)
    if top_level_token is not None:
        return top_level_token

    for media_source in media_sources:
        header_token = _extract_token_from_headers(
            _get_field(media_source, "RequiredHttpHeaders")
        )
        if header_token is not None:
            return header_token

        url_token = _extract_param_from_media_source_urls(
            media_source,
            _URL_TOKEN_PARAM_NAMES,
        )
        if url_token is not None:
            return url_token

    return None


def _extract_play_session_id(
    direct_sources: Sequence[Any],
    media_sources: Sequence[Any],
) -> str | None:
    """提取媒体服务器生成的 ``PlaySessionId``

    Args:
        direct_sources: 按优先级排列的 ``PlayState`` 与顶层会话候选源列表。
        media_sources: 候选 ``MediaSource`` 列表（当直接字段缺失时回退检索流 URL 参数）。

    Returns:
        提取到的非空 ``PlaySessionId``；均缺失时返回 ``None``。
    """
    direct_id = _first_non_empty_field(direct_sources, _PLAY_SESSION_ID_KEYS)
    if direct_id is not None:
        return direct_id

    for media_source in media_sources:
        url_id = _extract_param_from_media_source_urls(
            media_source,
            _PLAY_SESSION_PARAM_NAMES,
        )
        if url_id is not None:
            return url_id

    return None


def parse_active_sessions[SessionT: _SessionLike](
    data: Any,
    session_adapter: TypeAdapter[list[SessionT]],
) -> list[ActiveSession]:
    """将 Emby 或 Jellyfin `/Sessions` 原始响应列表解析为结构化 ``ActiveSession`` 列表

    Args:
        data: `/Sessions` 接口返回的反序列化 JSON 对象。
        session_adapter: 目标平台会话列表的预编译 ``TypeAdapter``。

    Returns:
        过滤空闲会话后得到的 ``ActiveSession`` 列表（仅保留存在 ``NowPlayingItem`` 的会话）。
    """
    parsed_sessions = session_adapter.validate_python(data)
    # 同步遍历原始 JSON 列表以读取未进入 OpenAPI 模型的字段（如 PlaySessionId/AccessToken）；
    # 若调用方传入非 list 可迭代对象导致无法按位置对齐原始字典，则退回 None 占位以继续使用已校验模型
    raw_items: Sequence[Any] = (
        cast("Sequence[Any]", data)
        if isinstance(data, list)
        and len(cast("Sequence[Any]", data)) == len(parsed_sessions)
        else [None] * len(parsed_sessions)
    )
    active_sessions: list[ActiveSession] = []

    for session, raw_item in zip(parsed_sessions, raw_items, strict=True):
        if session.NowPlayingItem is None:
            continue

        raw_session: Mapping[str, Any] = (
            cast("Mapping[str, Any]", raw_item) if isinstance(raw_item, Mapping) else {}
        )
        play_state = session.PlayState
        raw_play_state = raw_session.get("PlayState")
        is_paused = bool(play_state.IsPaused) if play_state is not None else False
        position_ticks = play_state.PositionTicks if play_state is not None else None
        media_sources = _collect_media_sources(raw_session, session)

        active_sessions.append(
            ActiveSession(
                user_id=_clean_str(session.UserId),
                session_id=_clean_str(session.Id),
                play_session_id=_extract_play_session_id(
                    (raw_play_state, play_state, raw_session, session),
                    media_sources,
                ),
                device_id=_clean_str(session.DeviceId),
                auth_token=_extract_auth_token(
                    (raw_session, session),
                    media_sources,
                ),
                item_id=_clean_str(session.NowPlayingItem.Id),
                is_paused=is_paused,
                position_ticks=position_ticks,
            )
        )

    return active_sessions
