import asyncio
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Literal, TypeVar, overload

import httpx2
from loguru import logger
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
T_parser = TypeVar("T_parser")

RequestErrorHook = Callable[[str, int | None, str], Awaitable[None]]
RequestRecoveredHook = Callable[[str], Awaitable[None]]


class RateLimiter:
    """速率限制器"""

    def __init__(self, rate: int, per: float = 1.0):
        self.rate = rate
        self.per = per
        self.allowance = rate
        self.last_check = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: float = 1.0):
        async with self._lock:
            current = monotonic()
            time_passed = current - self.last_check
            self.last_check = current
            self.allowance += time_passed * (self.rate / self.per)

            if self.allowance > self.rate:
                self.allowance = self.rate

            if self.allowance < amount:
                wait_time = (amount - self.allowance) * (self.per / self.rate)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                self.allowance = 0.0
            else:
                self.allowance -= amount


class BaseClient:
    """HTTP 客户端基类，提供通用的请求发送、重试与响应解析能力"""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        on_request_error: RequestErrorHook | None = None,
        on_request_recovered: RequestRecoveredHook | None = None,
    ):
        self._client = client
        self._on_request_error: RequestErrorHook | None = on_request_error
        self._on_request_recovered: RequestRecoveredHook | None = on_request_recovered
        self._has_request_error = False

    def set_request_hooks(
        self,
        *,
        on_request_error: RequestErrorHook | None = None,
        on_request_recovered: RequestRecoveredHook | None = None,
    ) -> None:
        """绑定或替换外部请求异常与恢复回调钩子。

        Args:
            on_request_error: 请求失败后触发的异步回调 ``(operation, status_code, error_message)``。
            on_request_recovered: 故障后首次恢复正常响应时触发的异步回调 ``(operation)``。
        """
        self._on_request_error = on_request_error
        self._on_request_recovered = on_request_recovered

    async def _notify_request_error(
        self,
        *,
        operation: str,
        status_code: int | None,
        error_message: str,
    ) -> None:
        """记录故障态并安全触发错误钩子（吞掉钩子自身异常，避免掩盖原始业务错误）。"""
        self._has_request_error = True
        if self._on_request_error is None:
            return
        try:
            await self._on_request_error(operation, status_code, error_message)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("执行客户端请求错误钩子失败: {}", exc)

    async def _notify_request_recovered(self, *, operation: str) -> None:
        """若此前处于故障态则复位并触发一次恢复钩子。"""
        if not self._has_request_error:
            return
        # 先复位状态再 await，避免并发请求同时成功时重复发射恢复通知
        self._has_request_error = False
        if self._on_request_recovered is None:
            return
        try:
            await self._on_request_recovered(operation)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("执行客户端请求恢复钩子失败: {}", exc)

    async def close(self):
        """关闭HTTP客户端连接"""
        if self._client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        response_model: type[T] | None = None,
        parser: Callable[[Any], T_parser] | None = None,
        raw: bool = False,
        _suppress_auth_error: bool = False,
        **kwargs,
    ) -> httpx2.Response | T | T_parser | None:
        """发送HTTP请求，子类可以重写此方法以实现特定的认证逻辑
        Args:
            method (str): HTTP方法，如'GET', 'POST', 'DELETE'等。
            url (str): 请求的URL路径。
            response_model (type[T], optional): 用于验证响应数据的Pydantic模型类。
            _suppress_auth_error (bool): 为 True 时由子类先行尝试 401/403 重登录后再决定是否触发异常钩子。
            **kwargs: 传递给httpx请求方法的其他参数，如params, json, headers等。
        Returns:
            T | None: 如果请求成功且响应验证通过，返回模型对象，否则返回None。
        """
        if self._client is None:
            raise RuntimeError("HTTP 客户端未初始化。首先调用 login()。")

        # 设置识别程序的 User-Agent
        headers = kwargs.get("headers", {})
        if "User-Agent" not in headers:
            headers["User-Agent"] = "TellyMeta/1.0"
        kwargs["headers"] = headers

        max_retries = 3
        operation = f"{method.upper()} {url}"

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)

                if response.status_code == 403 and (
                    "cloudflare" in response.text.lower()
                    or "just a moment" in response.text.lower()
                ):
                    logger.error(
                        "HTTP 错误 403：请求被 Cloudflare 拦截。这通常是因为站点启用了 WAF 机器人检测或“我在受攻击”模式。"
                    )
                    logger.error("URL: {}", url)
                    logger.error(
                        "提示：请尝试将运行该程序的服务器 IP 加入站点的 Cloudflare 白名单。"
                    )

                response.raise_for_status()
                if response_model and parser:
                    raise ValueError("response_model 和 parser 不能同时使用")

                await self._notify_request_recovered(operation=operation)

                if raw:
                    return response
                if response.status_code == 204 or not response.content:
                    return None

                data = response.json()

                if parser is not None:
                    return parser(data)

                if response_model is not None:
                    return response_model.model_validate(data)
                return None

            except ValidationError as e:
                logger.error("响应验证错误: {}", repr(e.errors()))
                raise
            except httpx2.TimeoutException as e:
                if attempt == max_retries:
                    logger.error("请求超时（已重试{}次）：{}", max_retries, e)
                    await self._notify_request_error(
                        operation=operation,
                        status_code=None,
                        error_message=str(e) or type(e).__name__,
                    )
                    raise
                logger.warning(
                    t"请求超时，正在进行第 {attempt + 1}/{max_retries} 次重试... URL: {url}"
                )
                await asyncio.sleep(1)
            except httpx2.HTTPStatusError as e:
                is_cf_block = e.response.status_code == 403 and (
                    "cloudflare" in e.response.text.lower()
                    or "just a moment" in e.response.text.lower()
                )
                if not is_cf_block:
                    logger.error(
                        "HTTP 错误：{} -{}", e.response.status_code, e.response.text
                    )
                will_relogin = (
                    _suppress_auth_error
                    and e.response.status_code in (401, 403)
                    and not is_cf_block
                )
                if not will_relogin and (
                    e.response.status_code >= 500
                    or e.response.status_code in (401, 403)
                ):
                    await self._notify_request_error(
                        operation=operation,
                        status_code=e.response.status_code,
                        error_message=(
                            f"HTTP {e.response.status_code}: {e.response.text[:300]}"
                        ),
                    )
                raise
            except httpx2.RequestError as e:
                logger.error("请求错误：{}", e)
                await self._notify_request_error(
                    operation=operation,
                    status_code=None,
                    error_message=str(e) or type(e).__name__,
                )
                raise
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("未知错误：{}", e)
                raise

        return None

    @overload
    async def get(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: None = None,
        raw: Literal[False] = False,
        **kwargs,
    ) -> None: ...

    @overload
    async def get(
        self,
        url: str,
        *,
        response_model: type[T],
        parser: None = None,
        raw: Literal[False] = False,
        **kwargs,
    ) -> T | None: ...

    @overload
    async def get(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: Callable[[Any], T_parser],
        raw: Literal[False] = False,
        **kwargs,
    ) -> T_parser | None: ...

    @overload
    async def get(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: None = None,
        raw: Literal[True],
        **kwargs,
    ) -> httpx2.Response: ...

    async def get(
        self,
        url: str,
        *,
        response_model: type[T] | None = None,
        parser: Callable[[Any], T_parser] | None = None,
        raw: bool = False,
        **kwargs,
    ) -> httpx2.Response | T | T_parser | None:
        """发送GET请求"""
        return await self._request(
            "GET", url, response_model=response_model, parser=parser, raw=raw, **kwargs
        )

    @overload
    async def post(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: None = None,
        raw: Literal[False] = False,
        **kwargs,
    ) -> None: ...

    @overload
    async def post(
        self,
        url: str,
        *,
        response_model: type[T],
        parser: None = None,
        raw: Literal[False] = False,
        **kwargs,
    ) -> T | None: ...

    @overload
    async def post(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: Callable[[Any], T_parser],
        raw: Literal[False] = False,
        **kwargs,
    ) -> T_parser | None: ...

    @overload
    async def post(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: None = None,
        raw: Literal[True],
        **kwargs,
    ) -> httpx2.Response: ...

    async def post(
        self,
        url: str,
        *,
        response_model: type[T] | None = None,
        parser: Callable[[Any], T_parser] | None = None,
        raw: bool = False,
        **kwargs,
    ) -> httpx2.Response | T | T_parser | None:
        """发送POST请求"""
        return await self._request(
            "POST", url, response_model=response_model, parser=parser, raw=raw, **kwargs
        )

    @overload
    async def delete(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: None = None,
        raw: Literal[False] = False,
        **kwargs,
    ) -> None: ...

    @overload
    async def delete(
        self,
        url: str,
        *,
        response_model: type[T],
        parser: None = None,
        raw: Literal[False] = False,
        **kwargs,
    ) -> T | None: ...

    @overload
    async def delete(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: Callable[[Any], T_parser],
        raw: Literal[False] = False,
        **kwargs,
    ) -> T_parser | None: ...

    @overload
    async def delete(
        self,
        url: str,
        *,
        response_model: None = None,
        parser: None = None,
        raw: Literal[True],
        **kwargs,
    ) -> httpx2.Response: ...

    async def delete(
        self,
        url: str,
        *,
        response_model: type[T] | None = None,
        parser: Callable[[Any], T_parser] | None = None,
        raw: bool = False,
        **kwargs,
    ) -> httpx2.Response | T | T_parser | None:
        """发送DELETE请求"""
        return await self._request(
            "DELETE",
            url,
            response_model=response_model,
            parser=parser,
            raw=raw,
            **kwargs,
        )


