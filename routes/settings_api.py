import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.database import get_db
from core.dependencies import (get_sonarr_clients, get_tmdb_client,
                               get_tvdb_client)
from core.webapp_auth import validate_admin_access
from models.orm import ServerInstance, ServerType
from models.schemas import (AdminDto, ArrServerDto, BindingUpdate, LibraryDto,
                            NsfwLibraryDto, QualityProfileDto, RootFolderDto,
                            ServerCreate, ServerDto, ServerUpdate,
                            SystemConfigResponse, ToggleResponse, TopicDto)
from repositories.config_repo import ConfigRepository
from repositories.server_repo import ServerRepository
from services.settings_service import SettingsServices
from workers.nfo_worker import rebuild_sonarr_metadata_task

router = APIRouter(prefix="/api/settings", tags=["settings"])

def mask_server_data(server: ServerInstance) -> dict:
    """脱敏并转换 ServerInstance"""
    data = {c.name: getattr(server, c.name) for c in server.__table__.columns}
    data['api_key'] = "******"

    # 路径映射 JSON -> List
    pm = data.get("path_mappings")
    mappings_list = []
    if isinstance(pm, str) and pm.strip():
        try:
            pm_dict = json.loads(pm)
            for remote, local in pm_dict.items():
                mappings_list.append({"remote": remote, "local": local})
        except json.JSONDecodeError:
            pass
    data["path_mappings"] = mappings_list
    return data

# --- System & Admin APIs ---

SYSTEM_KEY_MAP = {
    "enable_points": ConfigRepository.KEY_ENABLE_POINTS,
    "enable_verification": ConfigRepository.KEY_ENABLE_VERIFICATION,
    "enable_requestmedia": ConfigRepository.KEY_ENABLE_REQUESTMEDIA,
    "enable_cleanup_inactive_users": ConfigRepository.KEY_ENABLE_CLEANUP_INACTIVE_USERS,
}

@router.get("/system", dependencies=[Depends(validate_admin_access)], response_model=SystemConfigResponse)
async def get_system_config(session: AsyncSession = Depends(get_db)):
    repo = ConfigRepository(session)
    return {
        "enable_points": await repo.get_settings(ConfigRepository.KEY_ENABLE_POINTS, "true") == "true",
        "enable_verification": await repo.get_settings(ConfigRepository.KEY_ENABLE_VERIFICATION, "true") == "true",
        "enable_requestmedia": await repo.get_settings(ConfigRepository.KEY_ENABLE_REQUESTMEDIA, "true") == "true",
        "enable_cleanup_inactive_users": await repo.get_settings(ConfigRepository.KEY_ENABLE_CLEANUP_INACTIVE_USERS, "false") == "true",
    }

@router.post("/system/{key}/toggle", dependencies=[Depends(validate_admin_access)], response_model=ToggleResponse)
async def toggle_system_setting_endpoint(request: Request, key: str, session: AsyncSession = Depends(get_db)):
    db_key = SYSTEM_KEY_MAP.get(key)
    if not db_key:
        raise HTTPException(status_code=400, detail=f"Invalid key: {key}")
    service = SettingsServices(request.app, session)
    result = await service.toggle_system_setting(db_key)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    repo = ConfigRepository(session)
    new_value = await repo.get_settings(db_key)
    return {"success": True, "key": key, "db_key": db_key, "new_state": new_value == "true", "message": result.message}

