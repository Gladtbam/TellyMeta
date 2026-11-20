"""带有显式协商缓存控制头的静态资源挂载处理器"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

if TYPE_CHECKING:
    from starlette.types import Scope


class RevalidatingStaticFiles(StaticFiles):
    """为 Mini App 等静态资源响应注入 ``Cache-Control: no-cache, must-revalidate``，避免客户端 WebView 长期复用过期脚本。"""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """构造静态文件或 304 响应并附带强制再验证缓存头。

        Args:
            full_path: 静态文件磁盘绝对或相对路径。
            stat_result: 文件系统 ``os.stat`` 元数据，用于生成 ``ETag`` 与 ``Last-Modified``。
            scope: 当前请求 ASGI 作用域。
            status_code: 响应状态码，默认 ``200``。

        Returns:
            附带 ``Cache-Control: no-cache, must-revalidate`` 的响应对象。
        """
        response = super().file_response(
            full_path, stat_result, scope, status_code=status_code
        )
        # 确保 Telegram WebView 与代理层每次加载静态页面或脚本前均与服务端校验 ETag
        response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        return response
