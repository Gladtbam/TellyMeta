import asyncio
import contextlib
import re
from pathlib import Path

import aiofiles
import aiofiles.os as aio_os
from loguru import logger
from lxml import etree

from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from clients.tmdb_client import TmdbClient
from clients.tvdb_client import TvdbClient
from core.template_manager import template_manager
from models.sonarr import (EpisodeResource, SeriesResource, SonarrEpisode, SonarrSeries,
                           SonarrWebhookDownloadPayload,
                           SonarrWebhookSeriesAddPayload)
from models.tmdb import TmdbEpisode
from models.tvdb import TvdbData, TvdbEpisodesData


@contextlib.asynccontextmanager
async def _temporary_ignore_file(path: Path):
    """创建临时 .ignore 文件上下文管理器"""
    ignore_file = path / '.ignore'
    created = False
    if not await aio_os.path.exists(ignore_file):
        try:
            async with aiofiles.open(ignore_file, 'w', encoding='utf-8') as f:
                await f.write("# Ignore file created by TellyMeta.\n")
            created = True
            logger.debug("已创建忽略文件: {}", ignore_file)
        except OSError as e:
            logger.error("创建忽略文件失败: {} - {}", ignore_file, e)

    try:
        yield
    finally:
        if created:
            try:
                await aio_os.remove(ignore_file)
                logger.debug("已删除忽略文件: {}", ignore_file)
            except OSError as e:
                logger.error("删除忽略文件失败: {} - {}", ignore_file, e)


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


async def _get_episode_context(
    tmdb: TmdbClient,
    tvdb: TvdbClient | None,
    series_tmdb_id: int,
    ep_obj: EpisodeResource | SonarrEpisode,
) -> dict:
    """获取单集 NFO 上下文数据"""
    tvdb_id = ep_obj.tvdbId
    season_num = ep_obj.seasonNumber
    ep_num = ep_obj.episodeNumber
    air_date = str(ep_obj.airDate) if ep_obj.airDate else None

    # TVDB
    tvdb_data: TvdbData | None = None
    tvdb_ext_data: TvdbEpisodesData | None = None

    if tvdb:
        trans_payload = await tvdb.episodes_translations(tvdb_id)
        if trans_payload and isinstance(trans_payload.data, TvdbData):
            tvdb_data = trans_payload.data
        if not tvdb_data:
            with contextlib.suppress(Exception):
                tvdb_ext_data = await tvdb.episodes_extended(tvdb_id)

    # TMDB
    tmdb_ep: TmdbEpisode | None = None
    tmdb_find = await tmdb.find_info_by_external_id('tvdb_id', str(tvdb_id))
    if tmdb_find and tmdb_find.tv_episode_results:
        tmdb_ep = tmdb_find.tv_episode_results[0]

    if not tmdb_ep and series_tmdb_id > 0:
        target_air_date = (tvdb_ext_data.aired if tvdb_ext_data else None) or air_date

        if target_air_date:
            tmdb_season = await tmdb.get_tv_seasons_details(series_tmdb_id, season_num)
            if not tmdb_season:
                series_info = await tmdb.get_tv_series_details(series_tmdb_id)
                if series_info and series_info.seasons:
                    tmdb_season = await tmdb.get_tv_seasons_details(
                        series_tmdb_id,
                        series_info.seasons[-1].season_number
                    )

            if tmdb_season and tmdb_season.episodes:
                for ep in tmdb_season.episodes:
                    if ep.air_date == target_air_date:
                        tmdb_ep = ep
                        break

    tmdb_title = None
    if tmdb_ep and tmdb_ep.name and not re.match(r'^(第[\d ]+集|Episode\s*[\d ]+)$', tmdb_ep.name, re.IGNORECASE):
        tmdb_title = tmdb_ep.name

    title = (
        (tvdb_data.name if tvdb_data else None) or
        tmdb_title
    )

    plot = (
        (tvdb_data.overview if tvdb_data else None) or
        (tmdb_ep.overview if tmdb_ep else None)
    )

    return {
        "title": title,
        "plot": plot,
        "season_number": season_num,
        "episode_number": ep_num,
        "aired_date": (tmdb_ep.air_date if tmdb_ep else None) or air_date,
        "tvdb_id": tvdb_id,
        "tmdb_id": tmdb_ep.id if tmdb_ep else None
    }


