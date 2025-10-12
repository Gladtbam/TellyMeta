import logging
import sys
from functools import lru_cache
from pathlib import Path

from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict


def setup_logging():
    settings = get_settings()
    LOGS_DIR = Path(__file__).parent.parent / 'logs'
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger.remove()

    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True
    )

    logger.add(
        LOGS_DIR / "tellymeta_{time:YYYY-MM-DD}.log",
        level=settings.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="00:00",  # 每天生成一个新的日志文件
        retention="7 days",  # 保留最近7天的日志文件
        compression="zip",  # 压缩旧的日志文件
        encoding="utf-8",
        enqueue=True,  # 异步写日志，防止阻塞主线程
        backtrace=True,  # 启用堆栈追踪，方便调试
        diagnose=True  # 启用诊断信息，帮助识别日志记录中的问题
    )

    class InterceptHandler(logging.Handler):
        def emit(self, record):
            # 获取 Loguru 的日志级别
            level = logger.level(record.levelname).name if record.levelname else record.levelno
            # 找到调用日志的堆栈深度
            frame, depth = logging.currentframe(), 2
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logging.getLogger("uvicorn.access").handlers = [InterceptHandler()]
    logging.getLogger("uvicorn").handlers = [InterceptHandler()]
    logging.getLogger("fastapi").handlers = [InterceptHandler()]
    logging.getLogger("httpx").handlers = [InterceptHandler()]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore'
    )

    log_level: str = 'INFO'
    timezone: str = 'Asia/Shanghai'
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
