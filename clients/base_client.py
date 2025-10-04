import asyncio
import logging
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)

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

    async def _request(self, method: str, url: str, **kwargs):
        """发送HTTP请求，子类可以重写此方法以实现特定的认证逻辑"""
        if self._client is None:
            raise RuntimeError("HTTP client is not initialized. Call login() first.")

        try:
            response = await self._client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            logging.error("HTTP error: %s", e.response.text)
            raise
        except httpx.RequestError as e:
            logging.error("Request error: %s", e)
            raise
        except Exception as e:
            logging.error("Unexpected error: %s", e)
            raise
    
    async def get(self, url: str, **kwargs):
        """发送GET请求"""
        logging.info("Sending GET request to %s", url)
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs):
        """发送POST请求"""
        return await self._request("POST", url, **kwargs)

    async def delete(self, url: str, **kwargs):
        """发送DELETE请求"""
        return await self._request("DELETE", url, **kwargs)

class AuthenticatedClient(BaseClient):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)
        self._is_logged_in = False
        self._login_lock = asyncio.Lock()

    @abstractmethod
    async def _login(self):
        """子类必须实现登录逻辑"""
        raise NotImplementedError("Subclasses must implement the _login method")

    @abstractmethod
    async def _apply_auth(self):
        """子类可以重写此方法以应用特定的认证头"""
        raise NotImplementedError("Subclasses must implement the _apply_auth method")

    async def login(self):
        async with self._login_lock:
            # 确保只有一个协程在执行登录操作
            if self._is_logged_in:
                return
            try:
                await self._login()
                self._is_logged_in = True
                logging.info("Successfully logged into %s", self.__class__.__name__)
            except httpx.HTTPStatusError as e:
                logging.error("Login failed: %s", e.response.text)
                raise
            except httpx.RequestError as e:
                logging.error("Request error during login: %s", e)
                raise
            except Exception as e:
                logging.error("Unexpected error during login: %s", e)
                raise

    async def _request(self, method: str, url: str, **kwargs):
        '''自动登录逻辑'''
        if not self._is_logged_in:
            await self.login()

        # 确保请求头中包含认证信息
        auth_headers = await self._apply_auth()
        if auth_headers:
            kwargs['headers'] = {**kwargs.get('headers', {}), **auth_headers}
        try:
            return await super()._request(method, url, **kwargs)
        except AuthenticatedClientError as e:
            logging.error("Authentication error: %s", e)
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in [401, 403]:
                logging.warning("Session expired, re-logging in")
                self._is_logged_in = False
                await self.login()
                auth_headers = await self._apply_auth()
                if auth_headers:
                    kwargs['headers'] = {**kwargs.get('headers', {}), **auth_headers}
                return await super()._request(method, url, **kwargs)
            else:
                logging.error("HTTP error: %s", e.response.text)
                raise
        except httpx.RequestError as e:
            logging.error("Request error: %s", e)
            raise
        except Exception as e:
            logging.error("Unexpected error: %s", e)
            raise