@router.get("/admins", dependencies=[Depends(validate_admin_access)], response_model=list[AdminDto])
async def get_admins(request: Request, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    bot_admin_ids = await service.telegram_repo.get_admins()
    group_admins = await service.client.get_chat_admin_ids()
    result = []
    for user in group_admins:
        result.append({
            "id": user.id,
            "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
            "username": user.username,
            "is_bot_admin": user.id in bot_admin_ids
        })
    return result

@router.post("/admins/{user_id}/toggle", dependencies=[Depends(validate_admin_access)], response_model=ToggleResponse)
async def toggle_admin(request: Request, user_id: int, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    result = await service.toggle_admin(user_id)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {"success": True, "message": result.message}

@router.get("/topics", dependencies=[Depends(validate_admin_access)], response_model=list[TopicDto])
async def get_topics(request: Request, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    topic_map = await service.client.get_topic_map()
    return [{"id": k, "name": v} for k, v in topic_map.items()]

# --- Server Management APIs ---

@router.get("/servers", dependencies=[Depends(validate_admin_access)], response_model=list[ServerDto])
async def get_servers(session: AsyncSession = Depends(get_db)):
    repo = ServerRepository(session)
    servers = await repo.get_all()
    return [mask_server_data(s) for s in servers]

@router.get("/servers/{server_id}", dependencies=[Depends(validate_admin_access)], response_model=ServerDto)
async def get_server_detail(server_id: int, session: AsyncSession = Depends(get_db)):
    repo = ServerRepository(session)
    server = await repo.get_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return mask_server_data(server)

@router.post("/servers", dependencies=[Depends(validate_admin_access)], response_model=ToggleResponse)
async def create_server(request: Request, data: ServerCreate, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    result = await service.add_server(data.name, data.server_type, data.url, data.api_key)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.message)
    return {"success": True, "message": result.message}

@router.patch("/servers/{server_id}", dependencies=[Depends(validate_admin_access)], response_model=ToggleResponse)
async def update_server(request: Request, server_id: int, data: ServerUpdate, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    repo = ServerRepository(session)

    # 1. 状态切换
    if data.is_enabled is not None:
        await service.toggle_server_status(server_id)

    # 2. 基础信息
    update_dict = {}
    if data.name:
        update_dict['name'] = data.name
    if data.url:
        update_dict['url'] = data.url
    if data.api_key and data.api_key != "******":
        update_dict['api_key'] = data.api_key
    if data.tos is not None:
        update_dict['tos'] = data.tos

    # 3. 路径映射 (List -> JSON)
    if data.path_mappings is not None:
        mapping_dict = {m.remote: m.local for m in data.path_mappings if m.remote and m.local}
        update_dict['path_mappings'] = json.dumps(mapping_dict)

    # 4. 高级配置
    if data.notify_topic_id is not None:
        await repo.update_notify_config(server_id, notify_topic_id=data.notify_topic_id)
    if data.request_notify_topic_id is not None:
        await repo.update_notify_config(server_id, request_notify_topic_id=data.request_notify_topic_id)

    # 5. 注册策略
    policy_kwargs = {}
    if data.registration_mode:
        policy_kwargs['mode'] = data.registration_mode
    if data.registration_count_limit is not None:
        policy_kwargs['count'] = data.registration_count_limit
    if data.registration_time_limit is not None:
        policy_kwargs['time'] = data.registration_time_limit
    if data.registration_external_url is not None:
        policy_kwargs['external_url'] = data.registration_external_url
    if data.registration_external_parser is not None:
        policy_kwargs['external_parser'] = data.registration_external_parser

    if policy_kwargs:
        await repo.update_policy_config(server_id, **policy_kwargs)

    # 6. 有效期与 NSFW (Global for server)
    expiry_kwargs = {}
    if data.registration_expiry_days is not None:
        expiry_kwargs['expiry_days'] = data.registration_expiry_days
    if data.code_expiry_days is not None:
        expiry_kwargs['code_days'] = data.code_expiry_days

    if expiry_kwargs:
        await repo.update_expiry_config(server_id, **expiry_kwargs)

    if data.nsfw_enabled is not None:
        await repo.update_nsfw_config(server_id, enabled=data.nsfw_enabled)

    # 执行基础信息更新与重载
    if update_dict:
        await repo.update_basic_info(server_id, **update_dict)
        server = await repo.get_by_id(server_id)
        if server and server.is_enabled:
            await service._reload_server_client(server)

    return {"success": True, "message": "配置已保存"}

@router.delete("/servers/{server_id}", dependencies=[Depends(validate_admin_access)], response_model=ToggleResponse)
async def delete_server_endpoint(request: Request, server_id: int, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    result = await service.delete_server(server_id)
    return {"success": True, "message": result.message}

# --- NSFW Library APIs ---

@router.get("/servers/{server_id}/nsfw_libraries", dependencies=[Depends(validate_admin_access)], response_model=list[NsfwLibraryDto])
async def get_nsfw_libs(request: Request, server_id: int, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    try:
        return await service.get_nsfw_libraries_data(server_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post(
    "/servers/{server_id}/nsfw_libraries/{lib_id}/toggle",
    dependencies=[Depends(validate_admin_access)],
    response_model=ToggleResponse
)
async def toggle_nsfw_lib(request: Request, server_id: int, lib_id: str, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    try:
        result = await service.toggle_nsfw_library(server_id, lib_id)
        return {"success": True, "message": result.message}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

# --- Library Binding APIs ---

@router.get("/servers/{server_id}/libraries", dependencies=[Depends(validate_admin_access)], response_model=list[LibraryDto])
async def get_libraries(request: Request, server_id: int, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    try:
        return await service.get_libraries_data(server_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get("/bindings/options", dependencies=[Depends(validate_admin_access)], response_model=list[ArrServerDto])
async def get_binding_options(request: Request, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    return await service.get_arr_servers_data()

@router.get("/arr/{server_id}/resources", dependencies=[Depends(validate_admin_access)])
async def get_arr_resources(request: Request, server_id: int, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    try:
        profiles, folders = await service.get_arr_resources(server_id)
        return {
            "profiles": profiles,
            "folders": folders
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/bindings/{library_name}", dependencies=[Depends(validate_admin_access)], response_model=ToggleResponse)
async def save_binding(request: Request, library_name: str, data: BindingUpdate, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    try:
        await service.save_library_binding(library_name, data.server_id, data.quality_profile_id, data.root_folder)
        return {"success": True, "message": "绑定已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.delete("/bindings/{library_name}", dependencies=[Depends(validate_admin_access)], response_model=ToggleResponse)
async def delete_binding(request: Request, library_name: str, session: AsyncSession = Depends(get_db)):
    service = SettingsServices(request.app, session)
    try:
        await service.unbind_library(library_name)
        return {"success": True, "message": "已解除绑定"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/servers/{server_id}/rebuild_metadata", dependencies=[Depends(validate_admin_access)], response_model=ToggleResponse)
async def rebuild_server_metadata(
    request: Request,
    server_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    sonarr_clients: dict[int, SonarrClient] = Depends(get_sonarr_clients),
    tmdb_client: TmdbClient = Depends(get_tmdb_client),
    tvdb_client: TvdbClient = Depends(get_tvdb_client)
):
    """触发 Sonarr 元数据重建任务"""
    repo = ServerRepository(session)
    server = await repo.get_by_id(server_id)

    if not server:
        raise HTTPException(status_code=404, detail="服务器不存在")

    if server.server_type != ServerType.SONARR:
        raise HTTPException(status_code=400, detail="目前仅支持 Sonarr 服务器")

    client = sonarr_clients.get(server_id)
    if not client:
        raise HTTPException(status_code=500, detail="Sonarr 客户端未连接")

    # 添加后台任务
    background_tasks.add_task(
        rebuild_sonarr_metadata_task,
        client,
        tmdb_client,
        tvdb_client
    )

    return {"success": True, "message": "元数据重建任务已在后台启动，请查看日志关注进度。"}
