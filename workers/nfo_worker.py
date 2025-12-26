from pathlib import Path
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

import aiofiles
from loguru import logger

from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from models.sonarr import (SonarrWebhookDownloadPayload,
                           SonarrWebhookSeriesAddPayload)
from models.tmdb import TmdbEpisode, TmdbFindPayload, TmdbTv
from models.tvdb import TvdbEpisodesData, TvdbSeriesData


async def create_series_nfo(payload: SonarrWebhookSeriesAddPayload, tmdb: TmdbClient, tvdb: TvdbClient | None = None) -> None:
    """创建剧集 NFO 文件
    Args:
        payload (WebhookSeriesAddPayload): Sonarr Webhook SeriesAdd 负载数据。
        tmdb (TmdbClient): 用于获取 TMDB 信息的客户端实例。
        tvdb (TvdbClient | None): 用于获取 TVDB 信息的客户端实例。
    """
    tvdb_payload: TvdbSeriesData | None = None
    tmdb_payload: TmdbTv | None = None

    tvdb_payload = await tvdb.series_extended(payload.series.tvdbId) if tvdb else None
    tmdb_payload = await tmdb.get_tv_details(payload.series.tmdbId)

    title = (
        (tvdb_payload and tvdb_payload.name) or
        (tmdb_payload and tmdb_payload.name) or
        payload.series.title
    )
    plot = (
        (tvdb_payload and tvdb_payload.overview) or
        (tmdb_payload and tmdb_payload.overview)
    )

    genre = set()
    if tvdb_payload and tvdb_payload.genres:
        genre.update(tvdb_payload.genres)
    if tmdb_payload and tmdb_payload.genres:
        genre.update(tmdb_payload.genres)
    if payload.series.genres:
        genre.update(payload.series.genres)

    root = Element('tvshow')
    SubElement(root, "title").text = title
    if tmdb_payload and tmdb_payload.original_name:
        SubElement(root, "originaltitle").text = tmdb_payload.original_name
    if plot:
        SubElement(root, "plot").text = plot
    if tmdb_payload and tmdb_payload.first_air_date:
        SubElement(root, "premiered").text = tmdb_payload.first_air_date

    for genre in genre:
        SubElement(root, "genre").text = genre

    SubElement(root, "uniqueid", type="imdb").text = payload.series.imdbId
    SubElement(root, "uniqueid", type="tvdb").text = str(payload.series.tvdbId)
    SubElement(root, "uniqueid", type="tmdb").text = str(payload.series.tmdbId)

    rough_string = tostring(root, encoding='utf-8', method='xml')
    reparsed = minidom.parseString(rough_string)
    nfo_content = reparsed.toprettyxml(indent="  ", encoding='utf-8', standalone=True).decode('utf-8')

    async with aiofiles.open(Path(payload.series.path) / 'tvshow.nfo', 'w', encoding='utf-8') as nfo_file:
        await nfo_file.write(nfo_content)
    logger.info("已为系列 {} 创建 tvshow.nfo", payload.series.title)

async def create_episode_nfo(payload: SonarrWebhookDownloadPayload, tmdb: TmdbClient, tvdb: TvdbClient | None = None) -> None:
    """创建剧集 NFO 文件
    Args:
        payload (WebhookDownloadPayload): Sonarr Webhook DownloadPayload负载数据。
        tmdb (TmdbClient): 用于获取 TMDB 信息的客户端实例。
    """
    if not payload.episodes or not payload.episodeFile:
        return
    tvdb_payload: TvdbEpisodesData | None = None
    tmdb_payload: TmdbFindPayload | None = None
    episode_info: TmdbEpisode | None = None

    tvdb_payload = await tvdb.episodes_extended(payload.episodes[0].tvdbId) if tvdb else None
    tmdb_payload = await tmdb.find_info_by_external_id('tvdb_id', str(payload.episodes[0].tvdbId))
    if tmdb_payload and isinstance(tmdb_payload, TmdbFindPayload) and tmdb_payload.tv_episode_results:
        episode_info = tmdb_payload.tv_episode_results[0]

    title = (
        (tvdb_payload and tvdb_payload.name) or
        (episode_info and episode_info.name) or
        payload.series.title
    )
    plot = (
        (tvdb_payload and tvdb_payload.overview) or
        (episode_info and episode_info.overview)
    )

    root = Element('episodedetails')

    SubElement(root, "title").text = title
    SubElement(root, "plot").text = plot
    if episode_info and episode_info.air_date:
        SubElement(root, "aired").text = episode_info.air_date
    if episode_info and episode_info.id:
        SubElement(root, "uniqueid", type="tmdb").text = str(episode_info.id)
    SubElement(root, "season").text = str(payload.episodes[0].seasonNumber)
    SubElement(root, "episode").text = str(payload.episodes[0].episodeNumber)
    SubElement(root, "uniqueid", type="tvdb").text = str(payload.episodes[0].tvdbId)

    rough_string = tostring(root, encoding='utf-8', method='xml')
    reparsed = minidom.parseString(rough_string)
    nfo_content = reparsed.toprettyxml(indent="  ", encoding='utf-8', standalone=True).decode('utf-8')
    async with aiofiles.open(Path(payload.episodeFile.path).with_suffix('.nfo'), 'w', encoding='utf-8') as nfo_file:
        await nfo_file.write(nfo_content)
    logger.info("已为剧集 {} 创建 NFO 文件", title)
