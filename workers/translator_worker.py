import re
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from clients.ai_client import AIClientWarper
from clients.tmdb_client import TmdbService
from core.config import genre_mapping
from models.emby import QueryResult_BaseItemDto
from models.tmdb import TmdbFindPayload, TmdbTv
from services.media_service import MediaService

from loguru import logger

async def translate_emby_item(item_id: str) -> None:
    """翻译 Emby 媒体项的名称、排序名称和概述字段。
    Args:
        scheduler (AsyncIOScheduler): 任务调度器，用于安排重试任务。
        tmdb_client (TmdbService): TMDB 客户端，用于获取媒体信息。
        media_client (MediaService): 媒体服务客户端，用于获取和更新媒体项信息。
        ai_client (AIClientWarper): AI 客户端，用于执行翻译任务。
        item_id (str): 媒体项的唯一标识符。
    """
    from main import app
    scheduler: AsyncIOScheduler = app.state.scheduler
    tmdb_client: TmdbService = app.state.tmdb_client
    media_client: MediaService = app.state.media_client
    ai_client: AIClientWarper = app.state.ai_client

    is_translated = False

    item_info: QueryResult_BaseItemDto | None = await media_client.get_item_info(item_id)
    if not item_info or item_info.TotalRecordCount == 0 or item_info.TotalRecordCount == 0 or not item_info.Items:
        logger.error("未找到 ID {} 的信息", item_id)
        return

    item = item_info.Items[0]

    imdb_id = item.ProviderIds.get('Imdb') or item.ProviderIds.get('IMDB')
    tvdb_id = item.ProviderIds.get('Tvdb') or item.ProviderIds.get('TVDB')
    tmdb_info = None
    if imdb_id and tmdb_info is None:
        tmdb_info = await tmdb_client.get_info(imdb_id=imdb_id)
    if tvdb_id and tmdb_info is None:
        tmdb_info = await tmdb_client.get_info(tvdb_id=tvdb_id)

    if not tmdb_info:
        logger.warning("未找到项目 {} 的 TMDB 信息", item_id)

    fields_to_translate_item = {
        'Name': item.Name,
        'SortName': item.SortName,
        'Overview': item.Overview
        # 'Genres': item.get('Genres', [])
    }

    updates = {}
    sync_sort_name: bool = fields_to_translate_item['Name'] == fields_to_translate_item['SortName']

    for field, text in fields_to_translate_item.items():
        # 检查文本是否为空或包含中文字符
        if not text or not isinstance(text, str) or re.search(r'[\u4e00-\u9fff]', text):
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
            logger.warning("项目 {} 中的字段 {} 翻译失败：{}", field, item_id, text)

    if sync_sort_name:
        updates['SortName'] = updates['Name']

    if item.Genres:
        updates['Genres'] = [genre_mapping.get(genre, genre) for genre in item.Genres]

    if updates:
        item = item.model_copy(update=updates)

    if is_translated:
        logger.info("正在翻译项目 {}：{}", item_id, item.Name)
        await media_client.post_item_info(item_id, item)
        scheduler.add_job(
            translate_emby_item,
            'date',
            run_date=(datetime.now() + timedelta(days=8)),
            id=f'translate_emby_item_{item_id}',
            replace_existing=True,
            args=[item_id]
        )
        logger.info("计划在 8 天后重试项目 {}", item_id)
