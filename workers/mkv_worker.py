import asyncio
import logging
from typing import NoReturn

from workers.mkv_utils import mkv_merge

logger = logging.getLogger(__name__)

async def mkv_merge_task(task_queue: asyncio.Queue) -> NoReturn:
    """异步处理 MKV 合并任务队列"""
    while True:
        episode_path = None
        try:
            episode_path = await task_queue.get()
            logging.info("Processing %s for MKV merge", episode_path)
            await mkv_merge(episode_path)
        except Exception as e:
            logging.error("Error processing MKV merge task: %s", e)
        finally:
            if episode_path is not None:
                task_queue.task_done()
                logging.info("MKV merge task for %s completed", episode_path)
