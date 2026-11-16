"""Nginx 反代流媒体请求日志模型与流媒体过滤规则"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

_MEDIA_SEGMENT_EXTENSIONS = frozenset(
    {
        "mp4",
        "mkv",
        "webm",
        "avi",
        "mov",
        "m2ts",
        "ts",
        "m4s",
        "m4a",
        "m4v",
        "aac",
        "mp3",
        "flac",
        "ogg",
        "opus",
        "wav",
        "iso",
    }
)

_EXCLUDED_PATH_KEYWORDS = (
    "/images/",
    "/subtitles/",
    "/trickplay/",
    "/attachments/",
    "/web/",
)

_DIRECT_STREAM_NAME_RE = re.compile(
    r"^(original|stream|universal)(?:\.[a-z0-9]+)?$",
    re.IGNORECASE,
)
_ITEM_DOWNLOAD_SEGMENTS = frozenset({"download", "file"})
_AUTH_TOKEN_RE = re.compile(
    r'(?:^|[,\s])Token\s*=\s*(?:"([^"]*)"|([^,\s]+))',
    re.IGNORECASE,
)
_AUTH_DEVICE_ID_RE = re.compile(
    r'(?:^|[,\s])DeviceId\s*=\s*(?:"([^"]*)"|([^,\s]+))',
    re.IGNORECASE,
)


def format_traffic_bytes(num_bytes: int | None) -> str:
    """将字节数转换为最合适的 1024 进制可读流量字符串（B / KiB / MiB / GiB / TiB）

    Args:
        num_bytes: 原始字节数；为 ``None`` 或负数时按 ``0`` 处理。

    Returns:
        保留两位小数且带单位的可读流量字符串。
    """
    size_int = max(0, int(num_bytes or 0))
    if size_int == 0:
        return "0.00 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size_int)
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.2f} {units[idx]}"


def _clean_str(value: str | int | None) -> str | None:
    """将输入值归一化为去首尾空白的非空字符串，空白串返回 ``None``"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_auth_header_param(
    pattern: re.Pattern[str], *headers: str | None
) -> str | None:
    """从多个授权请求头文本中按优先级提取指定字段（兼容引号与非引号写法）

    Args:
        pattern: 用于匹配目标键值对的编译后正则表达式。
        *headers: 按优先级排列的原始请求头字符串。

    Returns:
        命中后的首个非空字段值，未命中返回 ``None``。
    """
    for header in headers:
        if not header:
            continue
        match = pattern.search(header)
        if match is not None:
            extracted = _clean_str(match.group(1) or match.group(2))
            if extracted is not None:
                return extracted
    return None


class UrlInfo(BaseModel):
    """Nginx URL 与请求上下文解析结果"""

    item_id: str | None = Field(None, description="媒体项 ID（如 48057 或 UUID）")
    api_key: str | None = Field(None, description="API Key / 认证令牌")
    device_id: str | None = Field(None, description="Device ID")


