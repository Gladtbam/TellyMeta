import logging
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def setup_logging():
    log_file = Path(__file__).parent.parent / 'bot.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore'
    )

    tmdb_api_key: str = ''
    tvdb_api_key: str = ''
    media_server_url: str = ''
    media_api_key: str = ''
    media_server: str = 'emby'  # or 'jellyfin'
    ai_api_key: str = ''
    ai_base_url: str = 'https://api.openai.com/v1'
    ai_model: str = 'gpt-4o-mini'
    qbittorrent_base_url: str = ''
    qbittorrent_username: str = ''
    qbittorrent_password: str = ''
    telegram_api_id: int = 0
    telegram_api_hash: str = ''
    telegram_bot_token: str = ''
    telegram_bot_name: str = ''
    telegram_chat_id: int = 0

@lru_cache
def get_settings() -> Settings:
    return Settings()

genre_mapping = {
    10759: "动作冒险",
    16: "动画",
    35: "喜剧",
    80: "犯罪",
    99: "纪录",
    18: "剧情",
    10751: "家庭",
    10762: "儿童",
    9648: "悬疑",
    10763: "新闻",
    10764: "真人秀",
    10765: "Sci-Fi & Fantasy",
    10766: "肥皂剧",
    10767: "脱口秀",
    10768: "War & Politics",
    37: "西部",
    28: "动作",
    12: "冒险",
    14: "奇幻",
    36: "历史",
    27: "恐怖",
    10402: "音乐",
    10749: "爱情",
    878: "科幻",
    10770: "电视电影",
    53: "惊悚",
    10752: "战争",
    'Action & Adventure': '动作冒险',
    'Action': '动作',
    'Adventure': '冒险',
    'Animation': '动画',
    'Anime': '动漫',
    'Awards Show': '颁奖典礼',
    'Children': '儿童',
    'Comedy': '喜剧',
    'Crime': '犯罪',
    'Documentary': '纪录片',
    'Drama': '剧情',
    'Family': '家庭',
    'Kids': '儿童',
    'Mystery': '悬疑',
    'News': '新闻',
    'Reality': '真人秀',
    'Sci-Fi & Fantasy': '科幻奇幻',
    'Sci-Fi': '科幻',
    'Fantasy': '奇幻',
    'Soap': '肥皂剧',
    'Talk': '脱口秀',
    'War & Politics': '战争与政治',
    'Western': '西部',
    'Horror': '恐怖',
    'Romance': '爱情',
    'History': '历史',
    'Music': '音乐',
    'Thriller': '惊悚'
}
