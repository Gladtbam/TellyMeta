'''
Jellyfin API: https://api.jellyfin.org/
'''
import logging
import random
import string
from datetime import timedelta
import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession
from database import only_read_engine
from loadconfig import init_config

config = init_config()

headers = {
    'Content-Type': 'application/json',
    'Authorization': 'MediaBrowser Token=' + config.media.apiKey
}

async def new_user(TelegramName):
    '''新建 Jellyfin 用户'''
    url = f'{config.media.host}/Users/New'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={'Name': TelegramName}, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['Id']
                else:
                    logging.warning("新建 Jellyfin 用户失败: %s", resp.status)
                    return None
    except ImportError as e:
        logging.error("新建 Jellyfin 用户失败: %s", e)
        return None

async def user_policy(jellyfin_id, BlockMeida):
    '''
        设置 Jellyfin 用户权限
        BlockMedia: 禁止用户访问的媒体库
    '''
    payload = {
        "IsAdministrator": False,                   # 是否为管理员
        "IsHidden": True,                           # 用户是否隐藏
        "EnableCollectionManagement": False,        # 是否允许管理集合
        "EnableSubtitleManagement", False,          # 是否允许管理字幕
        "EnableLyricManagement", False,             # 是否允许管理歌词
        "IsDisabled": False,                        # 用户是否被禁用
        # "MaxParentalRating": 0,                     # 最大家长级别
        # "BlockedTags": [],                          # 被阻止的标签列表
        # "AllowedTags": [],                          # 允许的标签列表
        "EnableUserPreferenceAccess": True,         # 是否允许用户访问首选项
        "AccessSchedules": [],                      # 定义用户的访问时间
        "BlockUnratedItems": [],                    # 阻止的未评级项目列表
        "EnableRemoteControlOfOtherUsers": False,   # 是否允许远程控制其他用户
        "EnableSharedDeviceControl": False,         # 是否允许共享设备的控制
        "EnableRemoteAccess": True,                 # 是否允许远程访问
        "EnableLiveTvManagement": False,            # 是否允许管理 Live TV
        "EnableLiveTvAccess": True,                 # 是否允许访问 Live TV
        "EnableMediaPlayback": True,                # 是否允许媒体播放
        "EnableAudioPlaybackTranscoding": False,    # 表示是否允许音频转码
        "EnableVideoPlaybackTranscoding": False,    # 表示是否允许视频转码
        "EnablePlaybackRemuxing": False,            # 是否允许播放重混
        "ForceRemoteSourceTranscoding": False,      # 是否强制远程源转码
        "EnableContentDeletion": False,             # 是否允许删除内容
        "EnableContentDeletionFromFolders": [],     # 允许从指定文件夹删除内容
        "EnableContentDownloading": False,          # 是否允许下载内容
        "EnableSyncTranscoding": False,             # 是否允许同步转码
        "EnableMediaConversion": False,             # 是否允许媒体转换
        "EnabledDevices": [],                       # 启用的设备列表
        "EnableAllDevices": True,                   # 是否启用所有设备
        "EnabledChannels": [],                      # 启用的频道列表
        "EnableAllChannels": True,                  # 是否启用所有频道
        "EnabledFolders": [],                       # 启用的文件夹列表
        "EnableAllFolders": True,                   # 是否启用所有文件夹
        "InvalidLoginAttemptCount": 0,              # 无效登录尝试的次数
        "LoginAttemptsBeforeLockout": 5,            # 锁定之前的登录尝试次数
        "MaxActiveSessions": 3,                     # 最大活动会话数
        "EnablePublicSharing": False,               # 是否允许公共共享
        "BlockedMediaFolders": [BlockMeida],        # 被阻止的媒体文件夹列表
        "BlockedChannels": [],                      # 被阻止的频道列表
        "RemoteClientBitrateLimit": 0,              # 远程客户端的比特率限制
        # "AuthenticationProviderId": "",             # 认证提供者的 ID
        # "PasswordResetProviderId": "",              # 密码重置提供者的 ID
        "SyncPlayAccess": "None"                   # 同步播放访问
    }
    url = f'{config.media.host}/Users/{jellyfin_id}/Policy'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 204:
                    return True
                else:
                    logging.warning("设置 Jellyfin 用户权限失败: %s: %s", resp.status, await resp.json())
                    return False
    except ImportError as e:
        logging.error("设置 Jellyfin 用户权限失败: %s", e)
        return False

