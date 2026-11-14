from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol, Self, runtime_checkable
from uuid import UUID


@runtime_checkable
class Dumpable(Protocol):
    """声明一个类可以被 Pydantic 的 model_dump 和 model_copy 方法使用。"""

    def model_dump(
        self,
        *,
        mode: str | Literal["json", "python"] = "json",
        context: Any | None = None,
        by_alias: bool | None = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        exclude_computed_fields: bool = False,
        round_trip: bool = False,
        warnings: bool | Literal["none", "warn", "error"] = True,
        fallback: Callable[[Any], Any] | None = None,
        serialize_as_any: bool = False,
    ) -> dict[str, Any]: ...

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self: ...


@runtime_checkable
class Library(Dumpable, Protocol):
    """定义媒体库的协议"""

    Name: str | None
    ItemId: str | None


@runtime_checkable
class BaseItem(Dumpable, Protocol):
    """定义媒体项的协议"""

    Name: str | None
    SortName: str | None = None
    Overview: str | None = None
    Genres: list[str] | None
    ProviderIds: dict[str, str] | None
    ParentIndexNumber: int | None = None
    PremiereDate: datetime | None = None

    @property
    def Id(self) -> str | UUID | None:
        """媒体项唯一标识（兼容 Emby 字符串与 Jellyfin UUID）。"""
        ...

    @property
    def SeriesId(self) -> str | UUID | None:
        """所属剧集标识（兼容 Emby 字符串与 Jellyfin UUID）。"""
        ...

    @property
    def Type(self) -> str | None:
        """媒体项类型（只读协变属性，兼容 StrEnum 与 str）。"""
        ...


@runtime_checkable
class Policy(Dumpable, Protocol):
    """定义用户策略的协议"""

    IsAdministrator: bool | None
    IsHidden: bool | None
    IsDisabled: bool | None
    BlockedTags: list[str] | None


@runtime_checkable
class User[PolicyT: Policy](Dumpable, Protocol):
    """定义用户的协议"""

    Name: str | None
    HasPassword: bool | None
    Policy: PolicyT | None

    @property
    def Id(self) -> str | UUID | None:
        """用户唯一标识（兼容 Emby 字符串与 Jellyfin UUID）。"""
        ...


@runtime_checkable
class DeviceInfo(Dumpable, Protocol):
    """定义设备信息的协议"""

    LastUserName: str | None

    @property
    def LastUserId(self) -> str | UUID | None:
        """最近登录用户标识（兼容 Emby 字符串与 Jellyfin UUID）。"""
        ...


@runtime_checkable
class PublicSystemInfo(Dumpable, Protocol):
    """定义公共信息的协议"""

    Version: str | None
    Id: str | None
