import asyncio
import re
from pathlib import Path

import aiofiles
from loguru import logger

from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.template_manager import template_manager
from models.sonarr import (EpisodeResource, SeriesResource,
                           SonarrWebhookDownloadPayload,
                           SonarrWebhookSeriesAddPayload)
from models.tmdb import TmdbEpisode, TmdbTvSeries
from models.tvdb import TvdbData, TvdbEpisodesData, TvdbPayload


async def _generate_and_save_nfo(template_name: str, context: dict, file_path: Path):
    """通用 NFO 生成与保存逻辑"""
    nfo_content = await template_manager.render(template_name, context)

    if not nfo_content:
        logger.error("{} 模板渲染返回空，跳过文件创建", template_name)
        return

    try:
        async with aiofiles.open(file_path, 'w', encoding='utf-8') as nfo_file:
            await nfo_file.write(nfo_content)
        logger.info("已创建 NFO 文件: {}", file_path)
    except OSError as e:
        logger.error("写入 NFO 文件失败 (IO错误): {} - {}", file_path, e)

async def create_series_nfo_from_resource(
    series: SeriesResource,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None
) -> None:
    """从 API 资源对象创建剧集 NFO"""
    if not series.path:
        return

    tvdb_payload = await tvdb.series_translations(series.tvdbId) if tvdb else None
    tmdb_payload = await tmdb.get_tv_series_details(series.tmdbId)

    context = {
        "title": getattr(tvdb_payload, 'name', None) or (tmdb_payload.name if tmdb_payload else series.title),
        "original_title": getattr(tmdb_payload, 'original_name', None),
        "plot": getattr(tvdb_payload, 'overview', None) or (tmdb_payload.overview if tmdb_payload else series.overview),
        "genres": (list(tmdb_payload.genres) if tmdb_payload and tmdb_payload.genres else series.genres),
        "premiered": getattr(tmdb_payload, 'first_air_date', None),
        "imdb_id": series.imdbId,
        "tvdb_id": series.tvdbId,
        "tmdb_id": series.tmdbId,
    }

    nfo_path = Path(series.path) / 'tvshow.nfo'
    await _generate_and_save_nfo("tvshow.nfo.j2", context, nfo_path)

async def create_episode_nfo_from_resource(
    series: SeriesResource,
    episode: EpisodeResource,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None
) -> None:
    """从 API 资源对象创建单集 NFO"""
    if not episode.hasFile or not episode.episodeFile or not episode.episodeFile.path:
        return

    tvdb_id = episode.tvdbId
    series_tmdb_id = series.tmdbId

    # 数据获取逻辑复用 Webhook 中的逻辑
    tvdb_data: TvdbData | None = None
    tvdb_ext_data: TvdbEpisodesData | None = None

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

    tmdb_ep: TmdbEpisode | None = None
    tmdb_find = await tmdb.find_info_by_external_id('tvdb_id', str(tvdb_id))
    if tmdb_find and tmdb_find.tv_episode_results:
        tmdb_ep = tmdb_find.tv_episode_results[0]

    # 回退匹配逻辑
    if not tmdb_ep and series_tmdb_id > 0:
        target_air_date = None
        if tvdb_ext_data:
            target_air_date = tvdb_ext_data.aired
        elif episode.airDate:
            target_air_date = str(episode.airDate)

        if target_air_date:
            tmdb_season = await tmdb.get_tv_seasons_details(series_tmdb_id, episode.seasonNumber)
            if not tmdb_season:
                # 尝试获取最新季
                series_info = await tmdb.get_tv_series_details(series_tmdb_id)
                if series_info and series_info.seasons:
                    last_season = series_info.seasons[-1]
                    tmdb_season = await tmdb.get_tv_seasons_details(series_tmdb_id, last_season.season_number)

            if tmdb_season and tmdb_season.episodes:
                for ep in tmdb_season.episodes:
                    if ep.air_date == target_air_date:
                        tmdb_ep = ep
                        break

    tmdb_title = None
    if tmdb_ep and tmdb_ep.name:
        if not re.match(r'^(第[\d ]+集|Episode\s*[\d ]+)$', tmdb_ep.name, re.IGNORECASE):
            tmdb_title = tmdb_ep.name

    title = (
        (tvdb_data.name if tvdb_data and tvdb_data.name else None) or
        tmdb_title or
        episode.title
    )
    plot = (
        (tvdb_data.overview if tvdb_data and tvdb_data.overview else None) or
        (tmdb_ep.overview if tmdb_ep and tmdb_ep.overview else None) or
        episode.overview
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

    nfo_path = Path(episode.episodeFile.path).with_suffix('.nfo')
    await _generate_and_save_nfo("episode.nfo.j2", context, nfo_path)

async def rebuild_sonarr_metadata_task(
    sonarr_client: SonarrClient,
    tmdb_client: TmdbClient | None,
    tvdb_client: TvdbClient | None
) -> None:
    """遍历 Sonarr 库并重建所有 NFO"""
    if not tmdb_client:
        logger.error("TMDB 客户端未配置，无法执行 Sonarr 元数据重建任务")
        return
    logger.info("开始重建 Sonarr ({}) 元数据...", sonarr_client.server_name)

    try:
        all_series = await sonarr_client.get_all_series()
        if not all_series:
            logger.warning("Sonarr 库为空或获取失败。")
            return

        total = len(all_series)
        logger.info("共获取到 {} 部剧集，开始处理...", total)

        for index, series in enumerate(all_series, 1):
            if series.id is None:
                continue
            logger.info("[{}/{}] 处理剧集: {}", index, total, series.title)

            # 1. 生成 tvshow.nfo
            await create_series_nfo_from_resource(series, tmdb_client, tvdb_client)

            # 2. 获取剧集的所有集数信息
            episodes = await sonarr_client.get_episode_by_series_id(series.id)
            if episodes:
                for ep in episodes:
                    # 仅为已下载的文件生成 NFO
                    if ep.hasFile and ep.episodeFile:
                        await create_episode_nfo_from_resource(series, ep, tmdb_client, tvdb_client)
                        # 简单的速率限制，避免被 TMDB/TVDB 封禁
                        await asyncio.sleep(0.2)

            await asyncio.sleep(1) # 剧集间间隔

        logger.info("Sonarr ({}) 元数据重建完成。", sonarr_client.server_name)

    except Exception as e:
        logger.exception("重建元数据任务异常: {}", e)

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

    nfo_path = Path(payload.series.path) / 'tvshow.nfo'
    await _generate_and_save_nfo("tvshow.nfo.j2", context, nfo_path)

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

    nfo_path = Path(payload.episodeFile.path).with_suffix('.nfo')
    await _generate_and_save_nfo("episode.nfo.j2", context, nfo_path)