class AuthenticatedClient(BaseClient):
    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        on_request_error: RequestErrorHook | None = None,
        on_request_recovered: RequestRecoveredHook | None = None,
    ):
        super().__init__(
            client,
            on_request_error=on_request_error,
            on_request_recovered=on_request_recovered,
        )
        self._is_logged_in = False
        self._login_lock = asyncio.Lock()
        self._max_retries = 1  # 最大重试次数

    @abstractmethod
    async def _login(self):
        """子类必须实现登录逻辑"""
        raise NotImplementedError("子类必须实现 _login 方法")

    @abstractmethod
    async def _apply_auth(self):
        """子类可以重写此方法以应用特定的认证头"""
        raise NotImplementedError("子类必须实现 _apply_auth 方法")

    async def login(self):
        async with self._login_lock:
            # 确保只有一个协程在执行登录操作
            if self._is_logged_in:
                return
            try:
                await self._login()
                self._is_logged_in = True
                logger.info("已成功登录 {}", self.__class__.__name__)
            except httpx2.HTTPStatusError as e:
                logger.error("登录失败： {}", e.response.text)
                raise
            except httpx2.RequestError as e:
                logger.error("登录期间请求错误：{}", e)
                raise
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("登录期间出现意外错误：{}", e)
                raise

    async def _request(
        self,
        method: str,
        url: str,
        *,
        response_model: type[T] | None = None,
        parser: Callable[[Any], T_parser] | None = None,
        raw: bool = False,
        _retry: int = 0,
        **kwargs,
    ) -> httpx2.Response | T | T_parser | None:
        """自动登录逻辑"""
        if not self._is_logged_in:
            await self.login()

        # 确保请求头中包含认证信息
        auth_headers = await self._apply_auth()
        if auth_headers:
            kwargs["headers"] = {**kwargs.get("headers", {}), **auth_headers}
        try:
            return await super()._request(
                method,
                url,
                response_model=response_model,
                parser=parser,
                raw=raw,
                _suppress_auth_error=(_retry < self._max_retries),
                **kwargs,
            )
        except httpx2.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                # 如果是 Cloudflare 拦截，则不应尝试重新登录
                if (
                    "cloudflare" in e.response.text.lower()
                    or "just a moment" in e.response.text.lower()
                ):
                    raise

                if _retry >= self._max_retries:
                    logger.error("达到最大重试次数，无法重新登录")
                    raise

                logger.warning("会话已过期，重新登录")
                self._is_logged_in = False
                await self.login()

                auth_headers = await self._apply_auth()
                if auth_headers:
                    kwargs["headers"] = {**kwargs.get("headers", {}), **auth_headers}
                return await self._request(
                    method,
                    url,
                    response_model=response_model,
                    parser=parser,
                    raw=raw,
                    _retry=_retry + 1,
                    **kwargs,
                )

            raise
