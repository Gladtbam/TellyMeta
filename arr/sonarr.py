'''
Sonarr API 
'''
import logging
import aiohttp
from loadconfig import init_config

config = init_config()
headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'X-Api-Key': config.sonarr.apiKey
    }
anime_headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'X-Api-Key': config.sonarrAnime.apiKey
    }

async def series_lookup(tvdbId, seriesType):
    '''
    从 TheTVDB 搜索剧集/动画
    '''
    try:
        if seriesType == "anime":
            seriesHeaders = anime_headers
            seriesHost = config.sonarrAnime.host
        elif seriesType == "tv":
            seriesHeaders = headers
            seriesHost = config.sonarr.host
        async with aiohttp.ClientSession(headers=seriesHeaders) as session:
            async with session.get(f"{seriesHost}/api/v3/series/lookup?term=tvdb%3A{tvdbId}") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.info("Error looking up series: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error looking up series: %s", e)
        return None

async def get_series_info(tvdbId, seriesType):
    '''
    从 Sonarr 获取剧集/动画信息
    '''
    try:
        if seriesType == "anime":
            seriesHeaders = anime_headers
            seriesHost = config.sonarrAnime.host
        elif seriesType == "tv":
            seriesHeaders = headers
            seriesHost = config.sonarr.host
        async with aiohttp.ClientSession(headers=seriesHeaders) as session:
            async with session.get(f"{seriesHost}/api/v3/series?tvdbId={tvdbId}&includeSeasonImages=true") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.info("Error looking up series: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error looking up series: %s", e)
        return None

async def get_episode_info(seriesId, seriesType):
    '''
    从 Sonarr 获取剧集/动画集数的信息
    '''
    try:
        if seriesType == "anime":
            seriesHeaders = anime_headers
            seriesHost = config.sonarrAnime.host
        elif seriesType == "tv":
            seriesHeaders = headers
            seriesHost = config.sonarr.host
        async with aiohttp.ClientSession(headers=seriesHeaders) as session:
            async with session.get(f"{seriesHost}/api/v3/episode?seriesId={seriesId}&includeImages=true") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.info("Error looking up series: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error looking up series: %s", e)
        return None

async def get_episode_id(episodeId, seriesType):
    '''
    从 Sonarr 获取剧集/动画集数的 Sonarr ID
    '''
    try:
        if seriesType == "anime":
            seriesHeaders = anime_headers
            seriesHost = config.sonarrAnime.host
        elif seriesType == "tv":
            seriesHeaders = headers
            seriesHost = config.sonarr.host
        async with aiohttp.ClientSession(headers=seriesHeaders) as session:
            async with session.get(f"{seriesHost}/api/v3/episode/{episodeId}") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.info("Error looking up series: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error looking up series: %s", e)
        return None

async def add_series(seriesInfo, rootFolderPath, seriesType):
    '''
    添加剧集/动画到 Sonarr
    '''
    try:
        if seriesType == "anime":
            async with aiohttp.ClientSession(headers=anime_headers) as session:
                async with session.post(f"{config.sonarrAnime.host}/api/v3/series", json={
                    "tvdbId": seriesInfo["tvdbId"],
                    "monitored": True,
                    "qualityProfileId": 1,
                    "seasons": seriesInfo["seasons"],
                    "seasonFolder": True,
                    "rootFolderPath": rootFolderPath,
                    "seriesType": seriesType,
                    "title": seriesInfo["title"],
                    "addOptions": {
                        "ignoreEpisodesWithFiles": True,
                        "ignoreEpisodesWithoutFiles": False,
                        "searchForMissingEpisodes": True
                    }
                    }) as resp:
                    if resp.status == 201:
                        logging.info("Added series: %s", seriesInfo["tvdbId"])
                        return await resp.json()
                    else:
                        logging.info("Error adding series: %s", resp.status)
                        return None
        elif seriesType == "standard":
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(f"{config.sonarr.host}/api/v3/series", json={
                    "tvdbId": seriesInfo["tvdbId"],
                    "monitored": True,
                    "qualityProfileId": 1,
                    "seasons": seriesInfo["seasons"],
                    "seasonFolder": True,
                    "rootFolderPath": rootFolderPath,
                    "seriesType": seriesType,
                    "title": seriesInfo["title"],
                    "addOptions": {
                        "ignoreEpisodesWithFiles": True,
                        "ignoreEpisodesWithoutFiles": False,
                        "searchForMissingEpisodes": True
                    }
                    }) as resp:
                    if resp.status == 201:
                        logging.info("Added series: %s", seriesInfo["tvdbId"])
                        return await resp.json()
                    else:
                        logging.info("Error adding series: %s", resp.status)
                        return None
    except ImportError as e:
        logging.error("Error adding series: %s", e)
        return False
    