import asyncio
from collections.abc import Coroutine
from typing import Any

# 保存后台任务强引用，防止任务在完成前被垃圾回收
_background_tasks: set[asyncio.Task[Any]] = set()


def create_background_task[T](coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """创建并跟踪后台异步任务，在任务完成后自动清理引用。

    Args:
        coro: 需要后台执行的协程对象。

    Returns:
        已绑定完成清理回调的 asyncio.Task 实例。
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
