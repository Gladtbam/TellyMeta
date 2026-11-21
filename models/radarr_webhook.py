"""Radarr Webhook 回调数据模型（基于上游 Radarr Webhook 源码维护，兼容 Python 3.15 与 extra='allow'）。"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from core.config import genre_mapping


def normalize_genres(value: Any) -> Any:
    """按全局类型映射表规范化影片/剧集类型列表。

    Args:
        value: 原始类型字段输入，可能为 ``list[str]``、``None`` 或其他透传值。

    Returns:
        当输入为 ``None`` 时返回空列表；为列表时返回映射后的类型列表；其余原样返回交由 Pydantic 校验。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        return value
    return [genre_mapping.get(genre, genre) for genre in value]


class ArrWebhookBaseModel(BaseModel):
    """Arr 系（Radarr / Sonarr）Webhook 通用基类。

    统一开启 ``extra='allow'``，确保上游新增字段或嵌套对象扩展属性不会导致反序列化失败。
    """

    model_config = ConfigDict(extra="allow")


class Languages(ArrWebhookBaseModel):
    """Radarr / Sonarr Webhook 语言信息（对应 ``Language``）。"""

    id: int
    name: str | None = None


class MediaCover(ArrWebhookBaseModel):
    """媒体封面图片信息（对应 ``WebhookImage``）。"""

    # MediaCoverTypes 经 Newtonsoft CamelCase 序列化（如 poster / fanart / banner / clearlogo 等）
    coverType: str
    url: str | None = None
    remoteUrl: str | None = None


class CustomFormatResource(ArrWebhookBaseModel):
    """自定义格式条目（对应 ``WebhookCustomFormat``，兼顾扩展字段）。"""

    id: int
    name: str | None = None
    # 以下两项来自早期 REST Resource 对齐，Webhook 本身仅发 id/name，保留可选默认以兼容旧测试数据
    includeCustomFormatWhenRenaming: bool | None = None
    specifications: list[dict[str, Any]] = Field(default_factory=list)


class CustomFormatInfoDto(ArrWebhookBaseModel):
    """自定义格式汇总信息（对应 ``WebhookCustomFormatInfo``）。"""

    customFormats: list[CustomFormatResource] = Field(default_factory=list)
    customFormatScore: int | None = None

    @property
    def formats_str(self) -> str:
        """将已匹配的自定义格式名称拼接为逗号分隔字符串。"""
        if not self.customFormats:
            return ""
        return ",".join(_format.name for _format in self.customFormats if _format.name)


class Movie(ArrWebhookBaseModel):
    """Radarr Webhook 电影信息（对应 ``WebhookMovie``）。"""

    id: int
    title: str
    year: int = 0
    # 仅三参构造函数会填充 filePath，多数 Webhook 中为 null/省略
    filePath: str | None = None
    releaseDate: str | None = None
    folderPath: str
    tmdbId: int = 0
    # Test 事件中以下元数据字段可能为 null/省略
    imdbId: str | None = None
    overview: str | None = None
    genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    images: list[MediaCover] = Field(default_factory=list)
    originalLanguage: Languages | None = None

    @field_validator("genres", mode="before")
    @classmethod
    def validate_genres(cls, value: Any) -> Any:
        """按全局类型映射表转换电影类型名称。"""
        return normalize_genres(value)


class RemoteMovie(ArrWebhookBaseModel):
    """Radarr 远端匹配电影信息（对应 ``WebhookRemoteMovie``）。"""

    tmdbId: int
    imdbId: str | None = None
    title: str
    year: int = 0


class MediaInfoDto(ArrWebhookBaseModel):
    """媒体文件编码与音视频流信息（对应 ``WebhookMovieFileMediaInfo`` / ``WebhookEpisodeFileMediaInfo``）。"""

    audioChannels: float
    audioCodec: str | None = None
    # 上游 C# 属性名为 AudioLanguages（复数）；兼容历史模型单数拼写 audioLanguage
    audioLanguages: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("audioLanguages", "audioLanguage"),
    )
    height: int
    width: int
    subtitles: list[str] = Field(default_factory=list)
    videoCodec: str | None = None
    videoDynamicRange: str | None = None
    videoDynamicRangeType: str | None = None


class FileInfoDto(ArrWebhookBaseModel):
    """Radarr 电影文件信息（对应 ``WebhookMovieFile``）。"""

    id: int
    relativePath: str
    path: str
    quality: str
    qualityVersion: int
    # 无发布组名或手工导入时可能为 null/省略
    releaseGroup: str | None = None
    sceneName: str | None = None
    indexerFlags: str | None = None
    size: int
    dateAdded: datetime
    languages: list[Languages] = Field(default_factory=list)
    mediaInfo: MediaInfoDto | None = None
    sourcePath: str | None = None
    # 升级覆盖旧文件（DeletedFiles）时可能携带回收站路径
    recycleBinPath: str | None = None


class RadarrRelease(ArrWebhookBaseModel):
    """Radarr 种子/发布信息（合并 ``WebhookRelease`` 与 ``WebhookGrabbedRelease``）。"""

    quality: str | None = None
    qualityVersion: int | None = None
    releaseGroup: str | None = None
    releaseTitle: str | None = None
    indexer: str | None = None
    size: int | None = None
    customFormatScore: int | None = None
    customFormats: list[str] = Field(default_factory=list)
    languages: list[Languages] = Field(default_factory=list)
    indexerFlags: list[str] = Field(default_factory=list)


