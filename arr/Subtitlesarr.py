from . import sonarr, radarr
import logging
import asyncio
import aiohttp
from Telegram import client
from LoadConfig import init_config
from telethon import events, types, Button
from telethon.tl.types import PeerUser, PeerChat, PeerChannel
from datetime import datetime, timedelta
import traceback
import os
import zipfile
import shutil

config = init_config()

@client.on(events.CallbackQuery(data='subtitle'))
async def request_subtitle(event):
    keyboard = [
        Button.inline('电影', data='movie_subtitle'),
        Button.inline('剧集', data='tv_subtitle'),
        Button.inline('动画', data='anime_subtitle'),
    ]
    message = None
    try:
        message = await event.respond('请选择上传类型', buttons=keyboard)
    except Exception as e:
        logging.error(traceback.format_exc())
    finally:
        await asyncio.sleep(10)
        await event.delete()
        await message.delete() if message is not None else None
        raise events.StopPropagation

@client.on(events.CallbackQuery(pattern=r'.*_subtitle$'))
async def subtitle(event):
    try:
        _calss = event.data.decode().split('_')[0]
        async with client.conversation(event.chat_id, timeout=60) as conv:
            await conv.send_message('请按 wiki 操作将字幕压缩成 zip 文件后发送。')
            try:
                reply_message = await conv.get_response()
                if reply_message and reply_message.file:
                    if reply_message.file.name.endswith('.zip'):
                        file_path = await reply_message.download_media(file='/tmp')
                        with zipfile.ZipFile(file_path, 'r') as zip_ref:
                            dbId = file_path.split('/')[-1].split('.')[0]
                            # subtitle_names = zip_ref.namelist()
                            subtitle_names = [name for name in zip_ref.namelist() if not name.endswith('/')]
                            zip_ref.extractall(path='/tmp')
                        os.remove(file_path)
                        await analyse_subtitle(_calss, dbId, subtitle_names, event)
                        shutil.rmtree('/tmp/' + dbId)
                    else:
                        await conv.send_message('请发送一个 zip 文件')
                else:
                    await conv.send_message('请发送一个文件')
            except asyncio.TimeoutError:
                await conv.send_message('超时未收到文件')
    except asyncio.TimeoutError:
        await event.respond('超时未收到文件')
    except Exception as e:
        logging.error(traceback.format_exc())
        await event.respond('处理文件时发生错误')

async def analyse_subtitle(_calss, dbId, subtitle_names, event):
    try:
        if _calss == 'movie':
            Info = await radarr.GetMovieInfo(dbId)
        elif _calss == 'tv' or _calss == 'anime':
            Info = await sonarr.GetSeriesInfo(dbId, _calss)
        # elif _calss == 'anime':
        #     Info = await sonarr.GetAnimeInfo(dbId)
        else:
            Info = None
            await event.respond('未知的类型')
            
        if Info is not None and Info:
            message = await client.send_message(event.sender_id,'正在处理字幕文件...')
            if _calss == 'movie':
                for subtitle_name in subtitle_names:
                    subtitle = f'/tmp/{subtitle_name}'
                    subtitle_suffix = subtitle_name[subtitle_name.rindex('.')+1:]
                    language = subtitle_name[subtitle_name.index('.')+1:subtitle_name.rindex('.')]
                    movieFilePath = Info[0]['movieFile']['path'].rpartition('.')[0]
                    destination = f"{movieFilePath}.{language}.{subtitle_suffix}"
                    shutil.move(subtitle, destination)
                    if os.path.exists(destination):
                        await client.edit_message(message, message.text + f'\n{subtitle_name} ...... ok')
                    else:
                        await client.edit_message(message, message.text + f'\n{subtitle_name} ...... fail')
            elif _calss == 'tv' or _calss == 'anime':
                seriesInfo = await sonarr.GetEpisodeInfo(Info[0]['id'], _calss)
                if seriesInfo is not None and seriesInfo:
                    # episodesInfo = [{'id': episode['id'], 'seasonNumber': episode['seasonNumber'], 'episodeNumber': episode['episodeNumber']} for episode in seriesInfo]
                    episodesInfo = {}
                    for episode in seriesInfo:
                        if episode['seasonNumber'] not in episodesInfo:
                            episodesInfo[episode['seasonNumber']] = {}
                        episodesInfo[episode['seasonNumber']][episode['episodeNumber']] = episode['id']
                    print(episodesInfo)
                    for subtitle_name in subtitle_names:
                        subtitle = f'/tmp/{subtitle_name}'
                        subtitle_suffix = subtitle_name[subtitle_name.rindex('.')+1:]
                        season = int(subtitle_name[subtitle_name.index('S')+1:subtitle_name.index('E')])
                        episode = int(subtitle_name[subtitle_name.index('E')+1:subtitle_name.index('.')])
                        language = subtitle_name[subtitle_name.index('.')+1:subtitle_name.rindex('.')]
                        episodeId = episodesInfo.get(season, {}).get(episode, None)
                        if episodeId is not None:
                            episodeInfo = await sonarr.GetEpisodeId(episodeId, _calss)
                            if episodeInfo and episodeInfo['seasonNumber'] == season and episodeInfo['episodeNumber'] == episode:
                                episodeFilePath = episodeInfo['episodeFile']['path'].rpartition('.')[0]
                                destination = f"{episodeFilePath}.{language}.{subtitle_suffix}"
                                shutil.move(subtitle, destination)
                                if os.path.exists(destination):
                                    message = await client.edit_message(message, message.text + f'\n{subtitle_name} ...... ok')
                                else:
                                    message = await client.edit_message(message, message.text + f'\n{subtitle_name} ...... fail0')
                            else:
                                message = await client.edit_message(message, f'{subtitle_name} ...... fail1')
                else:
                    await client.edit_message(message, message.text + f'\n未找到对应的{"剧集" if _calss == "tv" else "动画"}')
            else:
                await client.edit_message(message, message.text + '\n未知的类型')
        else:
            await event.respond('未找到对应的电影/剧集/动画')
    except Exception as e:
        logging.error(traceback.format_exc())
        await event.respond('处理字幕文件时发生错误, 请检查文件名是否正确')
    finally:
        await asyncio.sleep(10)
        await event.delete()
        # raise events.StopPropagation
        
