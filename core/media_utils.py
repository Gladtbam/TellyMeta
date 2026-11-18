"""媒体账号通用锁、UUID 规范化与请求头/字符串清洗辅助工具"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from typing import Any

# 匹配 Emby/Jellyfin Authorization 请求头中的 Token 字段（group(1) 为双引号包裹值，group(2) 为未加引号值）
AUTH_TOKEN_RE = re.compile(
    r'(?:^|[,\s])Token\s*=\s*(?:"([^"]*)"|([^,\s]+))',
    re.IGNORECASE,
)

# 媒体账号级进程内互斥锁表：若与服务器心跳锁嵌套，调用方必须先获取账号锁再获取心跳锁
_ACCOUNT_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def clean_str(value: Any, *, empty_markers: tuple[str, ...] = ()) -> str | None:
    """将任意输入值归一化为去首尾空白的非空字符串，空串或命中忽略标记时返回 ``None``

    Args:
        value: 待清洗的原始字符串、UUID 或数值。
        empty_markers: 额外视为空值的占位字符串集合（如 Nginx 日志占位符 ``"-"``）。

    Returns:
        去除首尾空白后的非空字符串；为空或命中忽略标记时返回 ``None``。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or (empty_markers and text in empty_markers):
        return None
    return text


def get_account_lock(user_id: int, server_id: int) -> asyncio.Lock:
    """获取同一媒体账号的串行化异步锁（供跨服务并发更新账号状态时共用）

    Args:
        user_id: Telegram 用户 ID。
        server_id: 媒体服务器实例 ID。

    Returns:
        对应账号维度 ``(user_id, server_id)`` 的进程内 ``asyncio.Lock``。
    """
    key = (user_id, server_id)
    lock = _ACCOUNT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ACCOUNT_LOCKS[key] = lock
    return lock


def expand_media_id_candidates(raw_media_id: str) -> list[str]:
    """扩展媒体账号 ID 匹配候选集，兼容 Jellyfin 带连字符与无连字符 UUID 写法

    Args:
        raw_media_id: 媒体服务器侧上报的原始用户 ID 字符串。

    Returns:
        按查找优先级去重排列的候选 ID 列表。
    """
    cleaned = raw_media_id.strip()
    candidates = [cleaned]
    compact = cleaned.replace("-", "").lower()
    if len(compact) == 32:
        with contextlib.suppress(ValueError):
            parsed = uuid.UUID(hex=compact)
            candidates.extend([parsed.hex, str(parsed), parsed.hex.upper()])
    # 去重同时保留查找优先级顺序
    return list(dict.fromkeys(candidates))


def media_ids_match(first: str | None, second: str | None) -> bool:
    """判断两个媒体平台侧用户 ID 是否指向同一外部媒体账号（忽略 UUID 连字符与大小写差异）

    Args:
        first: 第一个媒体账号外部 ID。
        second: 第二个媒体账号外部 ID。

    Returns:
        两者归一化后存在交集时返回 ``True``，任一为空或不匹配返回 ``False``。
    """
    c1 = clean_str(first)
    c2 = clean_str(second)
    if c1 is None or c2 is None:
        return False
    return bool(
        set(expand_media_id_candidates(c1)) & set(expand_media_id_candidates(c2))
    )