async def create_series_nfo_from_resource(
    series: SeriesResource | SonarrSeries,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None,
    is_override: bool = True
) -> None:
    """从剧集资源对象创建 NFO"""
    if not series.path:
        return
    
    nfo_path = Path(series.path) / 'tvshow.nfo'
    if await aio_os.path.exists(nfo_path) and not is_override:
        logger.debug("NFO 文件已存在且未设置覆盖，跳过: {}", nfo_path)
        return

    tvdb_payload = await tvdb.series_translations(series.tvdbId) if tvdb else None
    tmdb_payload = await tmdb.get_tv_series_details(series.tmdbId)

    context = {
        "title": getattr(tvdb_payload, 'name', None) or (tmdb_payload.name if tmdb_payload else series.title),
        "original_title": getattr(tmdb_payload, 'original_name', None),
        "plot": getattr(tvdb_payload, 'overview', None) or (tmdb_payload.overview if tmdb_payload else series.overview),
        "genres": (list(tmdb_payload.genres) if tmdb_payload and tmdb_payload.genres else getattr(series, 'genres', [])),
        "premiered": getattr(tmdb_payload, 'first_air_date', None),
        "imdb_id": series.imdbId,
        "tvdb_id": series.tvdbId,
        "tmdb_id": series.tmdbId,
    }

    await _generate_and_save_nfo("tvshow.nfo.j2", context, nfo_path)

async def create_episode_nfo_from_resource(
    series: SeriesResource | SonarrSeries,
    episode: EpisodeResource,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None,
    is_override: bool = True
) -> None:
    """从 API 资源对象创建单集 NFO"""
    if not episode.hasFile or not episode.episodeFile or not episode.episodeFile.path:
        return

    nfo_path = Path(episode.episodeFile.path).with_suffix('.nfo')
    if await aio_os.path.exists(nfo_path) and not is_override:
        logger.debug("NFO 文件已存在且未设置覆盖，跳过: {}", nfo_path)
        return

    context = await _get_episode_context(tmdb, tvdb, series.tmdbId, episode)
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

            await create_series_nfo_from_resource(series, tmdb_client, tvdb_client)

            episodes = await sonarr_client.get_episode_by_series_id(series.id)
            if episodes:
                for ep in episodes:
                    if ep.hasFile and ep.episodeFile:
                        await create_episode_nfo_from_resource(series, ep, tmdb_client, tvdb_client)
                        await asyncio.sleep(0.2)

            await asyncio.sleep(1)

        logger.info("Sonarr ({}) 元数据重建完成。", sonarr_client.server_name)

    except Exception as e:
        logger.exception("重建元数据任务异常: {}", e)

async def create_series_nfo(
    payload: SonarrWebhookSeriesAddPayload,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None
) -> None:
    """Webhook: 创建剧集 NFO"""
    await create_series_nfo_from_resource(payload.series, tmdb, tvdb, False)

async def create_episode_nfo(
    payload: SonarrWebhookDownloadPayload,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None
) -> None:
    """Webhook: 创建单集 NFO"""
    if not payload.episodes or not payload.episodeFile:
        return

    episode = payload.episodes[0]
    nfo_path = Path(payload.episodeFile.path).with_suffix('.nfo')

    context = await _get_episode_context(tmdb, tvdb, payload.series.tmdbId, episode)
    await _generate_and_save_nfo("episode.nfo.j2", context, nfo_path)

