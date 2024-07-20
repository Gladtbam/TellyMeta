'''
按配置选择导入 Jellyfin API 和 Emby API
'''
__all__ = ['media_api']
from loadconfig import init_config
config = init_config()

if config.other.paltform == 'emby':
    from . import emby_api as media_api
elif config.other.paltform == 'jellyfin':
    from . import jellyfin_api as media_api
else:
    raise ValueError('未知的平台')