async def get_user_info(jellyfin_id):
    '''获取 Jellyfin 用户信息'''
    url = f'{config.media.host}/Users/{jellyfin_id}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    logging.warning("获取 Jellyfin 用户信息失败: %s: %s", resp.status, await resp.json())
                    return None
    except ImportError as e:
        logging.error("获取 Jellyfin 用户信息失败: %s", e)
        return None

async def post_password(jellyfin_id, reset_passwd=False):
    '''重置 Jellyfin 用户密码'''
    passwd = ''.join(random.sample(string.ascii_letters + string.digits, 8))
    payload = {
        "CurrentPassword": "",
        "CurrentPw": "",
        "NewPw": passwd,
        "ResetPassword": reset_passwd
    }
    url = f'{config.media.host}/Users/{jellyfin_id}/Password'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 204:
                    return passwd
                else:
                    logging.warning("重置 Jellyfin 用户密码失败: %s: %s", resp.status, await resp.json())
                    return None
    except ImportError as e:
        logging.error("重置 Jellyfin 用户密码失败: %s", e)
        return None

async def delete_user(jellyfin_id):
    '''删除 Jellyfin 用户'''
    url = f'{config.media.host}/Users/{jellyfin_id}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers) as resp:
                if resp.status == 204:
                    return True
                else:
                    logging.warning("删除 Jellyfin 用户失败: %s: %s", resp.status, await resp.json())
                    return False
    except ImportError as e:
        logging.error("删除 Jellyfin 用户失败: %s", e)
        return False

async def ban_user(jellyfin_ids):
    '''禁用 Jellyfin 用户'''
    try:
        async with aiohttp.ClientSession() as session:
            for jellyfin_id in jellyfin_ids:
                url = f'{config.media.host}/Users/{jellyfin_id}/Policy'
                async with session.post(url, json={"IsDisabled": True}, headers=headers) as resp:
                    if resp.status != 204:
                        logging.warning("禁用 Jellyfin 用户 %s 失败: %s: %s", jellyfin_id, resp.status, await resp.json())
                        return False
            return True
    except ImportError as e:
        logging.error("禁用 Jellyfin 用户失败: %s", e)
        return False

async def delete_ban_user(jellyfin_ids):
    '''删除已禁用的 Jellyfin 用户'''
    try:
        async with aiohttp.ClientSession() as session:
            for jellyfin_id in jellyfin_ids:
                url = f'{config.media.host}/Users/{jellyfin_id}'
                async with session.delete(url, headers=headers) as resp:
                    if resp.status != 204:
                        logging.warning("删除已禁用的 Jellyfin 用户 %s 失败: %s: %s", jellyfin_id, resp.status, await resp.json())
                        return False
            return True
    except ImportError as e:
        logging.error("删除已禁用的 Jellyfin 用户失败: %s", e)
        return False

async def user_playlist(jellyfin_id, limit_date):
    '''获取 Jellyfin 用户播放记录'''
    limit_date = limit_date - timedelta(days=30)
    async with AsyncSession(only_read_engine) as session:
        async with session.begin():
            try:
                query = f"SELECT SUM(PlayDuration) FROM PlaybackActivity where UserId = '{jellyfin_id}' and DateCreated >= '{limit_date}'"
                total_duration = await session.execute(query)
                total_duration = total_duration.scalar()
                if total_duration is not None:
                    total_ratio = total_duration / 86400
                    return total_ratio
                else:
                    return None
            except ImportError as e:
                logging.error('Error occurred while getting Jellyfin user playlist: %s', e)
                await session.rollback()
                return None

async def session_list():
    '''获取 Jellyfin 用户在线数量'''
    url = f'{config.media.host}/Sessions'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    now_playing = 0
                    for item in data:
                        if item['NowPlayingItem'] in item and item['NowPlayingItem'] is not None:
                            now_playing += 1
                    return now_playing
                else:
                    logging.warning("获取 Jellyfin 用户在线数量失败: %s: %s", resp.status, await resp.json())
                    return None
    except ImportError as e:
        logging.error("获取 Jellyfin 用户在线数量失败: %s", e)
        return None
    
