import contextlib
import json
from datetime import datetime, timedelta

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
)
from sqlalchemy.ext.asyncio import AsyncSession

from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.config import get_settings
from core.database import get_db
from core.dependencies import (
    get_notification_service,
    get_radarr_clients,
    get_sonarr_clients,
    get_tmdb_client,
    get_tvdb_client,
)
from core.user_policy import (
    parse_server_default_user_policy,
    serialize_server_default_user_policy,
)
from core.webapp_auth import validate_admin_access
from models.events import (
    ADMIN_DM_ALLOWED_CATEGORIES,
    DEFAULT_GLOBAL_ROUTE_TARGETS,
    OPTIONAL_EVENT_LABELS,
    ROUTE_CATEGORY_LABELS,
    NotificationRouteCategory,
    NotificationRouteScope,
    NotificationTargetType,
    get_event_spec,
    get_optional_events_for_category,
)
from models.orm import NotificationRoute, ServerInstance, ServerType
from models.schemas import (
    AdminDto,
    ArrServerDto,
    BindingUpdate,
    DefaultUserPolicyDto,
    DefaultUserPolicySyncResponse,
    LibraryDto,
    LlmConfigUpdateDto,
    MetadataConfigUpdateDto,
    MkvConfigUpdateDto,
    NotificationRouteDeleteResponse,
    NotificationRouteDto,
    NotificationRouteMutationResponse,
    NotificationRoutesResponse,
    NotificationRouteTestRequest,
    NotificationRouteTestResponse,
    NotificationRouteUpsertDto,
    NsfwLibraryDto,
    OpsAlertLogDto,
    OpsAlertLogsPageResponse,
    OpsConfigUpdateDto,
    OptionalEventOptionDto,
    PointsConfigUpdateDto,
    PolicyConfigUpdateDto,
    RouteCategoryMetaDto,
    ServerCategoryRouteStateDto,
    ServerCreate,
    ServerDto,
    ServerRouteOverviewDto,
    ServerUpdate,
    SystemConfigResponse,
    ToggleResponse,
    TopicsResponse,
    TrafficConfigUpdateDto,
    WatchTimeConfigUpdateDto,
)
from repositories.config_repo import ConfigRepository
from repositories.notification_repo import (
    NotificationRouteRepository,
    OpsAlertRepository,
)
from repositories.server_repo import ServerRepository
from services.account_service import AccountService, format_traffic_release_summary
from services.notification_service import (
    NotificationService,
    get_or_create_notification_runtime,
    resolve_server_route_precedence,
)
from services.settings_service import SettingsServices
from services.traffic_service import invalidate_traffic_server_cache
from services.user_service import UserService
from workers.nfo_worker import (
    rebuild_radarr_metadata_task,
    rebuild_sonarr_metadata_task,
)
from workers.translator_worker import remove_server_translate_jobs

settings = get_settings()
router = APIRouter(prefix="/api/settings", tags=["settings"])


def mask_server_data(server: ServerInstance) -> dict:
    """脱敏并转换 ServerInstance"""
    data = {c.name: getattr(server, c.name) for c in server.__table__.columns}
    data["api_key"] = "******"

    # 路径映射 JSON -> List
    pm = data.get("path_mappings")
    mappings_list = []
    if isinstance(pm, str) and pm.strip():
        with contextlib.suppress(json.JSONDecodeError):
            pm_dict = json.loads(pm)
            for remote, local in pm_dict.items():
                mappings_list.append({"remote": remote, "local": local})
    data["path_mappings"] = mappings_list
    data["default_user_policy"] = parse_server_default_user_policy(
        data.get("default_user_policy")
    ).model_dump()
    return data


# --- System & Admin APIs ---

SYSTEM_KEY_MAP = {
    "enable_points": ConfigRepository.KEY_ENABLE_POINTS,
    "enable_verification": ConfigRepository.KEY_ENABLE_VERIFICATION,
    "enable_cleanup_inactive_users": ConfigRepository.KEY_ENABLE_CLEANUP_INACTIVE_USERS,
    "enable_mkv_process": ConfigRepository.KEY_ENABLE_MKV_PROCESS,
    "enable_llm_translate": ConfigRepository.KEY_ENABLE_LLM_TRANSLATE,
    "enable_watch_time": ConfigRepository.KEY_ENABLE_WATCH_TIME,
    "enable_traffic": ConfigRepository.KEY_ENABLE_TRAFFIC,
    **{
        field: db_key
        for field, (db_key, _) in ConfigRepository.POINTS_SUB_SWITCHES.items()
    },
}


def _is_mkvtoolnix_available(app) -> bool:
    """判断当前运行环境是否已检测到 mkvtoolnix（优先读取启动时缓存的状态）"""
    state = getattr(app, "state", None)
    if state is None:
        return False
    if hasattr(state, "mkvtoolnix_available"):
        return bool(state.mkvtoolnix_available)
    return getattr(state, "task_queue", None) is not None


@router.get(
    "/system",
    dependencies=[Depends(validate_admin_access)],
    response_model=SystemConfigResponse,
)
async def get_system_config(request: Request, session: AsyncSession = Depends(get_db)):
    """获取管理后台系统配置快照（包含总开关、6 项积分细粒度子开关、生效态及策略参数）。

    Args:
        request: FastAPI 请求对象，用于读取运行时应用状态（如 mkvtoolnix 可用性）。
        session: 异步数据库会话。

    Returns:
        匹配 ``SystemConfigResponse`` 契约的系统配置字典。
    """
    repo = ConfigRepository(session)
    llm_cfg = await repo.get_llm_config()
    meta_cfg = await repo.get_metadata_config()
    points_cfg = await repo.get_points_config()
    policy_cfg = await repo.get_policy_config()
    ops_cfg = await repo.get_ops_config()
    watch_time_cfg = await repo.get_watch_time_config()
    traffic_cfg = await repo.get_traffic_config()
    return {
        "enable_points": await repo.get_settings(
            ConfigRepository.KEY_ENABLE_POINTS, "true"
        )
        == "true",
        "enable_verification": await repo.get_settings(
            ConfigRepository.KEY_ENABLE_VERIFICATION, "true"
        )
        == "true",
        "enable_cleanup_inactive_users": await repo.get_settings(
            ConfigRepository.KEY_ENABLE_CLEANUP_INACTIVE_USERS, "false"
        )
        == "true",
        "enable_mkv_process": await repo.get_settings(
            ConfigRepository.KEY_ENABLE_MKV_PROCESS, "true"
        )
        == "true",
        "enable_watch_time": await repo.is_watch_time_enabled(),
        "enable_traffic": await repo.is_traffic_enabled(),
        "mkvtoolnix_available": _is_mkvtoolnix_available(request.app),
        "mkv_exclude_keywords": await repo.get_settings(
            ConfigRepository.KEY_MKV_EXCLUDE_KEYWORDS, ""
        )
        or "",
        "enable_llm_translate": llm_cfg["enable_llm_translate"],
        "ai_api_key_masked": "******" if llm_cfg["ai_api_key"] else "",
        "ai_base_url": llm_cfg["ai_base_url"],
        "ai_model": llm_cfg["ai_model"],
        "ai_temperature": llm_cfg["ai_temperature"],
        "ai_rpm": llm_cfg["ai_rpm"],
        "ai_rpd": llm_cfg["ai_rpd"],
        "ai_tpm": llm_cfg["ai_tpm"],
        "ai_concurrency": llm_cfg["ai_concurrency"],
        "tmdb_api_key_masked": "******" if meta_cfg["tmdb_api_key"] else "",
        "tvdb_api_key_masked": "******" if meta_cfg["tvdb_api_key"] else "",
        "effective_points_switches": await repo.get_effective_points_switches(),
        **points_cfg,
        **policy_cfg,
        **ops_cfg,
        **watch_time_cfg,
        **traffic_cfg,
    }


