import re
from datetime import datetime, timedelta
from typing import cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from clients.ai_client import AIClientWarper
from clients.tmdb_client import TmdbClient
from core.config import genre_mapping
from models.protocols import BaseItem
from models.tmdb import TmdbFindPayload
from services.media_service import MediaService


async def translate_emby_item(server_id: int, item_id: str) -> None:
    """翻译 Emby/Jellyfin 媒体项的名称、排序名称和概述字段。
    Args:
        scheduler (AsyncIOScheduler): 任务调度器，用于安排重试任务。
        tmdb_client (TmdbClient): TMDB 客户端，用于获取媒体信息。
        media_client (MediaService): 媒体服务客户端，用于获取和更新媒体项信息。
        ai_client (AIClientWarper): AI 客户端，用于执行翻译任务。
        item_id (str): 媒体项的唯一标识符。
    """
    from main import app
    scheduler: AsyncIOScheduler = app.state.scheduler
    tmdb_client: TmdbClient = app.state.tmdb_client
    media_clients: dict[int, MediaService] = app.state.media_clients
    ai_client: AIClientWarper = app.state.ai_client

    media_client = media_clients.get(server_id)
    if not media_client:
        logger.error("服务器 [{}] 客户端未运行或不存在，跳过翻译任务: {}", server_id, item_id)
        return
    is_translated = False

    item_info: BaseItem | None = await media_client.get_item_info(item_id)
    if item_info is None:
        return

    item = cast(BaseItem, item_info)

    imdb_id = item.ProviderIds.get('Imdb') or item.ProviderIds.get('IMDB')
    tvdb_id = item.ProviderIds.get('Tvdb') or item.ProviderIds.get('TVDB')
    tmdb_info = None
    if imdb_id and tmdb_info is None:
        tmdb_info = await tmdb_client.find_info_by_external_id('imdb_id', imdb_id)
    if tvdb_id and tmdb_info is None:
        tmdb_info = await tmdb_client.find_info_by_external_id('tvdb_id', tvdb_id)

    if not isinstance(tmdb_info, TmdbFindPayload):
        logger.warning("未找到项目 {} 的 TMDB 信息", item_id)

    fields_to_translate_item = {
        'Name': item.Name,
        'Overview': item.Overview
    }

    updates = {}

    for field, text in fields_to_translate_item.items():
        # 检查文本是否为空或包含中文字符
        if not text or not isinstance(text, str) or re.search(r'[\u4e00-\u9fff]', text):
            updates[field] = text
            continue

        is_translated = True
        translated_text = None

        if isinstance(tmdb_info, TmdbFindPayload) and hasattr(tmdb_info.tv_episode_results, field.lower()):
            translated_text = getattr(tmdb_info.tv_episode_results, field.lower())
        if not translated_text:
            translated_text = await ai_client.translate(field, text)

        if translated_text:
            updates[field] = translated_text
        else:
            updates[field] = text
            logger.warning("项目 {} 中的字段 {} 翻译失败：{}", field, item_id, text)

    if item.Genres:
        updates['Genres'] = [genre_mapping.get(genre, genre) for genre in item.Genres]

    if updates:
        item = item.model_copy(update=updates)

    if is_translated:
        logger.info("[{}]正在翻译项目 {}：{}", server_id, item_id, item.Name)
        await media_client.post_item_info(item_id, item)
        scheduler.add_job(
            translate_media_item,
            'date',
            run_date=(datetime.now() + timedelta(days=8)),
            id=f'translate_media_item_{server_id}_{item_id}',
            replace_existing=True,
            args=[server_id, item_id]
        )
        logger.info("[{}]计划在 8 天后重试项目 {}", server_id, item_id)
    else:
        logger.info("[{}]无需翻译ID: {}", server_id, item_id)

async def cancel_translate_media_item(server_id: int, item_id: str) -> None:
    """取消已计划的翻译任务。
    Args:
        scheduler (AsyncIOScheduler): 任务调度器，用于管理计划的任务。
        item_id (str): 媒体项的唯一标识符。
    """
    from main import app
    scheduler: AsyncIOScheduler = app.state.scheduler

    job_id = f'translate_media_item_{server_id}_{item_id}'
    job = scheduler.get_job(job_id)
    if job:
        scheduler.remove_job(job_id)
        logger.info("已取消项目 {} 的翻译任务", item_id)
    else:
        logger.info("项目 {} 没有计划的翻译任务", item_id)
