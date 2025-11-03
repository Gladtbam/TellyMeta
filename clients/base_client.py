import asyncio
from abc import ABC, abstractmethod
from typing import TypeVar

import httpx
from loguru import logger
from pydantic import BaseModel, ValidationError

T = TypeVar('T', bound=BaseModel)

class AuthenticatedClientError(Exception):
    """自定义认证失败异常"""
    pass

class BaseClient(ABC):
    """抽象基类，定义了基本的HTTP客户端接口"""
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def close(self):
        """关闭HTTP客户端连接"""
        if self._client:
            await self._client.aclose()

    async def _request(self, method: str, url: str, *,
                       response_model: type[T] | None = None, **kwargs) -> T | httpx.Response | None:
        """发送HTTP请求，子类可以重写此方法以实现特定的认证逻辑
        Args:
            method (str): HTTP方法，如'GET', 'POST', 'DELETE'等。
            url (str): 请求的URL路径。
            response_model (type[T], optional): 用于验证响应数据的Pydantic模型类。
            **kwargs: 传递给httpx请求方法的其他参数，如params, json, headers等。
        Returns:
            httpx.Response | None: 如果请求成功且响应验证通过，返回httpx.Response对象，否则返回None。
        """
        if self._client is None:
            raise RuntimeError("HTTP 客户端未初始化。首先调用 login()。")

        try:
            response = await self._client.request(method, url, **kwargs)
            response.raise_for_status()
            if response_model:
                try:
                    return response_model.model_validate(response.json())
                except ValidationError as e:
                    logger.error("响应验证错误: {}", repr(e.errors()))
                    return None
            return response
        except httpx.HTTPStatusError as e:
            logger.error("HTTP error: {}", e.response.text)
            return None
        except httpx.RequestError as e:
            logger.error("Request error: {}", e)
            return None
        except Exception as e:
            logger.error("Unexpected error: {}", e)
            return None

    async def get(self, url: str, *, response_model: type[T] | None = None, **kwargs) -> T | httpx.Response | None:
        """发送GET请求"""
        return await self._request("GET", url, response_model=response_model, **kwargs)

    async def post(self, url: str, *, response_model: type[T] | None = None, **kwargs) -> T | httpx.Response | None:
        """发送POST请求"""
        return await self._request("POST", url, response_model=response_model, **kwargs)

    async def delete(self, url: str, *, response_model: type[T] | None = None, **kwargs) -> T | httpx.Response | None:
        """发送DELETE请求"""
        return await self._request("DELETE", url, response_model=response_model, **kwargs)

class AuthenticatedClient(BaseClient):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)
        self._is_logged_in = False
        self._login_lock = asyncio.Lock()

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
                logger.info("Successfully logged into {}", self.__class__.__name__)
            except httpx.HTTPStatusError as e:
                logger.error("Login failed: {}", e.response.text)
                raise
            except httpx.RequestError as e:
                logger.error("Request error during login: {}", e)
                raise
            except Exception as e:
                logger.error("Unexpected error during login: {}", e)
                raise

    async def _request(self, method: str, url: str, *,
                       response_model: type[T] | None = None, **kwargs) -> T | httpx.Response | None:
        '''自动登录逻辑'''
        if not self._is_logged_in:
            await self.login()

        # 确保请求头中包含认证信息
        auth_headers = await self._apply_auth()
        if auth_headers:
            kwargs['headers'] = {**kwargs.get('headers', {}), **auth_headers}
        try:
            return await super()._request(method, url, response_model=response_model, **kwargs)
        except AuthenticatedClientError as e:
            logger.error("Authentication error: {}", e)
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in [401, 403]:
                logger.warning("Session expired, re-logging in")
                self._is_logged_in = False
                await self.login()
                auth_headers = await self._apply_auth()
                if auth_headers:
                    kwargs['headers'] = {**kwargs.get('headers', {}), **auth_headers}
                return await super()._request(method, url, response_model=response_model, **kwargs)
            else:
                logger.error("HTTP error: {}", e.response.text)
                raise
        except httpx.RequestError as e:
            logger.error("Request error: {}", e)
            raise
        except Exception as e:
            logger.error("Unexpected error: {}", e)
            raise