@router.post(
    "/system/ops",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_ops_config_endpoint(
    request: Request,
    payload: OpsConfigUpdateDto,
    session: AsyncSession = Depends(get_db),
):
    """更新定时任务调度与运维策略配置"""
    service = SettingsServices(request.app, session)
    result = await service.update_ops_config(payload)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {
        "success": True,
        "key": "ops_config",
        "message": result.message,
    }


@router.post(
    "/system/policy",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_policy_config_endpoint(
    request: Request,
    payload: PolicyConfigUpdateDto,
    session: AsyncSession = Depends(get_db),
):
    """更新入群验证超时与账号/求片宽限期配置"""
    service = SettingsServices(request.app, session)
    result = await service.update_policy_config(payload)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {
        "success": True,
        "key": "policy_config",
        "message": result.message,
    }


@router.post(
    "/system/watch-time",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_watch_time_config_endpoint(
    request: Request,
    payload: WatchTimeConfigUpdateDto,
    session: AsyncSession = Depends(get_db),
):
    """更新播放时长日上限、Emby 心跳间隔与断线宽限配置

    副作用：持久化配置并刷新缓存，同时热更新运行中 Emby 心跳任务触发周期。

    Args:
        request: FastAPI 请求对象，用于访问运行期应用状态与调度器。
        payload: 播放时长策略配置更新载荷。
        session: 异步数据库会话。

    Returns:
        包含成功状态、配置键 ``watch_time_config`` 与提示文案的字典。
    """
    service = SettingsServices(request.app, session)
    result = await service.update_watch_time_config(payload)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {
        "success": True,
        "key": "watch_time_config",
        "message": result.message,
    }


@router.post(
    "/system/traffic",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_traffic_config_endpoint(
    request: Request,
    payload: TrafficConfigUpdateDto,
    session: AsyncSession = Depends(get_db),
):
    """更新 Nginx 流量 UDP 监听地址、端口与内存聚合刷盘间隔配置

    副作用：保存配置后按新参数热重载或绑定 ``TrafficService`` UDP 监听套接字。

    Args:
        request: FastAPI 请求对象，用于访问应用状态中的流量采集服务。
        payload: 流量监听及流量包单价配置更新载荷。
        session: 异步数据库会话。

    Returns:
        包含成功状态、配置键 ``traffic_config`` 与提示文案的字典。
    """
    service = SettingsServices(request.app, session)
    result = await service.update_traffic_config(payload)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {
        "success": True,
        "key": "traffic_config",
        "message": result.message,
    }


@router.post(
    "/system/points",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_points_config_endpoint(
    request: Request,
    payload: PointsConfigUpdateDto,
    session: AsyncSession = Depends(get_db),
):
    """更新积分、签到与定价规则配置"""
    service = SettingsServices(request.app, session)
    result = await service.update_points_config(payload)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {
        "success": True,
        "key": "points_config",
        "message": result.message,
    }


@router.post(
    "/system/metadata",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_metadata_config_endpoint(
    request: Request,
    payload: MetadataConfigUpdateDto,
    session: AsyncSession = Depends(get_db),
):
    """更新 TMDB/TVDB 元数据 API Key 并热重载客户端"""
    service = SettingsServices(request.app, session)
    result = await service.update_metadata_config(payload)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {
        "success": True,
        "key": "metadata_config",
        "message": result.message,
    }


@router.post(
    "/system/llm",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_llm_config_endpoint(
    request: Request,
    payload: LlmConfigUpdateDto,
    session: AsyncSession = Depends(get_db),
):
    """更新 LLM 翻译配置并热重载全局 AI 客户端"""
    service = SettingsServices(request.app, session)
    result = await service.update_llm_config(payload)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {
        "success": True,
        "key": "llm_config",
        "message": result.message,
    }


@router.post(
    "/system/mkv_exclude_keywords",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_mkv_exclude_keywords(
    payload: MkvConfigUpdateDto, session: AsyncSession = Depends(get_db)
):
    """更新 MKV 自动处理排除关键词配置"""
    repo = ConfigRepository(session)
    cleaned = "\n".join(
        ConfigRepository.parse_exclude_keywords(payload.mkv_exclude_keywords)
    )
    await repo.set_settings(ConfigRepository.KEY_MKV_EXCLUDE_KEYWORDS, cleaned)
    return {
        "success": True,
        "key": "mkv_exclude_keywords",
        "db_key": ConfigRepository.KEY_MKV_EXCLUDE_KEYWORDS,
        "message": "MKV 排除关键词已保存",
    }


@router.post(
    "/system/{key}/toggle",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def toggle_system_setting_endpoint(
    request: Request, key: str, session: AsyncSession = Depends(get_db)
):
    """切换系统配置"""
    db_key = SYSTEM_KEY_MAP.get(key)
    if not db_key:
        raise HTTPException(status_code=400, detail=f"Invalid key: {key}")
    # 未检测到 mkvtoolnix 时禁止切换 MKV 自动处理开关
    if key == "enable_mkv_process" and not _is_mkvtoolnix_available(request.app):
        raise HTTPException(
            status_code=400,
            detail="未检测到 mkvtoolnix (mkvmerge)，无法操作 MKV 自动处理开关",
        )
    service = SettingsServices(request.app, session)
    result = await service.toggle_system_setting(db_key)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    repo = ConfigRepository(session)
    new_value = await repo.get_settings(db_key)
    return {
        "success": True,
        "key": key,
        "db_key": db_key,
        "new_state": new_value == "true",
        "message": result.message,
    }


@router.get(
    "/admins",
    dependencies=[Depends(validate_admin_access)],
    response_model=list[AdminDto],
)
async def get_admins(request: Request, session: AsyncSession = Depends(get_db)):
    """获取管理员列表"""
    service = SettingsServices(request.app, session)
    bot_admin_ids = await service.telegram_repo.get_admins()
    group_admins = await service.client.get_chat_admin_ids()
    result = []
    for user in group_admins:
        result.append(
            {
                "id": user.id,
                "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
                "username": user.username,
                "is_bot_admin": user.id in bot_admin_ids,
            }
        )
    return result


@router.post(
    "/admins/{user_id}/toggle",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def toggle_admin(
    request: Request, user_id: int, session: AsyncSession = Depends(get_db)
):
    """切换管理员状态"""
    service = SettingsServices(request.app, session)
    result = await service.toggle_admin(user_id)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {"success": True, "message": result.message}


@router.get(
    "/topics",
    dependencies=[Depends(validate_admin_access)],
    response_model=TopicsResponse,
)
async def get_topics(request: Request, session: AsyncSession = Depends(get_db)):
    """获取主题列表"""
    service = SettingsServices(request.app, session)
    topic_map = await service.client.get_group_topics()
    return {
        "is_forum": topic_map["is_forum"],
        "topics": [{"id": k, "name": v} for k, v in topic_map["topics"].items()],
    }


# --- Notification Routing, Test & Ops Alert APIs ---

_GLOBAL_ROUTE_CATEGORY_ORDER: tuple[NotificationRouteCategory, ...] = (
    NotificationRouteCategory.MEDIA_LIBRARY,
    NotificationRouteCategory.MEDIA_REQUEST,
    NotificationRouteCategory.ACCOUNT_LIFECYCLE,
    NotificationRouteCategory.OPS_ALERT,
    NotificationRouteCategory.COMMUNITY,
)
_SERVER_ROUTE_CATEGORY_ORDER: tuple[NotificationRouteCategory, ...] = (
    NotificationRouteCategory.MEDIA_LIBRARY,
    NotificationRouteCategory.MEDIA_REQUEST,
    NotificationRouteCategory.ACCOUNT_LIFECYCLE,
    NotificationRouteCategory.OPS_ALERT,
)


async def _sync_changed_legacy_topic_route(
    route_repo: NotificationRouteRepository,
    *,
    server_id: int,
    category: NotificationRouteCategory,
    topic_val: int,
) -> None:
    """在服务器更新接口显式修改遗留话题 ID 时，同步刷新对应单服覆盖路由并保留可选事件。"""
    existing = await route_repo.get_route(
        scope_type=NotificationRouteScope.SERVER,
        server_id=server_id,
        category=category,
    )
    if topic_val == 0:
        if existing is not None:
            await route_repo.delete_server_route(server_id=server_id, category=category)
        return

    preserved_events = (
        sorted(
            NotificationRouteRepository.parse_enabled_events(
                existing.enabled_events_json
            )
        )
        if existing is not None
        else []
    )
    preserved_escalate = (
        bool(existing.escalate_admin_dm) if existing is not None else False
    )
    if topic_val > 0:
        await route_repo.upsert_route(
            scope_type=NotificationRouteScope.SERVER,
            server_id=server_id,
            category=category,
            target_type=NotificationTargetType.FORUM_TOPIC,
            target_chat_id=existing.target_chat_id if existing is not None else None,
            target_topic_id=int(topic_val),
            escalate_admin_dm=preserved_escalate,
            enabled_events=preserved_events,
        )
    else:
        await route_repo.upsert_route(
            scope_type=NotificationRouteScope.SERVER,
            server_id=server_id,
            category=category,
            target_type=NotificationTargetType.CHAT_OR_CHANNEL,
            target_chat_id=int(topic_val),
            target_topic_id=None,
            escalate_admin_dm=preserved_escalate,
            enabled_events=preserved_events,
        )


def _route_orm_to_dto(route: NotificationRoute) -> NotificationRouteDto:
    """将数据库 ``NotificationRoute`` 记录转换为前端可消费的 ``NotificationRouteDto``。"""
    scope: str = (
        "server" if route.scope_type == NotificationRouteScope.SERVER else "global"
    )
    return NotificationRouteDto(
        id=route.id,
        scope_type="server" if scope == "server" else "global",
        server_id=route.server_id,
        category=str(route.category),
        target_type=str(route.target_type),
        target_chat_id=route.target_chat_id,
        target_topic_id=route.target_topic_id,
        escalate_admin_dm=bool(route.escalate_admin_dm),
        enabled_events=sorted(
            NotificationRouteRepository.parse_enabled_events(route.enabled_events_json)
        ),
        updated_at=route.updated_at,
    )


def _default_global_route_dto(
    category: NotificationRouteCategory,
) -> NotificationRouteDto:
    """当数据库尚未持久化指定全局类别路由时，生成内置默认路由视图。"""
    return NotificationRouteDto(
        id=None,
        scope_type="global",
        server_id=None,
        category=category.value,
        target_type=DEFAULT_GLOBAL_ROUTE_TARGETS[category].value,
        target_chat_id=None,
        target_topic_id=None,
        escalate_admin_dm=False,
        enabled_events=[],
        updated_at=None,
    )


def _legacy_topic_to_route_dto(
    *,
    server_id: int,
    category: NotificationRouteCategory,
    legacy_topic_id: int,
) -> NotificationRouteDto:
    """将 ``ServerInstance`` 遗留话题字段合成为只读展示路由 DTO，与运行时回退语义一致。"""
    if legacy_topic_id > 0:
        return NotificationRouteDto(
            scope_type="server",
            server_id=server_id,
            category=category.value,
            target_type=NotificationTargetType.FORUM_TOPIC.value,
            target_chat_id=None,
            target_topic_id=int(legacy_topic_id),
            escalate_admin_dm=False,
            enabled_events=[],
        )
    return NotificationRouteDto(
        scope_type="server",
        server_id=server_id,
        category=category.value,
        target_type=NotificationTargetType.CHAT_OR_CHANNEL.value,
        target_chat_id=int(legacy_topic_id),
        target_topic_id=None,
        escalate_admin_dm=False,
        enabled_events=[],
    )


def _build_optional_events_catalog() -> dict[str, list[OptionalEventOptionDto]]:
    """构建按路由类别分组的默认关闭可选事件元数据字典。"""
    catalog: dict[str, list[OptionalEventOptionDto]] = {}
    for category in _GLOBAL_ROUTE_CATEGORY_ORDER:
        options: list[OptionalEventOptionDto] = []
        for event in get_optional_events_for_category(category):
            spec = get_event_spec(event)
            options.append(
                OptionalEventOptionDto(
                    event=event.value,
                    label=OPTIONAL_EVENT_LABELS.get(event, event.value),
                    severity=spec.severity.value,
                )
            )
        catalog[category.value] = options
    return catalog


def _build_category_metas(
    optional_events: dict[str, list[OptionalEventOptionDto]],
) -> list[RouteCategoryMetaDto]:
    """生成 5 大路由类别的展示标题、说明与允许目标列表。"""
    base_targets = [
        NotificationTargetType.DISABLED.value,
        NotificationTargetType.MAIN_CHAT.value,
        NotificationTargetType.FORUM_TOPIC.value,
        NotificationTargetType.CHAT_OR_CHANNEL.value,
    ]
    metas: list[RouteCategoryMetaDto] = []
    for category in _GLOBAL_ROUTE_CATEGORY_ORDER:
        label, desc = ROUTE_CATEGORY_LABELS[category]
        allowed_targets = list(base_targets)
        if category in ADMIN_DM_ALLOWED_CATEGORIES:
            allowed_targets.append(NotificationTargetType.ADMIN_DM_GROUP.value)
        metas.append(
            RouteCategoryMetaDto(
                category=category.value,
                label=label,
                description=desc,
                allowed_target_types=allowed_targets,
                supports_escalate_admin_dm=(
                    category == NotificationRouteCategory.OPS_ALERT
                ),
                optional_events=optional_events.get(category.value, []),
            )
        )
    return metas


@router.get(
    "/notification_routes",
    dependencies=[Depends(validate_admin_access)],
    response_model=NotificationRoutesResponse,
)
async def get_notification_routes(
    session: AsyncSession = Depends(get_db),
) -> NotificationRoutesResponse:
    """获取全局通知路由矩阵、单服覆盖及遗留话题生效解析视图。"""
    route_repo = NotificationRouteRepository(session)
    server_repo = ServerRepository(session)

    all_routes = await route_repo.get_all_routes()
    servers = await server_repo.get_all()

    global_by_cat: dict[str, NotificationRouteDto] = {}
    server_route_dtos: list[NotificationRouteDto] = []
    server_overrides: dict[tuple[int, str], NotificationRouteDto] = {}

    for route in all_routes:
        dto = _route_orm_to_dto(route)
        if route.scope_type == NotificationRouteScope.GLOBAL:
            global_by_cat[dto.category] = dto
        elif route.scope_type == NotificationRouteScope.SERVER and route.server_id:
            server_route_dtos.append(dto)
            server_overrides[(int(route.server_id), dto.category)] = dto

    global_routes = [
        global_by_cat.get(cat.value) or _default_global_route_dto(cat)
        for cat in _GLOBAL_ROUTE_CATEGORY_ORDER
    ]
    global_lookup = {dto.category: dto for dto in global_routes}

    server_overviews: list[ServerRouteOverviewDto] = []
    for srv in servers:
        categories_state: dict[str, ServerCategoryRouteStateDto] = {}
        for cat in _SERVER_ROUTE_CATEGORY_ORDER:
            legacy_topic: int | None = None
            if cat == NotificationRouteCategory.MEDIA_LIBRARY and srv.notify_topic_id:
                legacy_topic = int(srv.notify_topic_id)
            elif (
                cat == NotificationRouteCategory.MEDIA_REQUEST
                and srv.request_notify_topic_id
            ):
                legacy_topic = int(srv.request_notify_topic_id)

            override = server_overrides.get((srv.id, cat.value))
            global_dto = global_lookup[cat.value]
            source, chosen = resolve_server_route_precedence(
                server_override=override,
                global_route=global_dto,
                legacy_topic_id=legacy_topic,
                category=cat,
            )
            if source == "server_override" and chosen is not None:
                categories_state[cat.value] = ServerCategoryRouteStateDto(
                    category=cat.value,
                    source="server_override",
                    is_override=True,
                    effective=chosen,
                    override=chosen,
                    legacy_topic_id=legacy_topic,
                )
            elif source == "legacy_topic" and legacy_topic:
                categories_state[cat.value] = ServerCategoryRouteStateDto(
                    category=cat.value,
                    source="legacy_topic",
                    is_override=False,
                    effective=_legacy_topic_to_route_dto(
                        server_id=srv.id,
                        category=cat,
                        legacy_topic_id=legacy_topic,
                    ),
                    override=None,
                    legacy_topic_id=legacy_topic,
                )
            else:
                categories_state[cat.value] = ServerCategoryRouteStateDto(
                    category=cat.value,
                    source="global",
                    is_override=False,
                    effective=chosen or global_dto,
                    override=None,
                    legacy_topic_id=legacy_topic,
                )

        server_overviews.append(
            ServerRouteOverviewDto(
                server_id=srv.id,
                server_name=srv.name,
                server_type=str(srv.server_type),
                notify_topic_id=srv.notify_topic_id,
                request_notify_topic_id=srv.request_notify_topic_id,
                categories=categories_state,
            )
        )

    optional_events = _build_optional_events_catalog()
    configured_chat_id = int(getattr(settings, "telegram_chat_id", 0) or 0)
    return NotificationRoutesResponse(
        global_routes=global_routes,
        server_routes=server_route_dtos,
        servers=server_overviews,
        categories=_build_category_metas(optional_events),
        optional_events=optional_events,
        telegram_chat_id_configured=bool(configured_chat_id),
        telegram_chat_id=configured_chat_id if configured_chat_id else None,
    )


@router.put(
    "/notification_routes",
    dependencies=[Depends(validate_admin_access)],
    response_model=NotificationRouteMutationResponse,
)
async def upsert_notification_route(
    data: NotificationRouteUpsertDto,
    session: AsyncSession = Depends(get_db),
) -> NotificationRouteMutationResponse:
    """新增或更新全局默认通知路由或单服覆盖通知路由。"""
    if data.scope_type == NotificationRouteScope.SERVER:
        assert data.server_id is not None
        server = await ServerRepository(session).get_by_id(data.server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="目标服务器不存在")

    route = await NotificationRouteRepository(session).upsert_route(
        scope_type=data.scope_type,
        server_id=data.server_id,
        category=data.category,
        target_type=data.target_type,
        target_chat_id=data.target_chat_id,
        target_topic_id=data.target_topic_id,
        escalate_admin_dm=data.escalate_admin_dm,
        enabled_events=data.enabled_events,
    )
    return NotificationRouteMutationResponse(
        success=True,
        message="通知路由配置已保存",
        route=_route_orm_to_dto(route),
    )


@router.delete(
    "/notification_routes/server/{server_id}/{category}",
    dependencies=[Depends(validate_admin_access)],
    response_model=NotificationRouteDeleteResponse,
)
async def delete_server_notification_route(
    request: Request,
    server_id: int,
    category: NotificationRouteCategory,
    session: AsyncSession = Depends(get_db),
) -> NotificationRouteDeleteResponse:
    """删除指定服务器的通知覆盖路由，并清理对应遗留话题字段使其真正恢复继承全局默认规则。"""
    server_repo = ServerRepository(session)
    server = await server_repo.get_by_id(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="目标服务器不存在")

    deleted = await NotificationRouteRepository(session).delete_server_route(
        server_id, category
    )
    # 同步清理迁移后仍保留的遗留话题列，避免全局路由为 disabled 时错误回退至旧话题
    legacy_cleared = False
    if category == NotificationRouteCategory.MEDIA_LIBRARY and (
        server.notify_topic_id is not None
    ):
        await server_repo.update_notify_config(server_id, notify_topic_id=None)
        legacy_cleared = True
        for attr in ("media_clients", "sonarr_clients", "radarr_clients"):
            client_map = getattr(getattr(request.app, "state", None), attr, None)
            if isinstance(client_map, dict) and server_id in client_map:
                client_map[server_id].notify_topic_id = None
    elif category == NotificationRouteCategory.MEDIA_REQUEST and (
        server.request_notify_topic_id is not None
    ):
        await server_repo.update_notify_config(server_id, request_notify_topic_id=None)
        legacy_cleared = True
        for attr in ("sonarr_clients", "radarr_clients"):
            client_map = getattr(getattr(request.app, "state", None), attr, None)
            if isinstance(client_map, dict) and server_id in client_map:
                client_map[server_id].request_notify_topic_id = None

    return NotificationRouteDeleteResponse(
        success=True,
        deleted=deleted,
        message="已清除单服覆盖，恢复继承全局默认路由"
        if (deleted or legacy_cleared)
        else "该服务器当前未配置此类别的独立覆盖路由",
    )


def _parse_ops_targets(raw_json: str | None) -> list[str]:
    """安全解析运维告警日志中序列化的目标列表 JSON。"""
    if not raw_json:
        return []
    with contextlib.suppress(TypeError, ValueError):
        parsed = json.loads(raw_json)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    return []


async def query_recent_ops_logs_page(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 20,
    offset: int = 0,
    event_type: str | None = None,
    severity: str | None = None,
    server_id: int | None = None,
) -> OpsAlertLogsPageResponse:
    """统一查询近 30 天内分页运维告警日志并关联服务器名称（供后台与 Mini App 管理员共用）。"""
    since = now - timedelta(days=30)
    ops_repo = OpsAlertRepository(session)
    items = await ops_repo.list_logs(
        limit=limit,
        offset=offset,
        event_type=event_type,
        severity=severity,
        server_id=server_id,
        since=since,
    )
    total = await ops_repo.count_logs(
        event_type=event_type,
        severity=severity,
        server_id=server_id,
        since=since,
    )
    servers = await ServerRepository(session).get_all()
    server_names = {srv.id: srv.name for srv in servers}

    dtos = [
        OpsAlertLogDto(
            id=entry.id,
            source_key=entry.source_key,
            server_id=entry.server_id,
            server_name=(
                server_names.get(entry.server_id)
                if entry.server_id is not None
                else None
            ),
            event_type=str(entry.event_type),
            severity=str(entry.severity),
            title=entry.title,
            body_text=entry.body_text,
            delivered_targets=_parse_ops_targets(entry.delivered_targets_json),
            delivery_status=entry.delivery_status,
            error_message=entry.error_message,
            created_at=entry.created_at,
        )
        for entry in items
    ]
    return OpsAlertLogsPageResponse(
        items=dtos,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/notifications/test",
    dependencies=[Depends(validate_admin_access)],
    response_model=NotificationRouteTestResponse,
)
async def test_notification_route(
    request: Request,
    data: NotificationRouteTestRequest,
    session: AsyncSession = Depends(get_db),
    admin_id: int = Depends(validate_admin_access),
    notification_service: NotificationService = Depends(get_notification_service),
) -> NotificationRouteTestResponse:
    """发送 ``SYSTEM_TEST`` 实测通知，验证已保存路由或候选目标参数是否通畅。"""
    server_name = "系统全局"
    if data.server_id is not None:
        server = await ServerRepository(session).get_by_id(data.server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="目标服务器不存在")
        server_name = server.name

    operator_name = await UserService(request.app, session).resolve_user_display_name(
        admin_id if isinstance(admin_id, int) else None,
        fallback="管理员",
    )
    target_desc = (
        data.target_type.value if data.target_type is not None else "当前生效路由"
    )
    sent = await notification_service.send_test_to_route(
        category=data.category,
        server_id=data.server_id,
        server_name=server_name,
        target_type=data.target_type,
        target_chat_id=data.target_chat_id,
        target_topic_id=data.target_topic_id,
        escalate_admin_dm=data.escalate_admin_dm,
        operator_name=operator_name,
        target_description=target_desc,
        message=data.message or "这是一条系统通知通路测试消息。",
        session=session,
    )

    report = getattr(notification_service, "last_test_report", None)
    if report is not None:
        delivered = list(report.delivered_targets)
        err_msg = "; ".join(report.errors) if report.errors else None
        if delivered and not err_msg:
            return NotificationRouteTestResponse(
                success=True,
                message=f"测试消息已成功发送至 {len(delivered)} 个目标",
                delivered_targets=delivered,
                error=None,
            )
        if delivered and err_msg:
            return NotificationRouteTestResponse(
                success=True,
                message=f"测试消息已部分送达（{len(delivered)} 个目标成功），部分失败",
                delivered_targets=delivered,
                error=err_msg,
            )
        final_error = err_msg or "测试消息未送达任何目标"
        return NotificationRouteTestResponse(
            success=False,
            message=final_error,
            delivered_targets=[],
            error=final_error,
        )

    if sent is not None:
        return NotificationRouteTestResponse(
            success=True,
            message="测试消息发送成功",
            delivered_targets=[target_desc],
            error=None,
        )
    return NotificationRouteTestResponse(
        success=False,
        message="测试消息发送失败或目标不可用",
        delivered_targets=[],
        error="测试消息发送失败或目标不可用",
    )


@router.get(
    "/notifications/ops_logs",
    dependencies=[Depends(validate_admin_access)],
    response_model=OpsAlertLogsPageResponse,
)
async def get_settings_ops_logs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    server_id: int | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
) -> OpsAlertLogsPageResponse:
    """分页查询近 30 天系统运维告警记录，支持事件类型、级别与服务器筛选。"""
    now = get_or_create_notification_runtime(request.app).clock()
    return await query_recent_ops_logs_page(
        session,
        now=now,
        limit=limit,
        offset=offset,
        event_type=event_type,
        severity=severity,
        server_id=server_id,
    )


# --- Server Management APIs ---


@router.get(
    "/servers",
    dependencies=[Depends(validate_admin_access)],
    response_model=list[ServerDto],
)
async def get_servers(session: AsyncSession = Depends(get_db)):
    """获取服务器列表"""
    repo = ServerRepository(session)
    servers = await repo.get_all()
    return [mask_server_data(s) for s in servers]


@router.get(
    "/servers/{server_id}",
    dependencies=[Depends(validate_admin_access)],
    response_model=ServerDto,
)
async def get_server_detail(server_id: int, session: AsyncSession = Depends(get_db)):
    """获取服务器详情"""
    repo = ServerRepository(session)
    server = await repo.get_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return mask_server_data(server)


@router.post(
    "/servers",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def create_server(
    request: Request, data: ServerCreate, session: AsyncSession = Depends(get_db)
):
    """创建服务器实例并持久化有效期、播放时长保活与流量配额初始配置

    副作用：写入新服务器记录、初始化对应媒体/下载器客户端并刷新流量路由缓存。

    Args:
        request: FastAPI 请求对象。
        data: 创建服务器请求载荷。
        session: 异步数据库会话。

    Returns:
        包含成功状态与提示文案的字典。
    """
    service = SettingsServices(request.app, session)
    serialized_default_policy: str | None = None
    if data.default_user_policy is not None:
        if data.server_type not in (ServerType.EMBY, ServerType.JELLYFIN):
            if data.default_user_policy.enabled or data.default_user_policy.extra_json:
                raise HTTPException(
                    status_code=400,
                    detail="仅 Emby/Jellyfin 媒体服务器支持配置默认 UserPolicy",
                )
        else:
            try:
                serialized_default_policy = serialize_server_default_user_policy(
                    data.default_user_policy, data.server_type
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = await service.add_server(
        data.name,
        data.server_type,
        data.url,
        data.api_key,
        registration_expiry_months=data.registration_expiry_months,
        allow_request=data.allow_request,
        request_points_mode=data.request_points_mode,
        request_points_fixed=data.request_points_fixed,
        request_points_ratio=data.request_points_ratio,
        allow_nfo=data.allow_nfo,
        enable_mkv_process=data.enable_mkv_process,
        enable_llm_translate=data.enable_llm_translate,
        enable_watch_time=data.enable_watch_time,
        enable_watch_time_renewal=data.enable_watch_time_renewal,
        watch_time_renewal_threshold_seconds=data.watch_time_renewal_threshold_seconds,
        enable_traffic=data.enable_traffic,
        enable_traffic_alert=data.enable_traffic_alert,
        enable_traffic_auto_suspend=data.enable_traffic_auto_suspend,
        traffic_quota_bytes=data.traffic_quota_bytes,
        allow_traffic_pack=data.allow_traffic_pack,
        traffic_pack_points_mode=data.traffic_pack_points_mode,
        traffic_pack_points_fixed=data.traffic_pack_points_fixed,
        traffic_pack_points_ratio=data.traffic_pack_points_ratio,
        traffic_pack_size_gib=data.traffic_pack_size_gib,
        default_user_policy=serialized_default_policy,
        qb_username=data.qb_username,
        qb_torrent_limit=data.qb_torrent_limit,
        qb_server_id=data.qb_server_id,
    )
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {"success": True, "message": result.message}


@router.patch(
    "/servers/{server_id}",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def update_server(
    request: Request,
    server_id: int,
    data: ServerUpdate,
    session: AsyncSession = Depends(get_db),
):
    """更新指定服务器实例的基础信息、注册策略与有效期配置

    副作用：可能更新数据库中的服务器配置、重载对应客户端，或在关闭 NSFW 时撤权。

    Args:
        request: FastAPI 请求对象（用于访问应用状态中的客户端字典）
        server_id: 目标服务器 ID
        data: 部分更新字段载体，含 ``registration_expiry_months`` 等配置
        session: 异步数据库会话

    Returns:
        包含操作结果布尔值与提示文本的字典
    """
    service = SettingsServices(request.app, session)
    repo = ServerRepository(session)

    # 状态切换（提前记录变更前 NSFW 开关，防止未改动 NSFW 时每次保存都触发全服撤权）
    current_server = await repo.get_by_id(server_id)
    previous_nsfw_enabled = (
        bool(current_server.nsfw_enabled) if current_server is not None else None
    )

    if (
        current_server
        and data.is_enabled is not None
        and current_server.is_enabled != data.is_enabled
    ):
        await service.toggle_server_status(server_id)

    # 基础信息
    update_dict = {}
    if data.name:
        update_dict["name"] = data.name
    if data.url:
        update_dict["url"] = data.url
    if data.api_key and data.api_key != "******":
        update_dict["api_key"] = data.api_key
    # 非空布尔/数值/定价字段按统一白名单增量写入，避免局部 PATCH 覆盖未传字段
    for field in (
        "tos",
        "allow_subtitle_upload",
        "allow_request",
        "request_points_mode",
        "request_points_fixed",
        "request_points_ratio",
        "allow_nfo",
        "enable_mkv_process",
        "enable_llm_translate",
        "enable_watch_time",
        "enable_watch_time_renewal",
        "watch_time_renewal_threshold_seconds",
        "enable_traffic",
        "enable_traffic_alert",
        "enable_traffic_auto_suspend",
        "traffic_quota_bytes",
        "allow_traffic_pack",
        "traffic_pack_points_mode",
        "traffic_pack_points_fixed",
        "traffic_pack_points_ratio",
        "traffic_pack_size_gib",
        "qb_torrent_limit",
    ):
        field_val = getattr(data, field)
        if field_val is not None:
            update_dict[field] = field_val
    if data.qb_username is not None:
        update_dict["qb_username"] = data.qb_username.strip() or None
    if "qb_server_id" in data.model_fields_set:
        if data.qb_server_id is not None:
            qb_target = await repo.get_by_id(data.qb_server_id)
            if not qb_target or qb_target.server_type != ServerType.QBITTORRENT:
                raise HTTPException(
                    status_code=400,
                    detail="绑定的 qBittorrent 服务器不存在或类型不匹配",
                )
        update_dict["qb_server_id"] = data.qb_server_id

    if data.default_user_policy is not None:
        if not current_server:
            raise HTTPException(status_code=404, detail="服务器不存在")
        if current_server.server_type not in (ServerType.EMBY, ServerType.JELLYFIN):
            raise HTTPException(
                status_code=400,
                detail="仅 Emby/Jellyfin 媒体服务器支持配置默认 UserPolicy",
            )
        try:
            update_dict["default_user_policy"] = serialize_server_default_user_policy(
                data.default_user_policy, current_server.server_type
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 路径映射 (List -> JSON)：仅当请求显式传入 path_mappings 时覆盖，避免局部 PATCH 误清空既有映射并触发客户端重载
    if "path_mappings" in data.model_fields_set and data.path_mappings is not None:
        mapping_dict = {
            m.remote: m.local for m in data.path_mappings if m.remote and m.local
        }
        update_dict["path_mappings"] = json.dumps(mapping_dict)

    # 高级配置：先记录旧值；仅在遗留话题字段实际发生修改时才同步单服通知路由，避免常规保存误覆盖高级路由规则
    prev_notify_topic = current_server.notify_topic_id if current_server else None
    prev_request_topic = (
        current_server.request_notify_topic_id if current_server else None
    )
    route_repo = NotificationRouteRepository(session)
    if data.notify_topic_id is not None:
        await repo.update_notify_config(server_id, notify_topic_id=data.notify_topic_id)
        if (
            "notify_topic_id" in data.model_fields_set
            and data.notify_topic_id != prev_notify_topic
        ):
            await _sync_changed_legacy_topic_route(
                route_repo,
                server_id=server_id,
                category=NotificationRouteCategory.MEDIA_LIBRARY,
                topic_val=data.notify_topic_id,
            )
    if data.request_notify_topic_id is not None:
        await repo.update_notify_config(
            server_id, request_notify_topic_id=data.request_notify_topic_id
        )
        if (
            "request_notify_topic_id" in data.model_fields_set
            and data.request_notify_topic_id != prev_request_topic
        ):
            await _sync_changed_legacy_topic_route(
                route_repo,
                server_id=server_id,
                category=NotificationRouteCategory.MEDIA_REQUEST,
                topic_val=data.request_notify_topic_id,
            )

    # 注册策略
    policy_kwargs = {}
    if data.registration_mode:
        policy_kwargs["mode"] = data.registration_mode
    if data.registration_count_limit is not None:
        policy_kwargs["count"] = data.registration_count_limit
    if data.registration_time_limit is not None:
        policy_kwargs["time"] = data.registration_time_limit
    if data.registration_external_url is not None:
        policy_kwargs["external_url"] = data.registration_external_url
    if data.registration_external_parser is not None:
        policy_kwargs["external_parser"] = data.registration_external_parser

    if policy_kwargs:
        await repo.update_policy_config(server_id, **policy_kwargs)

    # 有效期与 NSFW (Global for server)
    expiry_kwargs = {}
    if data.registration_expiry_months is not None:
        expiry_kwargs["expiry_months"] = data.registration_expiry_months
    if data.code_expiry_days is not None:
        expiry_kwargs["code_days"] = data.code_expiry_days

    if expiry_kwargs:
        await repo.update_expiry_config(server_id, **expiry_kwargs)

    # 执行基础信息更新与重载（仅在连接或鉴权等客户端相关字段变更时重载客户端，避免开关切换触发不必要重连）
    traffic_release_note = ""
    quota_changed = (
        data.traffic_quota_bytes is not None
        and current_server is not None
        and int(data.traffic_quota_bytes)
        != int(current_server.traffic_quota_bytes or 0)
    )
    if update_dict:
        if data.enable_traffic is False or quota_changed:
            # 必须在将单服流量关闭或变更配额之前先把在途字节刷盘，保证后续余量重算与解封准确
            await AccountService(request.app, session).flush_pending_traffic()
        await repo.update_basic_info(server_id, **update_dict)
        invalidate_traffic_server_cache(request.app)
        if data.enable_watch_time is False:
            # 关闭单台服务器播放时长采集时立即停用活跃会话并清理 Emby 心跳（保留历史记录）
            await service.handle_watch_time_disabled(server_id)
        if data.enable_llm_translate is False:
            # 关闭单台服务器 LLM 翻译时立即清理该服务器待执行的翻译与重试调度任务
            remove_server_translate_jobs(
                getattr(request.app.state, "scheduler", None), server_id
            )
        if (
            data.enable_traffic is False
            or data.enable_traffic_auto_suspend is False
            or quota_changed
        ):
            # 关流/关停用/调整配额后，按最新配额与开关安全释放符合条件的超流停用账号
            account_service = AccountService(request.app, session)
            (
                released_count,
                failed_count,
            ) = await account_service.release_traffic_suspensions(server_id)
            if (
                data.enable_traffic is False
                or data.enable_traffic_auto_suspend is False
                or released_count > 0
                or failed_count > 0
            ):
                traffic_release_note = format_traffic_release_summary(
                    released_count, failed_count, as_suffix=True
                )
        if {"name", "url", "api_key", "path_mappings", "qb_username"} & set(
            update_dict
        ):
            server = await repo.get_by_id(server_id)
            if server and server.is_enabled:
                await service._reload_server_client(server)

    # 仅在 NSFW 开关实际发生变化时才执行库配置更新与存量账户撤权，避免常规保存反复触发全服撤权
    nsfw_changed = (
        data.nsfw_enabled is not None
        and previous_nsfw_enabled is not None
        and bool(data.nsfw_enabled) != previous_nsfw_enabled
    )
    if nsfw_changed:
        target_nsfw = bool(data.nsfw_enabled)
        await repo.update_nsfw_config(server_id, enabled=target_nsfw)
        if not target_nsfw:
            account_service = AccountService(request.app, session)
            revoked_count, failed_count = await account_service.revoke_server_nsfw(
                server_id
            )
            if failed_count > 0:
                return {
                    "success": True,
                    "message": (
                        f"NSFW 开关已关闭（已禁止成员新开启），但有 {failed_count} 个现有账户撤权失败，"
                        f"已加入待重试队列，请检查媒体服务器状态{traffic_release_note}。"
                    ),
                }
            return {
                "success": True,
                "message": (
                    f"配置已保存，NSFW 开关已关闭，已撤销 {revoked_count} 个现有账户的 NSFW 权限"
                    f"{traffic_release_note}。"
                ),
            }
        pending = getattr(request.app.state, "_pending_nsfw_revokes", None)
        if isinstance(pending, dict):
            pending.pop(server_id, None)

    return {"success": True, "message": f"配置已保存{traffic_release_note}"}


@router.post(
    "/servers/{server_id}/sync_default_policy",
    dependencies=[Depends(validate_admin_access)],
    response_model=DefaultUserPolicySyncResponse,
)
async def sync_server_default_policy(
    request: Request,
    server_id: int,
    payload: DefaultUserPolicyDto | None = Body(default=None),
    session: AsyncSession = Depends(get_db),
):
    """保存（可选）并将指定服务器的默认 UserPolicy 同步应用至全部存量媒体账号"""
    repo = ServerRepository(session)
    server = await repo.get_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="服务器不存在")
    if server.server_type not in (ServerType.EMBY, ServerType.JELLYFIN):
        raise HTTPException(
            status_code=400,
            detail="仅 Emby/Jellyfin 媒体服务器支持同步 UserPolicy",
        )

    if payload is not None:
        try:
            serialized = serialize_server_default_user_policy(
                payload, server.server_type
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await repo.update_basic_info(server_id, default_user_policy=serialized)

    result = await AccountService(request.app, session).sync_server_default_user_policy(
        server_id
    )
    extra = result.extra_data if isinstance(result.extra_data, dict) else {}
    if not result.success and "total_count" not in extra:
        status = 404 if extra.get("not_found") else 400
        raise HTTPException(status_code=status, detail=result.message)

    return DefaultUserPolicySyncResponse(
        success=result.success,
        message=result.message,
        total_count=int(extra.get("total_count", 0)),
        synced_count=int(extra.get("synced_count", 0)),
        skipped_count=int(extra.get("skipped_count", 0)),
        failed_count=int(extra.get("failed_count", 0)),
    )


@router.delete(
    "/servers/{server_id}",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def delete_server_endpoint(
    request: Request, server_id: int, session: AsyncSession = Depends(get_db)
):
    """删除服务器"""
    service = SettingsServices(request.app, session)
    result = await service.delete_server(server_id)
    return {"success": True, "message": result.message}


# --- NSFW Library APIs ---


@router.get(
    "/servers/{server_id}/nsfw_libraries",
    dependencies=[Depends(validate_admin_access)],
    response_model=list[NsfwLibraryDto],
)
async def get_nsfw_libs(
    request: Request, server_id: int, session: AsyncSession = Depends(get_db)
):
    """获取NSFW库列表"""
    service = SettingsServices(request.app, session)
    try:
        return await service.get_nsfw_libraries_data(server_id)
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/servers/{server_id}/nsfw_libraries/{lib_id}/toggle",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def toggle_nsfw_lib(
    request: Request,
    server_id: int,
    lib_id: str,
    session: AsyncSession = Depends(get_db),
):
    """切换NSFW库状态"""
    service = SettingsServices(request.app, session)
    try:
        result = await service.toggle_nsfw_library(server_id, lib_id)
        return {"success": True, "message": result.message}
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise HTTPException(status_code=500, detail=str(e)) from e


# --- Library Binding APIs ---


@router.get(
    "/servers/{server_id}/libraries",
    dependencies=[Depends(validate_admin_access)],
    response_model=list[LibraryDto],
)
async def get_libraries(
    request: Request, server_id: int, session: AsyncSession = Depends(get_db)
):
    """获取库列表"""
    service = SettingsServices(request.app, session)
    try:
        return await service.get_libraries_data(server_id)
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/bindings/options",
    dependencies=[Depends(validate_admin_access)],
    response_model=list[ArrServerDto],
)
async def get_binding_options(
    request: Request, session: AsyncSession = Depends(get_db)
):
    """获取绑定选项(Arr服务器列表)"""
    service = SettingsServices(request.app, session)
    return await service.get_arr_servers_data()


@router.get("/arr/{server_id}/resources", dependencies=[Depends(validate_admin_access)])
async def get_arr_resources(
    request: Request, server_id: int, session: AsyncSession = Depends(get_db)
):
    """获取Arr资源(质量配置文件和根文件夹)"""
    service = SettingsServices(request.app, session)
    try:
        profiles, folders = await service.get_arr_resources(server_id)
        return {"profiles": profiles, "folders": folders}
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/servers/{server_id}/bindings/{library_name}",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def save_binding(
    request: Request,
    server_id: int,
    library_name: str,
    data: BindingUpdate,
    session: AsyncSession = Depends(get_db),
):
    """保存库绑定"""
    service = SettingsServices(request.app, session)
    try:
        await service.save_library_binding(
            library_name,
            server_id,
            data.arr_id,
            data.quality_profile_id,
            data.root_folder,
        )
        return {"success": True, "message": "绑定已保存"}
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete(
    "/servers/{server_id}/bindings/{library_name}",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def delete_binding(
    request: Request,
    server_id: int,
    library_name: str,
    session: AsyncSession = Depends(get_db),
):
    """解除库绑定"""
    service = SettingsServices(request.app, session)
    try:
        await service.unbind_library(server_id, library_name)
        return {"success": True, "message": "已解除绑定"}
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/servers/{server_id}/rebuild_metadata",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse,
)
async def rebuild_server_metadata(
    request: Request,
    server_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    sonarr_clients: dict[int, SonarrClient] = Depends(get_sonarr_clients),
    radarr_clients: dict[int, RadarrClient] = Depends(get_radarr_clients),
    tmdb_client: TmdbClient | None = Depends(get_tmdb_client),
    tvdb_client: TvdbClient | None = Depends(get_tvdb_client),
):
    """触发 Sonarr 元数据重建任务"""
    if not tmdb_client:
        raise HTTPException(
            status_code=400, detail="TMDB API Key 未配置，无法执行 NFO 重建任务"
        )

    repo = ServerRepository(session)
    server = await repo.get_by_id(server_id)

    if not server:
        raise HTTPException(status_code=404, detail="服务器不存在")

    if not server.allow_nfo:
        raise HTTPException(status_code=400, detail="该服务器未启用 NFO 生成功能")

    client = None
    if server.server_type == ServerType.SONARR:
        client = sonarr_clients.get(server_id)
    elif server.server_type == ServerType.RADARR:
        client = radarr_clients.get(server_id)
    else:
        raise HTTPException(
            status_code=400, detail="目前仅支持 Sonarr 和 Radarr 服务器"
        )

    if not client:
        raise HTTPException(status_code=500, detail="客户端未连接")

    # 添加后台任务
    if isinstance(client, SonarrClient):
        background_tasks.add_task(
            rebuild_sonarr_metadata_task, client, tmdb_client, tvdb_client
        )
    elif isinstance(client, RadarrClient):
        background_tasks.add_task(rebuild_radarr_metadata_task, client, tmdb_client)

    return {
        "success": True,
        "message": "元数据重建任务已在后台启动，请查看日志关注进度。",
    }
