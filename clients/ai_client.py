import asyncio
import textwrap
from contextlib import asynccontextmanager

import httpx2
from loguru import logger
from openai import AsyncOpenAI, OpenAIError

from clients.base_client import RateLimiter


class AIRateLimiter:
    """AI客户端多维速率限制器"""

    def __init__(
        self,
        rpm: int | None = None,
        tpm: int | None = None,
        rpd: int | None = None,
        concurrency: int | None = None,
    ):
        self.rpm_limiter = RateLimiter(rpm, 60.0) if rpm else None
        self.tpm_limiter = RateLimiter(tpm, 60.0) if tpm else None
        self.rpd_limiter = RateLimiter(rpd, 86400.0) if rpd else None
        self.concurrency_sem = asyncio.Semaphore(concurrency) if concurrency else None

    @asynccontextmanager
    async def acquire(self, tokens: int = 0):
        if self.concurrency_sem:
            await self.concurrency_sem.acquire()
        try:
            if self.rpm_limiter:
                await self.rpm_limiter.acquire(1)
            if self.tpm_limiter and tokens > 0:
                await self.tpm_limiter.acquire(tokens)
            if self.rpd_limiter:
                await self.rpd_limiter.acquire(1)
            yield
        finally:
            if self.concurrency_sem:
                self.concurrency_sem.release()


class AIClientWarper:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float | None = None,
        rpm: int | None = 60,
        rpd: int | None = None,
        tpm: int | None = None,
        concurrency: int | None = None,
        proxy: str | None = None,
    ) -> None:
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx2.AsyncClient(proxy=proxy) if proxy else None,
        )
        self.model = model
        self.temperature = temperature
        self._limiter = AIRateLimiter(
            rpm=rpm, rpd=rpd, tpm=tpm, concurrency=concurrency
        )

    async def translate(self, key: str, text: str):
        """使用AI翻译日本动画元数据的指定字段内容为中文。

        Args:
            key (str): 要翻译的字段名称。
            text (str): 要翻译的文本内容。
        Returns:
            str: 翻译后的文本，如果翻译失败则返回原文本。
        """
        async with self._limiter.acquire(tokens=len(text)):
            prompt = textwrap.dedent(f"""
                你是一个资深的日本动画（ACG）本地化翻译专家。
                请将以下动画元数据的【{key}】字段内容翻译成自然、流畅的简体中文。

                【翻译规则】
                1. 准确传达原意，符合中文语境和 ACG 圈子的阅读习惯。
                2. 人名、角色名、导演、工作室、专有名词（如特殊武器、魔法设定等）请务必使用**ACG界通用的标准中文译名**（例如保留常见的日文汉字转中文，或标准的罗马音中译）。如果是无法翻译的英文代号可保留原文。
                3. 严格保留所有的 HTML 标签（如 <i></i>）、Markdown 格式、换行符和缩进。
                4. 保持季号/集号（如 S01E02）、特殊符号不变，中英文之间适当留白。
                5. 只需要输出翻译后的最终纯文本，不要包含任何解释、注脚或“好的”、“翻译如下”等废话。

                【待翻译内容】
                {text}
            """).strip()

            logger.info("AI翻译请求，字段：{}，内容：{}", key, text)
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=1000,
                    temperature=self.temperature,
                )
                response_text = response.choices[0].message.content

                if response_text and any(
                    "\u4e00" <= char <= "\u9fff" for char in response_text
                ):
                    if response_text.startswith("【待翻译内容】"):
                        response_text = response_text.replace(
                            "【待翻译内容】", ""
                        ).strip()
                    return response_text

                logger.warning("翻译未返回有效的中文文本：{}", response_text)
                return text
            except OpenAIError as e:
                logger.error("AI 翻译期间出错：{}", e)
                return text

    async def close(self) -> None:
        """关闭底层 AsyncOpenAI 客户端以释放连接资源。"""
        await self.client.close()
