from pathlib import Path
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

import aiofiles
from loguru import logger

from clients.tmdb_client import TmdbService
from models.sonarr import SonarrPayload
from models.tmdb import TmdbFindPayload, TmdbTv


async def create_series_nfo(payload: SonarrPayload, tmdb: TmdbService) -> None:
    """创建剧集 NFO 文件
    Args:
        payload (SonarrPayload): Sonarr Webhook 负载数据。
        tmdb (TmdbService): 用于获取 TMDB 信息的客户端实例。
    """
    tmdb_payload = await tmdb.get_info(tmdb_id=str(payload.series.tmdbId))
    root = Element('tvshow')
    if tmdb_payload and isinstance(tmdb_payload, TmdbTv):
        if tmdb_payload.name:
            SubElement(root, "title").text = tmdb_payload.name
        if tmdb_payload.original_name:
            SubElement(root, "originaltitle").text = tmdb_payload.original_name
        if tmdb_payload.overview:
            SubElement(root, "plot").text = tmdb_payload.overview
        if tmdb_payload.first_air_date:
            SubElement(root, "premiered").text = tmdb_payload.first_air_date
        if tmdb_payload.genres:
            for genre in tmdb_payload.genres:
                SubElement(root, "genre").text = genre

    SubElement(root, "uniqueid", type="imdb").text = payload.series.imdbId
    SubElement(root, "uniqueid", type="tvdb").text = str(payload.series.tvdbId)
    if payload.series.tmdbId:
        SubElement(root, "uniqueid", type="tmdb").text = str(payload.series.tmdbId)
    for genre in payload.series.genres:
        SubElement(root, "genre").text = genre
    rough_string = tostring(root, encoding='utf-8', method='xml')
    reparsed = minidom.parseString(rough_string)
    nfo_content = reparsed.toprettyxml(indent="  ", encoding='utf-8', standalone=True).decode('utf-8')

    async with aiofiles.open(Path(payload.series.path) / 'tvshow.nfo', 'w', encoding='utf-8') as nfo_file:
        await nfo_file.write(nfo_content)
    logger.info("已为系列 {} 创建 tvshow.nfo", payload.series.title)

async def create_episode_nfo(payload: SonarrPayload, tmdb: TmdbService) -> None:
    """创建剧集 NFO 文件
    Args:
        payload (SonarrPayload): Sonarr Webhook 负载数据。
        tmdb (TmdbService): 用于获取 TMDB 信息的客户端实例。
    """
    if not payload.episodes or not payload.episodeFile:
        return
    tmdb_payload = await tmdb.get_info(tvdb_id=str(payload.episodes[0].tvdbId))
    root = Element('episodedetails')
    if tmdb_payload and isinstance(tmdb_payload, TmdbFindPayload) and tmdb_payload.tv_episode_results:
        episode_info = tmdb_payload.tv_episode_results[0]
        if episode_info.name:
            SubElement(root, "title").text = episode_info.name
        if episode_info.overview:
            SubElement(root, "plot").text = episode_info.overview
        if episode_info.air_date:
            SubElement(root, "aired").text = episode_info.air_date
        if episode_info.id:
            SubElement(root, "uniqueid", type="tmdb").text = str(episode_info.id)
    SubElement(root, "season").text = str(payload.episodes[0].seasonNumber)
    SubElement(root, "episode").text = str(payload.episodes[0].episodeNumber)
    SubElement(root, "uniqueid", type="tvdb").text = str(payload.episodes[0].tvdbId)
    rough_string = tostring(root, encoding='utf-8', method='xml')
    reparsed = minidom.parseString(rough_string)
    nfo_content = reparsed.toprettyxml(indent="  ", encoding='utf-8', standalone=True).decode('utf-8')
    async with aiofiles.open(Path(payload.episodeFile.path).with_suffix('.nfo'), 'w', encoding='utf-8') as nfo_file:
        await nfo_file.write(nfo_content)
    logger.info("已为剧集 {} 创建 NFO 文件", tmdb_payload.tv_episode_results[0].name if tmdb_payload and isinstance(tmdb_payload, TmdbFindPayload) and tmdb_payload.tv_episode_results else "未知剧集")
