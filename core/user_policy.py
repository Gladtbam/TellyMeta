"""单服默认 UserPolicy 模板解析、校验、保留字段保护与 Emby/Jellyfin 合并规则模块"""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from models.emby import UserPolicy as EmbyUserPolicy
from models.jellyfin import UserPolicy as JellyfinUserPolicy

# 受系统或安全边界独占控制的 UserPolicy 字段（小写键 -> 中文拒绝说明）
RESERVED_USER_POLICY_FIELDS: dict[str, str] = {
    "isadministrator": "安全限制：不允许在默认 UserPolicy 中配置 IsAdministrator",
    "isdisabled": "生命周期限制：IsDisabled 由到期封禁与超流停用独占控制，不允许在默认 UserPolicy 中覆盖",
    "enableallfolders": "媒体库权限限制：EnableAllFolders 由单服 NSFW 配置独占管理，不允许在默认 UserPolicy 中覆盖",
    "enabledfolders": "媒体库权限限制：EnabledFolders 由单服 NSFW 配置独占管理，不允许在默认 UserPolicy 中覆盖",
    "excludedsubfolders": "媒体库权限限制：ExcludedSubFolders 由单服 NSFW 配置独占管理，不允许在默认 UserPolicy 中覆盖",
    "blockedmediafolders": "媒体库权限限制：BlockedMediaFolders 由单服 NSFW 配置独占管理，不允许在默认 UserPolicy 中覆盖",
    "authenticationproviderid": "认证限制：不允许覆盖内部认证提供程序字段 AuthenticationProviderId",
    "passwordresetproviderid": "认证限制：不允许覆盖内部密码重置提供程序字段 PasswordResetProviderId",
}

_JELLYFIN_REQUIRED_VALIDATION_DEFAULTS: dict[str, str] = {
    "AuthenticationProviderId": "__validation__",
    "PasswordResetProviderId": "__validation__",
}


class DefaultUserPolicySpec(BaseModel):
    """服务器默认媒体账号 UserPolicy 精简高频配置与可选高级 JSON 覆盖"""

    enabled: bool = False
    enable_media_playback: bool = True
    enable_video_transcoding: bool = True
    enable_audio_transcoding: bool = True
    enable_playback_remuxing: bool = True
    enable_content_downloading: bool = True
    enable_remote_access: bool = True
    enable_remote_control: bool = False
    is_hidden: bool = True
    max_bitrate_mbps: float = Field(default=0.0, ge=0.0)
    max_concurrent_streams: int = Field(default=0, ge=0)
    extra_json: str = ""

    @field_validator("max_bitrate_mbps")
    @classmethod
    def _validate_bitrate_finite(cls, value: float) -> float:
        """校验远程码率上限（Mbps）必须为非负有限实数。"""
        if not math.isfinite(value) or value < 0:
            raise ValueError("远程码率上限（Mbps）必须为不小于 0 的有限数值")
        return float(value)

    @field_validator("extra_json")
    @classmethod
    def _normalize_extra_json(cls, value: str) -> str:
        """规范化高级 JSON 文本首尾空白。"""
        return (value or "").strip()


