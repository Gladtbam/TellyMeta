'''
Emby API
'''
import logging
import random
import string
import aiohttp
from loadconfig import init_config

headers = {
    'Content-Type': 'application/json'
}

config = init_config()

async def new_user(TelegramName):
    '''新建 Emby 用户'''
    url = f'{config.emby.host}/emby/Users/New?api_key={config.emby.apiKey}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={'Name': TelegramName}, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()  # 获取返回的json
                    return data['Id']  # 返回json数据
                else:
                    return None
    except ImportError as e:
        logging.error("新建 Emby 用户失败: %s", e)
        return None

async def user_policy(EmbyId, BlockMeida):
    '''设置 Emby 用户权限'''
    data = {
        "IsAdministrator": False,                   # 是否为管理员
        "IsHidden": True,                           # 用户是否隐藏
        "IsHiddenRemotely": True,                   # 是否在远程访问时隐藏
        "IsHiddenFromUnusedDevices": False,         # 是否在未使用的设备上隐藏
        "IsDisabled": False,                        # 用户是否被禁用
        # "MaxParentalRating": 0,                     # 最大家长级别
        "BlockedTags": [],                          # 被阻止的标签列表
        "IsTagBlockingModeInclusive": False,        # 是否以标签阻止模式进行阻止
        "IncludeTags": [],                          # 包含的标签列表
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
        "EnablePlaybackRemuxing": False,            # 是否允许媒体复用
        "EnableContentDeletion": False,             # 是否允许删除内容
        "EnableContentDeletionFromFolders": [],     # 允许从指定文件夹删除内容
        "EnableContentDownloading": False,          # 是否允许下载内容
        "EnableSubtitleDownloading": False,         # 是否允许下载字幕
        "EnableSubtitleManagement": False,          # 是否允许管理字幕
        "EnableSyncTranscoding": False,             # 是否允许同步转码
        "EnableMediaConversion": False,             # 是否允许媒体转换
        "EnabledChannels": [],                      # 允许访问的频道列表
        "EnableAllChannels": True,                  # 是否允许访问所有频道
        "EnabledFolders": [],                       # 允许访问的文件夹列表
        "EnableAllFolders": True,                   # 是否允许访问所有文件夹
        "InvalidLoginAttemptCount": 0,              # 无效登录尝试的次数
        "EnablePublicSharing": False,               # 是否允许公开共享
        "BlockedMediaFolders": [BlockMeida],                  # 被阻止的媒体文件夹列表
        "RemoteClientBitrateLimit": 0,              # 远程客户端的比特率限制
        # "AuthenticationProviderId": "string",     # 认证提供者的 ID
        "ExcludedSubFolders": [],                   # 排除的子文件夹列表
        "SimultaneousStreamLimit": 3,               # 同时流式传输的限制
        "EnabledDevices": [],                       # 允许访问的设备列表
        "EnableAllDevices": True                    # 是否允许访问所有设备
    }
    url = f"{config.emby.host}/emby/Users/{EmbyId}/Policy?api_key={config.emby.apiKey}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers) as resp:
                _bool = resp.status == 200
                return _bool
                # if resp.status == 200:
                #     return True
                # else:
                #     return False
    except ImportError as e:
        logging.error("设置 Emby 用户权限失败: %s", e)
        return False

async def get_user_info(EmbyId):
    '''获取 Emby 用户信息'''
    url = f"{config.emby.host}/emby/Users/{EmbyId}?api_key={config.emby.apiKey}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    return None
    except ImportError as e:
        logging.error("获取 Emby 用户信息失败: %s", e)
        return None

async def post_password(EmbyId, ResetPassword=False):
    '''创建/重置 Emby 用户密码'''
    Pw = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(12))
    data = {
        "Id": EmbyId,
        "CurrentPw": "",
        "NewPw": Pw,
        "ResetPassword": ResetPassword
    }
    url = f"{config.emby.host}/emby/Users/{EmbyId}/Password?api_key={config.emby.apiKey}"
    try:
        async with aiohttp.ClientSession() as session:
            if ResetPassword is True:
                async with session.post(url, json={"ResetPassword": ResetPassword}, headers=headers) as resp:
                    _bool = resp.status in [200, 204]
                    return _bool
                    # if resp.status in [200, 204]:
                    #     return True
                    # else:
                    #     return False
            else:
                async with session.post(url, json=data, headers=headers) as resp:
                    if resp.status in [200, 204]:
                        return Pw
                    else:
                        return None
    except ImportError as e:
        logging.error("创建/重置 Emby 用户密码失败: %s", e)
        return None

async def delete_emby_user(EmbyId):
    '''删除 Emby 用户'''
    url = f"{config.emby.host}/emby/Users/{EmbyId}?api_key={config.emby.apiKey}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers) as resp:
                _bool = resp.status == 200
                return _bool
                # if resp.status == 200:
                #     return True
                # else:
                #     return False
    except ImportError as e:
        logging.error("删除 Emby 用户失败: %s", e)
        return False

async def ban_emby_user(EmbyIds):
    '''禁用 Emby 用户'''
    try:
        async with aiohttp.ClientSession() as session:
            for EmbyId in EmbyIds:
                url = f"{config.emby.host}/emby/Users/{EmbyId}/Policy?api_key={config.emby.apiKey}"
                async with session.post(url, json={"IsDisabled": True}, headers=headers) as resp:
                    if resp.status != 200:
                        return False
            return True
    except ImportError as e:
        logging.error("禁用 Emby 用户失败: %s", e)
        return False

async def delete_ban_user(EmbyIds):
    '''删除已禁用 Emby 用户'''
    try:
        async with aiohttp.ClientSession() as session:
            for EmbyId in EmbyIds:
                url = f"{config.emby.host}/emby/Users/{EmbyId}?api_key={config.emby.apiKey}"
                async with session.delete(url, headers=headers) as resp:
                    if resp.status != 200:
                        return False
            return True
    except ImportError as e:
        logging.error("删除已禁用 Emby 用户失败: %s", e)
        return False

async def user_playlist(EmbyId, LimitDate):
    '''获取 Emby 用户播放记录'''
    url = f"{config.emby.host}/emby/user_usage_stats/UserPlaylist?user_id={EmbyId}&aggregate_data=false&days=30&end_date={LimitDate}&api_key={config.emby.apiKey}"
    total_duration = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data:
                        total_duration += int(item['duration'])
                    total_ratio = total_duration / 86400
                    return total_ratio
                else:
                    return None
    except ImportError as e:
        logging.error("获取 Emby 用户播放记录失败: %s", e)
        return None

async def session_list():
    '''获取 Emby 用户在线数量'''
    url = f"{config.emby.host}/emby/user_usage_stats/session_list?api_key={config.emby.apiKey}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    now_playing = 0
                    for item in data:
                        if 'NowPlayingItem' in item:
                            now_playing += 1
                    return now_playing
                else:
                    return None
    except ImportError as e:
        logging.error("获取 Emby 用户在线数量失败: %s", e)
        return None

# 基于哪吒探针
async def get_server_info():
    '''获取服务器信息'''
    url = f"{config.probe.host}/api/v1/server/details?id={config.probe.Id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={'Authorization': config.probe.token}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    return None
    except ImportError as e:
        logging.error("获取服务器信息失败: %s", e)
        return None
