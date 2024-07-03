'''
剧集/动画/电影 搜索
'''
import logging
from datetime import datetime, timedelta
import asyncio
import aiohttp
from telethon import events, Button
from telegram import client
from loadconfig import init_config
from . import sonarr, radarr

config = init_config()
next_runtime = None
metadataInfo = {'type': None, 'info': None}

@client.on(events.CallbackQuery(data=r'request'))
async def search(event):
    '''
    发送搜索按钮
    '''
    keyboard = [
            Button.inline('电影', data='movie_search'),
            Button.inline('剧集', data='tv_search'),
            Button.inline('动画', data='anime_search'),
    ]
    message = None
    try:
        message = await event.respond('请选择搜索的类型', buttons=keyboard)
    except ImportError as e:
        logging.error(e)
    finally:
        await asyncio.sleep(10)
        await event.delete()
        if message is not None:
            await message.delete()
        raise events.StopPropagation

@client.on(events.CallbackQuery(pattern=r'.*_search$'))
async def reuqest_search(event):
    '''
    识别后缀为_search 的按钮, 并发送搜索提示
    '''
    global metadataInfo, next_runtime
    if next_runtime is not None and datetime.now() < next_runtime:
        delta = next_runtime - datetime.now()
        minutes, seconds = divmod(delta.total_seconds(), 60)
        await event.answer(f"请 {int(minutes)} 分 {int(seconds)} 秒后再试")
        return
    metadataInfo_old = metadataInfo
    s = event.data.decode().split('_')[0]
    async with client.conversation(event.chat_id, timeout=60) as conv:
        await conv.send_message(f'请输入{" tvdbId" if s == "anime" or s == "tv" else "电影的 tmdbId"}')
        try:
            reply_message = await conv.get_response()
            if reply_message.text.isdigit():
                if s == 'tv' or s == 'anime':
                    seriesInfo = await sonarr.get_series_info(reply_message.text, s)
                    if seriesInfo is None or not seriesInfo:
                        seriesInfo = await sonarr.series_lookup(reply_message.text, s)
                        if seriesInfo is None or not seriesInfo:
                            await event.reply(f"未找到该{'剧集' if s == 'tv' else '动画'}, 请检查 tvdbId 是否正确")
                        else:
                            await send_info(event, seriesInfo[0], _class='tv' if s == 'tv' else 'anime')
                            metadataInfo = {'type': 'tv' if s == 'tv' else 'anime', 'info': seriesInfo[0]}
                    else:
                        await event.reply("已在队列中, 请勿重复添加")
                elif s == 'movie':
                    movieInfo = await radarr.get_movie_info(reply_message.text)
                    if movieInfo is None or not movieInfo:
                        movieInfo = await radarr.movie_lookup(reply_message.text)
                        if movieInfo is None or not movieInfo:
                            await event.reply("未找到该电影, 请检查 tmdbId 是否正确")
                        else:
                            await send_info(event, movieInfo, _class='movie')
                            metadataInfo = {'type': 'movie', 'info': movieInfo}
                    else:
                        await event.reply("已在队列中, 请勿重复添加")
            else:
                await conv.send_message('格式错误, 请重新输入')
        except asyncio.TimeoutError:
            await conv.send_message('超时取消')
        except ImportError as e:
            logging.error("search error: %s", e)
            await conv.send_message('处理文件时发生错误')
        finally:
            if metadataInfo_old != metadataInfo:
                next_runtime = datetime.now() + timedelta(minutes=5)

