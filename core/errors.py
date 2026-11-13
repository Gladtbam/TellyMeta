"""统一 HTTP 错误定义与异常处理器"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

_STATUS_CODE_MAP: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


class ApiError(HTTPException):
    """带有机器可识别错误码与面向用户说明的统一 HTTP 业务/授权异常。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        """初始化统一 API 异常。

        Args:
            status_code: 非 2xx HTTP 状态码。
            code: 稳定的机器可识别错误码（如 ``SERVER_NOT_FOUND``）。
            message: 面向前端用户的可读错误说明。
        """
        self.code = code
        self.message = message
        super().__init__(
            status_code=status_code,
            detail={"code": code, "message": message},
        )


def build_error_payload(code: str, message: str) -> dict[str, object]:
    """构造统一的错误响应体。

    Args:
        code: 机器可识别错误码。
        message: 面向用户的错误说明。

    Returns:
        包含 ``success``、``code``、``message`` 与兼容字段 ``detail`` 的字典。
    """
    return {
        "success": False,
        "code": code,
        "message": message,
        "detail": message,
    }


def register_exception_handlers(app: FastAPI) -> None:
    """为 FastAPI 应用注册统一的 HTTPException 与参数校验异常处理器。

    Args:
        app: FastAPI 应用实例。
    """

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_payload(exc.code, exc.message),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        # 兼容直接使用 HTTPException(detail="...") 或 dict detail 的路由与鉴权依赖
        if isinstance(exc.detail, dict):
            code = str(
                exc.detail.get("code")
                or _STATUS_CODE_MAP.get(exc.status_code, f"HTTP_{exc.status_code}")
            )
            message = str(
                exc.detail.get("message") or exc.detail.get("detail") or "请求失败"
            )
        else:
            code = _STATUS_CODE_MAP.get(exc.status_code, f"HTTP_{exc.status_code}")
            message = str(exc.detail) if exc.detail else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_payload(code, message),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.error("URL: {} 请求失败： {}", request.url, exc.errors())
        return JSONResponse(
            status_code=422,
            content=build_error_payload("VALIDATION_ERROR", "请求参数校验失败"),
        )