class RenamedMovieFiles(FileInfoDto):
    """重命名电影文件记录（对应继承自 ``WebhookMovieFile`` 的 ``WebhookRenamedMovieFile``）。"""

    previousRelativePath: str
    previousPath: str


class DownloadInfo(ArrWebhookBaseModel):
    """下载客户端任务摘要信息（对应 ``WebhookDownloadClientItem``）。"""

    quality: str
    qualityVersion: int
    title: str | None = None
    indexer: str | None = None
    size: int


class DownloadStatusMessage(ArrWebhookBaseModel):
    """需要人工干预时的下载状态告警明细（对应 ``WebhookDownloadStatusMessage``）。"""

    title: str | None = None
    messages: list[str] = Field(default_factory=list)


class WebhookBase[EventT: str](ArrWebhookBaseModel):
    """Arr Webhook 通用事件基类（对应 ``WebhookPayload``）。"""

    eventType: EventT
    instanceName: str | None = None
    applicationUrl: str | None = None


class RadarrWebhookGrabPayload(WebhookBase[Literal["Grab"]]):
    """Radarr Grab 事件（对应 ``WebhookGrabPayload``）。"""

    eventType: Literal["Grab"]
    movie: Movie
    remoteMovie: RemoteMovie
    release: RadarrRelease
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    customFormatInfo: CustomFormatInfoDto | None = None


class RadarrWebhookDownloadPayload(WebhookBase[Literal["Download"]]):
    """Radarr Download / Upgrade 入库事件（对应 ``WebhookImportPayload``）。"""

    eventType: Literal["Download"]
    movie: Movie
    remoteMovie: RemoteMovie | None = None
    movieFile: FileInfoDto
    release: RadarrRelease | None = None
    isUpgrade: bool = False
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    deletedFiles: list[FileInfoDto] = Field(default_factory=list)
    customFormatInfo: CustomFormatInfoDto | None = None


class RadarrWebhookAddedPayload(WebhookBase[Literal["MovieAdded"]]):
    """Radarr 电影添加事件（对应 ``WebhookAddedPayload``）。"""

    eventType: Literal["MovieAdded"]
    movie: Movie
    # AddMovieMethod 枚举：manual / list / collection
    addMethod: str


class RadarrWebhookMovieFileDeletePayload(WebhookBase[Literal["MovieFileDelete"]]):
    """Radarr 电影文件删除事件（对应 ``WebhookMovieFileDeletePayload``）。"""

    eventType: Literal["MovieFileDelete"]
    movie: Movie
    movieFile: FileInfoDto
    # DeleteMediaFileReason 枚举：missingFromDisk / manual / upgrade / noLinkedEpisodes / manualOverride
    deleteReason: str


class RadarrWebhookMovieDeletePayload(WebhookBase[Literal["MovieDelete"]]):
    """Radarr 电影删除事件（对应 ``WebhookMovieDeletePayload``）。"""

    eventType: Literal["MovieDelete"]
    movie: Movie
    deletedFiles: bool
    movieFolderSize: int | None = None


class RadarrWebhookRenamePayload(WebhookBase[Literal["Rename"]]):
    """Radarr 重命名事件（对应 ``WebhookRenamePayload``）。"""

    eventType: Literal["Rename"]
    movie: Movie
    renamedMovieFiles: list[RenamedMovieFiles] = Field(default_factory=list)


class RadarrWebhookHealthPayload(WebhookBase[Literal["Health", "HealthRestored"]]):
    """Radarr 健康检查及恢复事件（对应 ``WebhookHealthPayload``）。"""

    eventType: Literal["Health", "HealthRestored"]
    # HealthCheckResult 枚举：ok / notice / warning / error
    level: str
    message: str
    type: str
    wikiUrl: str | None = None


class RadarrWebhookApplicationUpdatePayload(WebhookBase[Literal["ApplicationUpdate"]]):
    """Radarr 应用更新事件（对应 ``WebhookApplicationUpdatePayload``）。"""

    eventType: Literal["ApplicationUpdate"]
    message: str
    previousVersion: str
    newVersion: str


class RadarrWebhookManualInteractionPayload(
    WebhookBase[Literal["ManualInteractionRequired"]]
):
    """Radarr 需要人工干预事件（对应 ``WebhookManualInteractionPayload``）。"""

    eventType: Literal["ManualInteractionRequired"]
    movie: Movie | None = None
    downloadInfo: DownloadInfo
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    downloadStatus: str | None = None
    downloadStatusMessages: list[DownloadStatusMessage] = Field(default_factory=list)
    customFormatInfo: CustomFormatInfoDto | None = None
    release: RadarrRelease | None = None


class RadarrWebhookTestPayload(WebhookBase[Literal["Test"]]):
    """Radarr Webhook 测试事件（上游复用 ``WebhookGrabPayload`` 发送测试样例）。"""

    eventType: Literal["Test"]
    movie: Movie | None = None
    remoteMovie: RemoteMovie | None = None
    release: RadarrRelease | None = None
    downloadClient: str | None = None
    downloadClientType: str | None = None
    downloadId: str | None = None
    customFormatInfo: CustomFormatInfoDto | None = None


RadarrPayload = Annotated[
    RadarrWebhookGrabPayload
    | RadarrWebhookDownloadPayload
    | RadarrWebhookAddedPayload
    | RadarrWebhookMovieFileDeletePayload
    | RadarrWebhookMovieDeletePayload
    | RadarrWebhookRenamePayload
    | RadarrWebhookHealthPayload
    | RadarrWebhookApplicationUpdatePayload
    | RadarrWebhookManualInteractionPayload
    | RadarrWebhookTestPayload,
    Field(discriminator="eventType"),
]
