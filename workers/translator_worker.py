import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from clients.ai_client import AIClientWarper
from clients.tmdb_client import TmdbService
from core.config import genre_mapping
from models import tmdb
from models.tmdb import TmdbFindPayload
from services.media_service import MediaService

logger = logging.getLogger(__name__)

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

    item_info = await media_client.get_item_info(item_id)
    if not item_info:
        logging.error("No item info found for ID %s", item_id)
        return

    item = item_info.get('Items', [])[0] if 'Items' in item_info else item_info

    imdb_id = item.get('ProviderIds', {}).get('Imdb') or item.get('ProviderIds', {}).get('IMDB')
    tvdb_id = item.get('ProviderIds', {}).get('Tvdb') or item.get('ProviderIds', {}).get('TVDB')
    tmdb_info = None
    if imdb_id and tmdb_info is None:
        tmdb_info = await tmdb_client.get_info(imdb_id=imdb_id)
    if tvdb_id and tmdb_info is None:
        tmdb_info = await tmdb_client.get_info(tvdb_id=tvdb_id)

    if not tmdb_info or isinstance(tmdb_info, TmdbFindPayload):
        logging.warning("No TMDB info found for item %s", item_id)

    fields_to_translate_item = {
        'Name': item.get('Name'),
        'SortName': item.get('SortName'),
        'Overview': item.get('Overview')
        # 'Genres': item.get('Genres', [])
    }

    sync_sort_name = fields_to_translate_item['Name'] == fields_to_translate_item['SortName']

    for field, text in fields_to_translate_item.items():
        # 检查文本是否为空或包含中文字符
        if not text or not isinstance(text, str) or any('\u4e00' <= char <= '\u9fff' for char in text):
            continue

        is_translated = True
        translated_text = None

        if tmdb_info and getattr(tmdb_info, field.lower()):
            translated_text = getattr(tmdb_info, field.lower())
        else:
            translated_text = await ai_client.translate(field, text)

        if translated_text:
            item[field] = translated_text
        else:
            logging.warning("Translation failed for field %s in item %s: %s", field, item_id, text)

    if sync_sort_name:
        item['SortName'] = item['Name']

    if item.get('Genres'):
        item['Genres'] = [genre_mapping.get(genre, genre) for genre in item['Genres']]

    if is_translated:
        logging.info("Translating item %s: %s", item_id, item['Name'])
        await media_client.post_item_info(item_id, item)
        scheduler.add_job(
            translate_emby_item,
            'date',
            run_date=(datetime.now() + timedelta(days=8)),
            id=f'translate_emby_item_{item_id}',
            replace_existing=True,
            args=[item_id]
        )
        logging.info("Scheduled retry for item %s in 8 days", item_id)
