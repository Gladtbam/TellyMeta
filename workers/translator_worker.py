import re
from datetime import datetime, timedelta
from typing import cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from clients.ai_client import AIClientWarper
from clients.tmdb_client import TmdbClient
from core.config import genre_mapping
from models.protocols import BaseItem
from models.tmdb import TmdbEpisode
from services.media_service import MediaService


async def _get_tmdb_episode_info(
    item: BaseItem,
    tmdb_client: TmdbClient,
    media_client: MediaService
) -> TmdbEpisode | None:
    """Helper: 获取单集的 TMDB 信息，包含复杂的 ID 关联和回退逻辑"""
    tvdb_id = item.ProviderIds.get('Tvdb') or item.ProviderIds.get('TVDB')
    if tvdb_id:
        tmdb_find = await tmdb_client.find_info_by_external_id('tvdb_id', tvdb_id)
        if tmdb_find and tmdb_find.tv_episode_results:
            return tmdb_find.tv_episode_results[0]

    imdb_id = item.ProviderIds.get('Imdb') or item.ProviderIds.get('IMDB')
    if imdb_id:
        tmdb_find = await tmdb_client.find_info_by_external_id('imdb_id', imdb_id)
        if tmdb_find and tmdb_find.tv_episode_results:
            return tmdb_find.tv_episode_results[0]

    if not item.SeriesId:
        return None

    series_info: BaseItem | None = await media_client.get_item_info(item.SeriesId)
    if not series_info:
        return None

    series_tmdb_id = series_info.ProviderIds.get('Tmdb') or series_info.ProviderIds.get('TMDB')
    if not series_tmdb_id:
        return None

    season_num = item.ParentIndexNumber
    if season_num is None:
        return None

    tmdb_season = await tmdb_client.get_tv_seasons_details(series_tmdb_id, season_num)

    if not tmdb_season:
        logger.warning(f"获取 TMDB S{season_num} 失败，尝试获取最新季进行匹配")
        series_detail = await tmdb_client.get_tv_series_details(series_tmdb_id)
        if series_detail and series_detail.seasons:
            last_season = series_detail.seasons[-1]
            tmdb_season = await tmdb_client.get_tv_seasons_details(series_tmdb_id, last_season.season_number)

    if not tmdb_season or not tmdb_season.episodes:
        return None

    # 尝试通过首播日期匹配
    premiere_date = item.PremiereDate

    if premiere_date:
        for ep in tmdb_season.episodes:
            if ep.air_date == premiere_date.strftime('%Y-%m-%d'):
                return ep

    return None

async def translate_media_item(server_id: int, item_id: str) -> None:
    """翻译 Emby/Jellyfin 媒体项的名称、排序名称和概述字段。"""
    from main import app
    scheduler: AsyncIOScheduler = app.state.scheduler
    tmdb_client: TmdbClient | None = app.state.tmdb_client
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

    # 尝试从 TMDB 获取对应的信息
    tmdb_name: str | None = None
    tmdb_overview: str | None = None

    if item.Type == 'Episode':
        if not tmdb_client:
            tmdb_ep = None
        else:
            tmdb_ep = await _get_tmdb_episode_info(item, tmdb_client, media_client)
        if tmdb_ep:
            tmdb_overview = tmdb_ep.overview
            # 过滤无效标题
            if tmdb_ep.name and not re.match(r'^(第[\d ]+集|Episode\s*[\d ]+)$', tmdb_ep.name, re.IGNORECASE):
                tmdb_name = tmdb_ep.name

    # 电影或剧集处理 (Movie / Series) # Radarr 能够提供足够的中文元数据，因此跳过Movie
    else:
        imdb_id = item.ProviderIds.get('Imdb') or item.ProviderIds.get('IMDB')
        if imdb_id and tmdb_client:
            tmdb_find_res = await tmdb_client.find_info_by_external_id('imdb_id', imdb_id)
            if tmdb_find_res:
                res = None
                # if item.Type == 'Movie' and tmdb_find_res.movie_results:
                #     res = tmdb_find_res.movie_results[0]
                #     tmdb_name = res.title
                if item.Type == 'Series' and tmdb_find_res.tv_results:
                    res = tmdb_find_res.tv_results[0]
                    tmdb_name = res.name

                if res and res.overview:
                    tmdb_overview = res.overview

    fields_to_translate_item = {
        'Name': item.Name,
        'Overview': item.Overview
    }

    updates = {}

    for field, text in fields_to_translate_item.items():
        # 检查文本是否为空或包含中文字符
        if not text or not isinstance(text, str) or (re.search(r'[\u4e00-\u9fff]', text) and '（AI翻译）' not in text):
            updates[field] = text
            continue

        is_translated = True
        translated_text = None

        # 优先使用 TMDB 数据
        if field == 'Name' and tmdb_name:
            translated_text = tmdb_name
        elif field == 'Overview' and tmdb_overview:
            translated_text = tmdb_overview

        # 如果 TMDB 没有数据，使用 AI 翻译
        if not translated_text:
            translated_text = await ai_client.translate(field, text)

        if translated_text:
            updates[field] = f"{translated_text}（AI翻译）"
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