async def get_country(imdbId):
    '''
    获取剧集/动画/电影出产国
    '''
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout, headers={'Accept': '*/*'}) as session:
            async with session.get(f"https://www.omdbapi.com/?i={imdbId}&plot=full&apikey={config.other.OMDBApiKey}") as resp:
                if resp.status == 200:
                    country =  (await resp.json()).get('Country', '').split(', ')[0]
                    image = (await resp.json()).get('Poster', '')
                    if country in ['Albania', 'Andorra', 'Anguilla', 'Antigua and Barbuda', 'Aruba', 'Austria', 'Bahamas', 'Barbados',
                                   'Belarus', 'Belgium', 'Belize', 'Bermuda', 'Bosnia and Herzegovina', 'Bulgaria', 'Canada',
                                   'Cayman Islands', 'Costa Rica', 'Croatia', 'Cuba', 'Czech Republic', 'Denmark', 'Dominica',
                                   'Dominican Republic', 'El Salvador', 'Estonia', 'Faroe Islands', 'Finland', 'France', 'Germany',
                                   'Gibraltar', 'Greece', 'Greenland', 'Grenada', 'Guadeloupe', 'Guatemala', 'Haiti', 'Vatican City',
                                   'Honduras', 'Hungary', 'Iceland', 'Ireland', 'Italy', 'Jamaica', 'Latvia', 'Liechtenstein',
                                   'Lithuania', 'Luxembourg', 'Malta', 'Martinique', 'Mexico', 'Moldova', 'Monaco', 'Montenegro',
                                   'Montserrat', 'Netherlands', 'Nicaragua', 'North Macedonia', 'Norway', 'Panama', 'Poland',
                                   'Portugal', 'Romania', 'Russia', 'San Marino', 'Serbia', 'Slovakia', 'Slovenia', 'Spain',
                                   'Sweden', 'Switzerland', 'Trinidad and Tobago', 'Turkey', 'Turks and Caicos Islands', 'Ukraine',
                                   'United Kingdom', 'United States', 'British Virgin Islands', 'U.S. Virgin Islands', 'Puerto Rico',
                                   'Saint Kitts and Nevis', 'Saint Lucia', 'Saint Vincent and the Grenadines']:
                        category = '欧美'
                    elif country in ['China', 'Honk Kong', 'Hong Kong Special Administrative Region', 'Macao',
                                     'Macao Special Administrative Region', 'Taiwan']:
                        category = '国产'
                    elif country in ['Japan', 'North Korea', 'South Korea', 'Vietnam', 'Korea']:
                        category = '日韩'
                    else:
                        category = '其它'
                    return country, category, image
                else:
                    logging.info("Error looking up movie: %s", resp.status)
                    return None, None, None
    except ImportError as e:
        logging.error("Error looking up movie: %s", e)
        return None, None, None

async def send_info(event, info, _class):
    '''
    发送剧集/动画/电影信息到 Telegram'''
    try:
        country, category, image = await get_country(info['imdbId'])

        message = f'''
<h1><b>{info['title']}</b> ({info['year']})<h1>\n\n
<b>国家:</b> {country}\n
<b>推荐分区:</b> {category}\n
<b>简介:</b> {info['overview']}\n
<b>类型:</b> {', '.join(info['genres'])}\n
<b>语言:</b> {info['originalLanguage']['name']}\n
<b>时长:</b> {info['runtime']}\n
<h2><a href="https://www.imdb.com/title/{info['imdbId']}">IMDB</a>\t<a href="{f"https://www.themoviedb.org/movie/{info['tmdbId']}" if _class == 'movie' else f"http://www.thetvdb.com/?tab=series&id={info['tvdbId']}"}">{"TMDB" if _class == 'movie' else "TVDB"}</a></h2>\n
'''

        buttons_movie = [
            [
                Button.inline('国产(含港澳台)', b"movie_zh"),
                Button.inline('欧美', b"movie_euus"),
                Button.inline('日韩', b"movie_jak")
            ],
            [
                Button.inline('其它', b"movie_other"),
                Button.inline('动画', b"movie_anime")
            ]
        ]
        buttons_tv = [
            [
                Button.inline('国产(含港澳台)', b"tv_zh"),
                Button.inline('欧美', b"tv_euus"),
                Button.inline('日韩', b"tv_jak")
            ],
            [
                Button.inline('记录片', b"tv_doc"),
                Button.inline('其它', b"tv_other")
            ]
        ]
        buttons_anime = [
            [
                Button.inline('确认', b"anime")
            ]
        ]

        if image is None:
            image = 'https://artworks.thetvdb.com/banners/images/missing/movie.jpg'
        # for i in info['images']:
        #     if i['coverType'] == 'poster':
        #         image = i['remoteUrl'] if 'remoteUrl' in i else i['url']
        #     elif i['coverType'] == 'fanart':
        #         image = i['remoteUrl'] if 'remoteUrl' in i else i['url']

        # async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        #     async with session.get(image) as resp:
        #         if resp.status == 200:
        #             image = io.BytesIO(await resp.read())
        #         else:
        #             image = 'https://artworks.thetvdb.com/banners/images/missing/movie.jpg'

        if _class == 'movie':
            await client.send_message(event.chat_id, message, buttons=buttons_movie, parse_mode='html', file=image)
        elif _class == 'tv':
            await client.send_message(event.chat_id, message, buttons=buttons_tv, parse_mode='html', file=image)
        elif _class == 'anime':
            await client.send_message(event.chat_id, message, buttons=buttons_anime, parse_mode='html', file=image)
        elif _class == 'addtrue':
            user = await client.get_entity(event.sender_id)
            username = user.first_name + ' ' + user.last_name if user.last_name else user.first_name
            message += f'\n<a herf="tg://user?id={event.sender_id}">求片人{username}</a>'
            channel = await client.get_input_entity(config.telegram.requiredChannel)
            await client.send_message(channel, message, parse_mode='html', file=image)
        else:
            await client.send_message(event.chat_id, message , parse_mode='html', file=image)
    except ImportError as e:
        logging.error("Error sending message: %s", e)
        await event.reply(f"发送错误: {e}")

