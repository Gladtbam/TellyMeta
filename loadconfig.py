'''
定义生成并加载配置文件
'''
import json

class DictToObject:
    '''将字典转换为对象'''
    def __init__(self, dictionary):
        for key, value in dictionary.items():
            if isinstance(value, dict):
                setattr(self, key, DictToObject(value))
            else:
                setattr(self, key, value)
class Config:
    '''定义配置文件数据结构'''
    def __init__(self):
        '''数据库配置'''
        self.dataBase = {
            'dataBaseType': 'mysql',
            'host': 'localhost',
            'port': 3066,
            'user': 'root',
            'password': 'password',
            'databaseName': 'bot'
            }
        '''Telegram配置'''
        self.telegram = {
            'token': None,
            'apiId': None,
            'apiHash': None,
            'botName': None,
            'chatID': None,
            'requiredChannel': None,
            'notifyChannel': None
            }
        '''Emby配置'''
        self.emby = {
            'host': None,
            'apiKey': None
            }
        '''哪吒探针配置'''
        self.probe = {
            'host': None,
            'token': None,
            'id': None
            }
        '''Lidarr配置'''
        self.lidarr = {
            'host': None,
            'apiKey': None
            }
        '''Radarr配置'''
        self.radarr = {
            'host': None,
            'apiKey': None
            }
        '''Sonarr配置'''
        self.sonarr = {
            'host': None,
            'apiKey': None
            }
        '''Sonarr动画配置'''
        self.sonarrAnime = {
            'host': None,
            'apiKey': None
            }
        '''其他配置'''
        self.other = {
            'adminId': [],
            'OMDBApiKey': None,
            'ratio': 1,
            'wiki': None
            }

    def prompt_for_config(self, config, name=None):
        '''提示用户输入配置'''
        print(f"请输入{name}:")
        for key in config:
            if isinstance(config[key], dict):
                self.prompt_for_config(config[key])
            else:
                config[key] = input(f"请输入{key}:")
        return config

    def save_config(self):
        '''保存配置文件'''
        with open('config.json', 'w', encoding="utf-8") as file:
            json.dump(self.__dict__, file, ensure_ascii=False, indent=4)

def load_config():
    '''加载配置文件'''
    try:
        with open('config.json', 'r', encoding="utf-8") as file:
            config_dict = json.load(file)
            return DictToObject(config_dict)
    except FileNotFoundError:
        return None

def init_config():
    '''初始化配置文件, 如果配置文件不存在则创建配置文件并返回配置文件实例, 否则返回配置文件实例'''
    config = load_config()
    if config is None:
        config = Config()
        config.prompt_for_config(config.dataBase, "数据库配置")
        config.prompt_for_config(config.telegram, "Telegram配置")
        config.save_config()
        config = load_config()
    return config
