import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_data_dir() -> Path:
    """获取数据目录（优先使用配置项，否则按操作系统标准路径解析）"""
    configured = (get_settings().data_dir or "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        )
        return base / "TellyMeta" / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TellyMeta"
    if (
        hasattr(os, "geteuid")
        and os.geteuid() == 0
        and not os.environ.get("XDG_DATA_HOME")
    ):
        return Path("/var/lib/tellymeta")
    xdg_data = Path(
        os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    )
    return xdg_data / "tellymeta"


def get_logs_dir() -> Path:
    """获取日志目录（优先使用配置项，否则按操作系统标准路径解析）"""
    configured = (get_settings().log_dir or "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        )
        return base / "TellyMeta" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "TellyMeta"
    if (
        hasattr(os, "geteuid")
        and os.geteuid() == 0
        and not os.environ.get("XDG_STATE_HOME")
    ):
        return Path("/var/log/tellymeta")
    xdg_state = Path(
        os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    )
    return xdg_state / "tellymeta" / "logs"


def setup_logging():
    settings = get_settings()
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()

    def console_filter(record):
        return "POST /webhook/" not in record["message"]

    logger.add(
        sys.stderr,
        level=settings.log_level.upper(),
        filter=console_filter,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
    )

    logger.add(
        logs_dir / "tellymeta.log",
        level=settings.log_level.upper(),
        filter=console_filter,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="00:00",  # 每天生成一个新的日志文件
        retention="7 days",  # 保留最近7天的日志文件
        # compression="gz",  # 压缩旧的日志文件
        encoding="utf-8",
        enqueue=True,  # 异步写日志，防止阻塞主线程
        backtrace=True,  # 启用堆栈追踪，方便调试
        diagnose=True,  # 启用诊断信息，帮助识别日志记录中的问题
    )

    class InterceptHandler(logging.Handler):
        def emit(self, record):
            # 获取 Loguru 的日志级别
            level = (
                logger.level(record.levelname).name
                if record.levelname
                else record.levelno
            )
            # 找到调用日志的堆栈深度
            frame, depth = logging.currentframe(), 2
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )

    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    loggers_to_silence = [
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "fastapi",
        "httpx2",
    ]
    for log_name in loggers_to_silence:
        _logger = logging.getLogger(log_name)
        _logger.handlers = [InterceptHandler()]
        _logger.propagate = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"
    port: int = 5080
    proxy: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_bot_token: str = ""
    telegram_bot_name: str = ""
    telegram_chat_id: int = 0
    telegram_webapp_url: str = ""
    database_url: str = ""
    data_dir: str = ""
    log_dir: str = ""


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
    "Action & Adventure": "动作冒险",
    "Action": "动作",
    "Adventure": "冒险",
    "Animation": "动画",
    "Anime": "动漫",
    "Awards Show": "颁奖典礼",
    "Children": "儿童",
    "Comedy": "喜剧",
    "Crime": "犯罪",
    "Documentary": "纪录片",
    "Drama": "剧情",
    "Family": "家庭",
    "Kids": "儿童",
    "Mystery": "悬疑",
    "News": "新闻",
    "Reality": "真人秀",
    "Sci-Fi & Fantasy": "科幻奇幻",
    "Sci-Fi": "科幻",
    "Fantasy": "奇幻",
    "Soap": "肥皂剧",
    "Talk": "脱口秀",
    "War & Politics": "战争与政治",
    "Western": "西部",
    "Horror": "恐怖",
    "Romance": "爱情",
    "History": "历史",
    "Music": "音乐",
    "Thriller": "惊悚",
}