async def handle_series_add_metadata(
    client: SonarrClient,
    payload: SonarrWebhookSeriesAddPayload,
    tmdb: TmdbClient,
    tvdb: TvdbClient | None = None
) -> None:
    """处理剧集添加事件"""
    async with _temporary_ignore_file(Path(payload.series.path)):
        await create_series_nfo(payload, tmdb, tvdb)
        await asyncio.sleep(60)

        logger.info("正在为新添加的剧集 {} 检查现有集数...", payload.series.title)
        try:
            episodes = await client.get_episode_by_series_id(payload.series.id)
            if episodes:
                count = 0
                for ep in episodes:
                    if ep.hasFile and ep.episodeFile and ep.episodeFile.path:
                        if client.path_mappings:
                            ep.episodeFile.path = client.to_local_path(ep.episodeFile.path)

                        await create_episode_nfo_from_resource(payload.series, ep, tmdb, tvdb, False)
                        count += 1

                if count > 0:
                    logger.info("已补全剧集 {} 的 {} 个现有文件 NFO", payload.series.title, count)
        except Exception as e:
            logger.error("为新剧集生成单集 NFO 失败: {}", e)

def _clean_nfo_xml_sync(file_path: Path) -> bool:
    """使用 lxml 清洗 NFO 文件 (同步)"""
    try:
        parser = etree.XMLParser(remove_blank_text=True, strip_cdata=False, recover=True)
        tree = etree.parse(str(file_path), parser)
        root = tree.getroot()
        modified = False

        for tag in ['thumb', 'poster', 'fanart']:
            for elem in root.findall(tag):
                if elem.text and elem.text.strip().startswith(('http', 'https')):
                    root.remove(elem)
                    modified = True

        for fanart in root.findall('fanart'):
            for thumb in fanart.findall('thumb'):
                if thumb.text and thumb.text.strip().startswith(('http', 'https')):
                    fanart.remove(thumb)
                    modified = True
            if len(list(fanart)) == 0:
                root.remove(fanart)
                modified = True

        if modified:
            tree.write(str(file_path), encoding='utf-8', xml_declaration=True, pretty_print=True)
            return True
    except Exception as e:
        logger.error("清洗 NFO 文件失败 {}: {}", file_path, e)
    return False


async def clean_radarr_nfo(movie_path: str | Path) -> None:
    """清洗电影目录 NFO"""
    path = Path(movie_path)
    if not await aio_os.path.exists(path):
        return

    async with _temporary_ignore_file(path):
        await asyncio.sleep(5) # Wait for FS

        nfo_files = []
        with contextlib.suppress(Exception):
            files = await aio_os.listdir(path)
            nfo_files = [path / f for f in files if f.lower().endswith('.nfo')]

        loop = asyncio.get_running_loop()
        for nfo_file in nfo_files:
            if await aio_os.path.exists(nfo_file):
                if await loop.run_in_executor(None, _clean_nfo_xml_sync, nfo_file):
                    logger.info("已清洗 NFO 中的在线链接: {}", nfo_file)

async def rebuild_radarr_nfo_clean_task(radarr_client: RadarrClient) -> None:
    """遍历 Radarr 库并清洗所有 NFO"""
    logger.info("开始清洗 Radarr ({}) NFO 元数据...", radarr_client.server_name)
    try:
        all_movies = await radarr_client.get_all_movies()
        if not all_movies:
            logger.warning("Radarr 库为空或获取失败。")
            return

        total = len(all_movies)
        logger.info("共获取到 {} 部电影，开始处理...", total)

        for index, movie in enumerate(all_movies, 1):
            if not movie.path:
                continue
            if index % 20 == 0:
                await asyncio.sleep(0.1)
            await clean_radarr_nfo(movie.path)

        logger.info("Radarr ({}) NFO 清洗完成。", radarr_client.server_name)
    except Exception as e:
        logger.exception("Radarr NFO 清洗任务异常: {}", e)
