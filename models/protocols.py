from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol, runtime_checkable, TypeVar
from typing_extensions import Self


@runtime_checkable
class Dumpable(Protocol):
    """声明一个类可以被 Pydantic 的 model_dump 和 model_copy 方法使用。"""
    def model_dump(
        self,
        *,
        mode: str | Literal['json', 'python'] = 'json',
        context: Any | None = None,
        by_alias: bool | None = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        exclude_computed_fields: bool = False,
        round_trip: bool = False,
        warnings: bool | Literal['none', 'warn', 'error'] = True,
        fallback: Callable[[Any], Any] | None = None,
        serialize_as_any: bool = False,
    ) -> dict[str, Any]:
        ...

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        ...

@runtime_checkable
class Library(Dumpable, Protocol):
    """定义媒体库的协议"""
    Name: str
    ItemId: str | None

LibraryT = TypeVar("LibraryT", bound=Library)

@runtime_checkable
class BaseItem(Dumpable, Protocol):
    """定义媒体项的协议"""
    Name: str
    Id: str
    SortName: str | None = None
    Overview: str | None = None
    Genres: list
    ProviderIds: dict

BaseItemT_co = TypeVar("BaseItemT_co", bound=BaseItem, covariant=True)

@runtime_checkable
class Policy(Dumpable, Protocol):
    """定义用户策略的协议"""
    IsAdministrator: bool
    IsHidden: bool
    IsDisabled: bool
    BlockedTags: list

PolicyT = TypeVar("PolicyT", bound=Policy)

@runtime_checkable
class User(Dumpable, Protocol[PolicyT]):
    """定义用户的协议"""
    Id: str
    Name: str
    HasPassword: bool
    Policy: PolicyT

UserT = TypeVar("UserT", bound=User)

@runtime_checkable
class DeviceInfo(Dumpable, Protocol):
    """定义设备信息的协议"""
    LastUserName: str
    LastUserId: str

DeviceInfoT = TypeVar("DeviceInfoT", bound=DeviceInfo)

@runtime_checkable
class PublicSystemInfo(Dumpable, Protocol):
    """定义公共信息的协议"""
    Version: str
    Id: str

PublicSystemInfoT = TypeVar("PublicSystemInfoT", bound=PublicSystemInfo)