def _validate_policy_model(
    overrides: dict[str, Any],
    policy_cls: type[EmbyUserPolicy] | type[JellyfinUserPolicy],
    server_label: str,
    *,
    required_defaults: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按目标平台 UserPolicy Pydantic 模型校验覆盖字段名与值类型，并返回 JSON 归一化字典。"""
    unknown_keys = [key for key in overrides if key not in policy_cls.model_fields]
    if unknown_keys:
        raise ValueError(
            f"高级 JSON 包含未知的 {server_label} UserPolicy 字段: {', '.join(unknown_keys)}"
        )

    validation_input = dict(overrides)
    if required_defaults:
        for req_key, placeholder in required_defaults.items():
            validation_input.setdefault(req_key, placeholder)

    try:
        validated = policy_cls.model_validate(validation_input)
    except ValidationError as exc:
        first_err = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first_err.get("loc", ()))
        msg = str(first_err.get("msg") or "字段值类型不合法")
        raise ValueError(
            f"高级 JSON 字段 {loc or '(root)'} 校验失败 ({server_label}): {msg}"
        ) from exc

    dumped = validated.model_dump(mode="json")
    return {key: dumped[key] for key in overrides}


def parse_extra_policy_json(
    raw_json: str | None,
    server_type: str | None = None,
) -> dict[str, Any]:
    """解析并严格校验管理员填写的高级 JSON Policy 覆盖项

    Args:
        raw_json: 原始 JSON 字符串（为空或纯空白时返回空字典）。
        server_type: 目标媒体服务器类型（``emby`` / ``jellyfin``）；为空时同时按两套模型校验。

    Returns:
        经目标平台 ``UserPolicy`` 模型校验并归一化为 JSON 原生类型的覆盖字典。

    Raises:
        ValueError: 当 JSON 语法非法、非对象类型、命中受保护保留字段或包含未知/非法类型字段时抛出。
    """
    text = (raw_json or "").strip()
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"高级 JSON 覆盖格式无效: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            '高级 JSON 覆盖必须为 JSON 对象（例如 {"EnablePublicSharing": false}）'
        )

    for key in parsed:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("高级 JSON 覆盖的字段名必须为非空字符串")
        reserved_reason = RESERVED_USER_POLICY_FIELDS.get(key.strip().lower())
        if reserved_reason is not None:
            raise ValueError(reserved_reason)

    normalized_type = (server_type or "").strip().lower()
    if normalized_type == "emby":
        return _validate_policy_model(parsed, EmbyUserPolicy, "Emby")
    if normalized_type == "jellyfin":
        return _validate_policy_model(
            parsed,
            JellyfinUserPolicy,
            "Jellyfin",
            required_defaults=_JELLYFIN_REQUIRED_VALIDATION_DEFAULTS,
        )

    emby_dumped = _validate_policy_model(parsed, EmbyUserPolicy, "Emby")
    _validate_policy_model(
        parsed,
        JellyfinUserPolicy,
        "Jellyfin",
        required_defaults=_JELLYFIN_REQUIRED_VALIDATION_DEFAULTS,
    )
    return emby_dumped


def parse_server_default_user_policy(raw: str | None) -> DefaultUserPolicySpec:
    """从 ``ServerInstance.default_user_policy`` 数据库列安全解析配置模板

    Args:
        raw: 数据库存储的 JSON 字符串（可为 ``None``）。

    Returns:
        解析后的 ``DefaultUserPolicySpec``；缺失或损坏时回退至未启用默认实例。
    """
    if not raw or not str(raw).strip():
        return DefaultUserPolicySpec()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return DefaultUserPolicySpec()
        return DefaultUserPolicySpec.model_validate(data)
    except json.JSONDecodeError, ValidationError, ValueError:
        return DefaultUserPolicySpec()


def serialize_server_default_user_policy(
    spec: DefaultUserPolicySpec,
    server_type: str | None,
) -> str:
    """校验默认 UserPolicy 配置并序列化为可落库的规范 JSON 字符串

    Args:
        spec: 待保存的 ``DefaultUserPolicySpec`` 实例。
        server_type: 目标媒体服务器类型（``emby`` / ``jellyfin``）。

    Returns:
        规范化后的 JSON 字符串。

    Raises:
        ValueError: 当 ``extra_json`` 非法、包含保留字段或字段不兼容目标媒体服务器时抛出。
    """
    if spec.extra_json:
        parse_extra_policy_json(spec.extra_json, server_type)
    return spec.model_dump_json()


def build_default_user_policy_patch(
    spec: DefaultUserPolicySpec,
    server_type: str | None,
) -> dict[str, Any]:
    """根据已启用的默认 UserPolicy 配置构造待合并下发的策略补丁字典

    自动处理 Emby（``SimultaneousStreamLimit``）与 Jellyfin（``MaxActiveSessions``）
    并发会话字段差异，并在末尾叠加经过安全校验的高级 JSON 覆盖项。

    Args:
        spec: 已解析的服务器默认策略配置。
        server_type: 目标媒体服务器类型（``emby`` / ``jellyfin``）。

    Returns:
        未启用时返回空字典；启用时返回可直接合并至 ``UserPolicy`` 的策略字典。
    """
    if not spec.enabled:
        return {}

    bitrate_bps = (
        round(spec.max_bitrate_mbps * 1_000_000) if spec.max_bitrate_mbps > 0 else 0
    )
    concurrent_limit = max(0, int(spec.max_concurrent_streams))

    patch: dict[str, Any] = {
        "IsHidden": bool(spec.is_hidden),
        "EnableMediaPlayback": bool(spec.enable_media_playback),
        "EnableVideoPlaybackTranscoding": bool(spec.enable_video_transcoding),
        "EnableAudioPlaybackTranscoding": bool(spec.enable_audio_transcoding),
        "EnablePlaybackRemuxing": bool(spec.enable_playback_remuxing),
        "EnableContentDownloading": bool(spec.enable_content_downloading),
        "EnableRemoteAccess": bool(spec.enable_remote_access),
        "EnableRemoteControlOfOtherUsers": bool(spec.enable_remote_control),
        "RemoteClientBitrateLimit": bitrate_bps,
    }
    if (server_type or "").strip().lower() == "jellyfin":
        patch["MaxActiveSessions"] = concurrent_limit
    else:
        patch["SimultaneousStreamLimit"] = concurrent_limit

    extra_overrides = parse_extra_policy_json(spec.extra_json, server_type)
    for key, value in extra_overrides.items():
        if key.strip().lower() not in RESERVED_USER_POLICY_FIELDS:
            patch[key] = value
    return patch


def merge_user_policy(
    base_policy: Any | None,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """以账号现有外部策略为基底安全合并默认 Policy 补丁，绝不覆盖受保护字段

    Args:
        base_policy: 外部媒体服务器返回的当前 ``UserPolicy`` 模型或字典（可为 ``None``）。
        patch: 由 ``build_default_user_policy_patch`` 生成的策略补丁字典。

    Returns:
        保留既有 ``IsDisabled``、``IsAdministrator``、文件夹权限与认证器字段后的合并字典。
    """
    if base_policy is None:
        merged: dict[str, Any] = {}
    elif isinstance(base_policy, dict):
        merged = dict(base_policy)
    else:
        merged = base_policy.model_dump()

    for key, value in patch.items():
        if key.strip().lower() in RESERVED_USER_POLICY_FIELDS:
            continue
        merged[key] = value
    return merged
