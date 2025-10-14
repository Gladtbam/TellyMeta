from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol, runtime_checkable, TypeVar
from typing_extensions import Self


@runtime_checkable
class Dumpable(Protocol):
    """声明一个类可以被 Pydantic 的 model_dump 和 model_copy 方法使用。"""
    def model_dump(
        self,
        *,
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
class BaseItem(Dumpable, Protocol):
    """定义媒体项的协议"""
    Name: str
    Id: str
    SortName: str
    Overview: str
    Genres: list
    ProviderIds: dict

BaseItemT = TypeVar("BaseItemT", bound=BaseItem)

@runtime_checkable
class Policy(Dumpable, Protocol):
    """定义用户策略的协议"""
    IsAdministrator: bool
    IsHidden: bool
    IsDisabled: bool
    BlockedTags: list

PolicyT = TypeVar("PolicyT", bound=Policy)

@runtime_checkable
class QueryResult(Dumpable, Protocol[BaseItemT]):
    """定义查询结果的协议"""
    Items: list[BaseItemT]
    TotalRecordCount: int

QueryResultT = TypeVar("QueryResultT", bound=QueryResult)

@runtime_checkable
class User(Dumpable, Protocol[PolicyT]):
    """定义用户的协议"""
    Id: str
    Name: str
    HasPassword: bool
    Policy: PolicyT

UserT = TypeVar("UserT", bound=User)
