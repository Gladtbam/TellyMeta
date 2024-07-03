'''
Lidarr API
'''
import logging
import aiohttp
from loadconfig import init_config


config = init_config()
headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'X-Api-Key': config.lidarr.apiKey
    }

async def artist_lookup(musicbrainz_id):
    '''
    Lookup artist by MusicBrainz ID
    '''
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{config.lidarr.host}/api/v1/artist/lookup?term=mbid%3A{musicbrainz_id}") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.info("Error looking up artist: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error looking up artist: %s", e)
        return None

async def album_lookup(musicbrainz_id):
    '''
    Lookup album by MusicBrainz ID
    '''
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{config.lidarr.host}/api/v1/album/lookup?term=mbid%3A{musicbrainz_id}") as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.info("Error looking up album: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("Error looking up album: %s", e)
        return None
    