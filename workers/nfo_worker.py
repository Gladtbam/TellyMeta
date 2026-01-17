import re
from pathlib import Path

import aiofiles
from loguru import logger

from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.template_manager import template_manager
from models.sonarr import (SonarrWebhookDownloadPayload,
                           SonarrWebhookSeriesAddPayload)
from models.tmdb import TmdbEpisode, TmdbTvSeries
from models.tvdb import TvdbData, TvdbEpisodesData, TvdbPayload


async def create_series_nfo(
    payload: SonarrWebhookSeriesAddPayload,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None
) -> None:
    """创建剧集 NFO 文件
    Args:
        payload (SonarrWebhookSeriesAddPayload): Sonarr Webhook SeriesAdd 负载数据。
        tmdb (TmdbClient): 用于获取 TMDB 信息的客户端实例。
        tvdb (TvdbClient | None): 用于获取 TVDB 信息的客户端实例。
    """
    tvdb_payload: TvdbPayload | None = None
    tmdb_payload: TmdbTvSeries | None = None

    tvdb_payload = await tvdb.series_translations(payload.series.tvdbId) if tvdb else None
    tmdb_payload = await tmdb.get_tv_series_details(payload.series.tmdbId)

    context = {
        "title": getattr(tvdb_payload, 'name', None) or (tmdb_payload.name if tmdb_payload else None),
        "original_title": getattr(tmdb_payload, 'original_name', None),
        "plot": getattr(tvdb_payload, 'overview', None) or (tmdb_payload.overview if tmdb_payload else None),
        "genres": (list(tmdb_payload.genres) if tmdb_payload and tmdb_payload.genres else []),
        "premiered": getattr(tmdb_payload, 'first_air_date', None),
        "imdb_id": payload.series.imdbId,
        "tvdb_id": payload.series.tvdbId,
        "tmdb_id": payload.series.tmdbId,
    }

    nfo_content = await template_manager.render("tvshow.nfo.j2", context)

    if not nfo_content:
        logger.error("TvShow NFO 模板渲染返回空，跳过文件创建")
        return

    nfo_path = Path(payload.series.path) / 'tvshow.nfo'
    try:
        async with aiofiles.open(nfo_path, 'w', encoding='utf-8') as nfo_file:
            await nfo_file.write(nfo_content)
        logger.info("已为系列 {} 创建 tvshow.nfo", payload.series.title)
    except OSError as e:
        logger.error("写入 NFO 文件失败 (IO错误): {} - {}", nfo_path, e)

async def create_episode_nfo(
    payload: SonarrWebhookDownloadPayload,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None
) -> None:
    """创建剧集 NFO 文件
    Args:
        payload (SonarrWebhookDownloadPayload): Sonarr Webhook DownloadPayload负载数据。
        tmdb (TmdbClient): 用于获取 TMDB 信息的客户端实例。
        tvdb (TvdbClient | None): 用于获取 TVDB 信息的客户端实例。
    """
    if not payload.episodes or not payload.episodeFile:
        return

    episode = payload.episodes[0]
    tvdb_id = episode.tvdbId
    series_tmdb_id = payload.series.tmdbId

    # TVDB
    tvdb_data: TvdbData | None = None
    tvdb_ext_data: TvdbEpisodesData | None = None # 用于回退匹配的扩展数据

    if tvdb:
        trans_payload = await tvdb.episodes_translations(tvdb_id)
        if trans_payload and isinstance(trans_payload.data, TvdbData):
            tvdb_data = trans_payload.data

        if not tvdb_data:
            try:
                tvdb_ext = await tvdb.episodes_extended(tvdb_id)
                tvdb_ext_data = tvdb_ext
            except Exception:
                pass

    # TMDB
    tmdb_ep: TmdbEpisode | None = None

    tmdb_find = await tmdb.find_info_by_external_id('tvdb_id', str(tvdb_id))
    if tmdb_find and tmdb_find.tv_episode_results:
        tmdb_ep = tmdb_find.tv_episode_results[0]
        logger.debug("通过 TVDB ID {} 关联到 TMDB 剧集: {}", tvdb_id, tmdb_ep.name)

    # 如果没有通过 TVDB ID 关联到 TMDB 剧集，尝试通过日期和季号匹配
    if not tmdb_ep and series_tmdb_id > 0:

        target_air_date = None
        if tvdb_ext_data:
            target_air_date = tvdb_ext_data.aired
        elif episode.airDate:
            target_air_date = str(episode.airDate)

        if target_air_date:
            logger.info("TMDB ID关联失败，尝试通过首播日期 {} 匹配 (S{})...", target_air_date, episode.seasonNumber)

            tmdb_season = await tmdb.get_tv_seasons_details(series_tmdb_id, episode.seasonNumber)

            if not tmdb_season:
                logger.warning(f"获取 TMDB S{episode.seasonNumber} 失败，尝试获取最新季进行匹配")
                series_info = await tmdb.get_tv_series_details(series_tmdb_id)
                if series_info and series_info.seasons:
                    last_season = series_info.seasons[-1]
                    logger.info("正在获取 TMDB 最新季 S{} 的详情", last_season.season_number)
                    tmdb_season = await tmdb.get_tv_seasons_details(series_tmdb_id, last_season.season_number)

            if tmdb_season and tmdb_season.episodes and target_air_date:
                for ep in tmdb_season.episodes:
                    if ep.air_date == target_air_date:
                        tmdb_ep = ep
                        logger.info("通过首播日期 {} 匹配到 TMDB 剧集: {}", target_air_date, ep.name)
                        break

    tmdb_title = None
    if tmdb_ep and tmdb_ep.name:
        if not re.match(r'^(第[\d ]+集|Episode\s*[\d ]+)$', tmdb_ep.name, re.IGNORECASE):
            tmdb_title = tmdb_ep.name

    title = (
        (tvdb_data.name if tvdb_data and tvdb_data.name else None) or
        tmdb_title
    )
    plot = (
        (tvdb_data.overview if tvdb_data and tvdb_data.overview else None) or
        (tmdb_ep.overview if tmdb_ep and tmdb_ep.overview else None)
    )

    context = {
        "title": title,
        "plot": plot,
        "season_number": episode.seasonNumber,
        "episode_number": episode.episodeNumber,
        "aired_date": (tmdb_ep.air_date if tmdb_ep else None) or str(episode.airDate),
        "tvdb_id": tvdb_id,
        "tmdb_id": tmdb_ep.id if tmdb_ep else None
    }

    nfo_content = await template_manager.render("episode.nfo.j2", context)

    if not nfo_content:
        logger.error("Episode NFO 模板渲染返回空，跳过文件创建")
        return

    nfo_path = Path(payload.episodeFile.path).with_suffix('.nfo')
    try:
        async with aiofiles.open(nfo_path, 'w', encoding='utf-8') as nfo_file:
            await nfo_file.write(nfo_content)
        logger.info("已为剧集 {} 创建 NFO 文件", title)
    except OSError as e:
        logger.error("写入 NFO 文件失败 (IO错误): {} - {}", nfo_path, e)