@client.on(events.CallbackQuery(pattern=r'^(movie_|tv_).*$|^anime$'))
async def add_search(event):
    '''
    添加剧集/动画/电影到 Radarr/Sonarr
    '''
    global metadataInfo
    try:
        category = event.data.decode()
        if category == 'anime' and metadataInfo['type'] == 'anime':
            info = await sonarr.add_series(metadataInfo['info'], '/mnt/remote/Anime/TV', 'anime')
        elif category == 'tv_zh' and metadataInfo['type'] == 'tv':
            info = await sonarr.add_series(metadataInfo['info'], '/mnt/remote/TV/China', 'standard')
        elif category == 'tv_euus' and metadataInfo['type'] == 'tv':
            info = await sonarr.add_series(metadataInfo['info'], '/mnt/remote/TV/EA', 'standard')
        elif category == 'tv_jak' and metadataInfo['type'] == 'tv':
            info = await sonarr.add_series(metadataInfo['info'], '/mnt/remote/TV/JK', 'standard')
        elif category == 'tv_doc' and metadataInfo['type'] == 'tv':
            info = await sonarr.add_series(metadataInfo['info'], '/mnt/remote/Documentary', 'standard')
        elif category == 'tv_other' and metadataInfo['type'] == 'tv':
            info = await sonarr.add_series(metadataInfo['info'], '/mnt/remote/TV/Others', 'standard')
        elif category == 'movie_zh' and metadataInfo['type'] == 'movie':
            info = await radarr.add_movie(metadataInfo['info'], '/mnt/remote/Movie/China')
        elif category == 'movie_euus' and metadataInfo['type'] == 'movie':
            info = await radarr.add_movie(metadataInfo['info'], '/mnt/remote/Movie/EA')
        elif category == 'movie_jak' and metadataInfo['type'] == 'movie':
            info = await radarr.add_movie(metadataInfo['info'], '/mnt/remote/Movie/JK')
        elif category == 'movie_other' and metadataInfo['type'] == 'movie':
            info = await radarr.add_movie(metadataInfo['info'], '/mnt/remote/Movie/Others')
        elif category == 'movie_anime' and metadataInfo['type'] == 'movie':
            info = await radarr.add_movie(metadataInfo['info'], '/mnt/remote/Anime/Movie')
        else:
            info = None

        if info is not None:
            await event.reply("添加成功")
            await send_info(event, info, _class='addtrue')
        else:
            await event.reply("添加失败")
    except ImportError as e:
        logging.error("Error adding movie: %s", e)
        await event.reply(f"添加失败: {e}")
