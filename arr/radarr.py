'''
Radarr API
'''
import logging
import aiohttp
from loadconfig import init_config

config = init_config()
headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'X-Api-Key': config.radarr.ApiKey
    }

async def movie_lookup(tmdbId):
    '''
    从 TheMovieDB 搜索电影
    '''
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{config.radarr.Host}/api/v3/movie/lookup/tmdb?tmdbId={tmdbId}") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.info("Error looking up movie: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error looking up movie: %s", e)
        return None

async def get_movie_info(tmdbId):
    '''
    从 Radarr 获取电影信息
    '''
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{config.radarr.Host}/api/v3/movie?tmdbId={tmdbId}") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.info("Error looking up movie: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error looking up movie: %s", e)
        return None

async def add_movie(movieInfo, rootFolderPath):
    '''
    添加电影到 Radarr
    '''
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(f"{config.radarr.Host}/api/v3/movie", json={
                "title": movieInfo['title'],
                "tmdbId": movieInfo['tmdbId'],
                "year": movieInfo['year'],
                "qualityProfileId": 1,
                "titleSlug": movieInfo['titleSlug'],
                "rootFolderPath": rootFolderPath,
                "monitored": True,
                "minimumAvailability": "released",
                "addOptions": {
                    "searchForMovie": True
                }
            }) as resp:
                if resp.status == 201:
                    return await resp.json()
                else:
                    logging.info("Error adding movie: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error adding movie: %s", e)
        return None
    