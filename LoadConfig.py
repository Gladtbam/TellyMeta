'''
定义生成并加载配置文件
'''
from dataclasses import dataclass, field
from typing import Optional
import yaml
from dataclasses_json import dataclass_json

@dataclass_json
@dataclass
class DataBase:
    '''定义数据库配置数据结构'''
    Host: Optional[str] = 'localhost'
    Port: Optional[int] = 3066
    User: Optional[str] = None
    Password: Optional[str] = None
    DatabaseName: Optional[str] = None

@dataclass_json
@dataclass
class Telegram:
    '''定义Telegram配置数据结构'''
    Token: Optional[str] = None
    ApiId: Optional[int] = None
    ApiHash: Optional[str] = None
    BotName: Optional[str] = None
    ChatID: Optional[int] = None
    RequiredChannel: Optional[str] = None
    NotifyChannel: Optional[str] = None

@dataclass_json
@dataclass
class Emby:
    '''定义Emby配置数据结构'''
    Host: Optional[str] = None
    ApiKey: Optional[str] = None

@dataclass_json
@dataclass
class Probe:
    '''定义哪吒探针配置数据结构'''
    Host: Optional[str] = None
    Token: Optional[str] = None
    Id: Optional[str] = None

@dataclass_json
@dataclass
class Lidarr:
    '''定义Lidarr配置数据结构'''
    Host: Optional[str] = None
    ApiKey: Optional[str] = None

@dataclass_json
@dataclass
class Radarr:
    '''定义Radarr配置数据结构'''
    Host: Optional[str] = None
    ApiKey: Optional[str] = None

@dataclass_json
@dataclass
class Sonarr:
    '''定义Sonarr配置数据结构'''
    Host: Optional[str] = None
    ApiKey: Optional[str] = None

@dataclass_json
@dataclass
class SonarrAnime:
    '''定义Sonarr动画配置数据结构'''
    Host: Optional[str] = None
    ApiKey: Optional[str] = None

@dataclass_json
@dataclass
class Other:
    '''定义其他配置数据结构'''
    AdminId: Optional[list] = None
    OMDBApiKey: Optional[str] = None
    Ratio: int = 1
    Wiki: Optional[str] = None

@dataclass_json
@dataclass
class Config:
    '''定义配置文件数据结构'''
    dataBase: DataBase = field(default_factory=DataBase)
    telegram: Telegram = field(default_factory=Telegram)
    emby: Emby = field(default_factory=Emby)
    probe: Probe = field(default_factory=Probe)
    lidarr: Lidarr = field(default_factory=Lidarr)
    radarr: Radarr = field(default_factory=Radarr)
    sonarr: Sonarr = field(default_factory=Sonarr)
    sonarrAnime: SonarrAnime = field(default_factory=SonarrAnime)
    other: Other = field(default_factory=Other)

def load_config():
    '''加载配置文件'''
    try:
        with open('config.yaml', 'r', encoding="utf-8") as file:
            config_dict = yaml.safe_load(file)
            return Config.from_dict(config_dict) # type: ignore
    except FileNotFoundError:
        return None

def save_config(config):
    '''保存配置文件'''
    with open('config.yaml', 'w', encoding="utf-8") as file:
        config_dict = config.to_dict()
        yaml.dump(config_dict, file)
    print("配置文件已保存, 当前代码存在BUG, 请手动检查配置文件是否正确")
    exit(0)

def get_user_input(cls):
    '''获取用户输入'''
    instance = cls()
    for field in cls.__dataclass_fields__.keys():
        value = input(f"请输入 {cls.__name__} 的 {field} 配置：")
        if value:
            setattr(instance, field, value)
    return instance

def init_config():
    '''初始化配置文件, 如果配置文件不存在则创建配置文件并返回配置文件实例, 否则返回配置文件实例'''
    config = load_config()
    if not isinstance(config, Config):
        print("配置文件不存在, 请按照提示填写配置文件")
        config = Config(
            other=get_user_input(Other),
            dataBase=get_user_input(DataBase),
            telegram=get_user_input(Telegram),
            emby=get_user_input(Emby),
            probe=get_user_input(Probe),
            lidarr=get_user_input(Lidarr),
            radarr=get_user_input(Radarr),
            sonarr=get_user_input(Sonarr),
            sonarrAnime=get_user_input(SonarrAnime)
        )
        save_config(config)
    else:
        return config