class Nginx(BaseModel):
    """Nginx JSON UDP 日志模型，支持多服务器识别、认证提取与流媒体过滤"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    timestamp: datetime
    http_ip: str = ""
    method: str
    url: str
    status: int
    body_bytes_sent: int  # 实际发出响应体字节数
    http_range: str | None = ""

    # 多服识别字段（允许 Nginx 模板传入 server_id、server_name 或 host）
    server_id: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("server_id", "serverId"),
    )
    server_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("server_name", "serverName"),
    )
    host: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "host", "http_host", "server_host", "server_addr"
        ),
    )

    # 可选请求头/顶层认证与设备标识字段
    api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("api_key", "ApiKey", "token"),
    )
    device_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("device_id", "DeviceId", "deviceId"),
    )
    http_x_emby_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "http_x_emby_token", "x_emby_token", "X-Emby-Token"
        ),
    )
    http_x_mediabrowser_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "http_x_mediabrowser_token",
            "x_mediabrowser_token",
            "X-MediaBrowser-Token",
        ),
    )
    http_authorization: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "http_authorization", "authorization", "Authorization"
        ),
    )
    http_x_emby_authorization: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "http_x_emby_authorization",
            "x_emby_authorization",
            "X-Emby-Authorization",
        ),
    )

    @property
    def resolved_server_id(self) -> int | None:
        """提取并规范化整型的服务器 ID（无法解析为正整数时返回 ``None``）"""
        if self.server_id is None or isinstance(self.server_id, bool):
            return None
        if isinstance(self.server_id, int):
            return self.server_id if self.server_id > 0 else None
        text = self.server_id.strip()
        if text.isdigit():
            val = int(text)
            return val if val > 0 else None
        return None

    @property
    def resolved_server_name(self) -> str | None:
        """提取去除空白后的服务器名称"""
        return _clean_str(self.server_name)

    @property
    def resolved_host(self) -> str | None:
        """提取规范化小写 Host（不含协议头），用于与服务器 URL 做匹配"""
        cleaned = _clean_str(self.host)
        if not cleaned:
            return None
        if "://" in cleaned:
            parsed = urlparse(cleaned)
            return parsed.netloc.lower() or None
        return cleaned.lower()

    @property
    def body_human(self) -> str:
        """自动将 body_bytes_sent 转换为最合适的 1024 进制单位（B, KiB, MiB, GiB, TiB）"""
        return format_traffic_bytes(self.body_bytes_sent)

    def _match_media_item_id(self, path: str) -> str | None:
        """判断请求路径是否为音视频直连流或 HLS/DASH 媒体分片，并提取媒体项 ID

        Args:
            path: URL 路径部分（不含 query）。

        Returns:
            属于有效音视频直连流、下载流或媒体分片时返回媒体项 ID，否则返回 ``None``。
        """
        lowered_path = path.lower()
        if any(keyword in lowered_path for keyword in _EXCLUDED_PATH_KEYWORDS):
            return None

        parts = [p for p in path.split("/") if p]
        if not parts:
            return None

        # 兼容 /emby/... 前缀与无前缀路径；音视频流路径至少包含 <resource>/<item_id>/<segment> 三段
        offset = 1 if parts[0].lower() == "emby" else 0
        if len(parts) - offset < 3:
            return None

        resource = parts[offset].lower()
        item_id = _clean_str(parts[offset + 1])
        if not item_id:
            return None

        tail = parts[offset + 2 :]
        filename = tail[-1].lower()
        ext = filename.rsplit(".", 1)[-1] if "." in filename else ""

        if resource == "items":
            if len(tail) == 1 and filename in _ITEM_DOWNLOAD_SEGMENTS:
                return item_id
            return None

        if resource not in ("videos", "audio"):
            return None

        # 明确排除播放列表索引（.m3u8/.mpd）与外挂/分段字幕文件，仅统计真实媒体流载体
        if ext in ("m3u8", "mpd", "srt", "ass", "ssa", "vtt", "sub", "idx", "sup"):
            return None

        if len(tail) == 1 and _DIRECT_STREAM_NAME_RE.match(filename):
            return item_id

        if ext in _MEDIA_SEGMENT_EXTENSIONS:
            return item_id

        return None

    @property
    def is_media_stream(self) -> bool:
        """判断当前日志是否属于可计费的音视频直连流或 HLS/DASH 媒体分片响应

        仅统计 HTTP ``GET`` 且状态码为 ``200``/``206``、响应字节大于 ``0`` 的媒体流请求，
        排除封面图片、字幕、播放列表清单、静态网页及普通 API 请求。
        """
        if self.method.upper() != "GET":
            return False
        if self.status not in (200, 206):
            return False
        if self.body_bytes_sent <= 0:
            return False
        path = urlparse(self.url).path
        return self._match_media_item_id(path) is not None

    @property
    def url_info(self) -> UrlInfo:
        """从 URL 路径、Query 参数与可选请求头中提取媒体项 ID、认证令牌与设备 ID

        Returns:
            包含 ``item_id``、``api_key`` 与 ``device_id`` 的 ``UrlInfo`` 对象。
        """
        parsed = urlparse(self.url)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=False)
        lowered_query = {k.lower(): v for k, v in query.items()}

        def first_query(*keys: str) -> str | None:
            for key in keys:
                values = lowered_query.get(key.lower())
                if values:
                    cleaned = _clean_str(values[0])
                    if cleaned is not None:
                        return cleaned
            return None

        item_id = self._match_media_item_id(path)

        # 令牌优先级：URL Query 参数 -> 顶层显式 Token/X-Emby-Token 字段 -> Authorization 请求头 Token
        api_key = (
            first_query(
                "api_key", "apikey", "token", "x-emby-token", "x-mediabrowser-token"
            )
            or _clean_str(self.api_key)
            or _clean_str(self.http_x_emby_token)
            or _clean_str(self.http_x_mediabrowser_token)
            or _extract_auth_header_param(
                _AUTH_TOKEN_RE,
                self.http_x_emby_authorization,
                self.http_authorization,
            )
        )

        # 设备标识优先级：URL Query 参数 -> 顶层显式 device_id -> Authorization 请求头 DeviceId
        device_id = (
            first_query("deviceid", "device_id")
            or _clean_str(self.device_id)
            or _extract_auth_header_param(
                _AUTH_DEVICE_ID_RE,
                self.http_x_emby_authorization,
                self.http_authorization,
            )
        )

        return UrlInfo(item_id=item_id, api_key=api_key, device_id=device_id)
