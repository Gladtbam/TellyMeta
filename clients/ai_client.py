import asyncio
import textwrap
from contextlib import asynccontextmanager

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
        concurrency: int | None = None
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
        concurrency: int | None = None
    ) -> None:
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.model = model
        self.temperature = temperature
        self._limiter = AIRateLimiter(rpm=rpm, rpd=rpd, tpm=tpm, concurrency=concurrency)

    async def translate(self, key: str, text: str):
        """使用AI翻译日本动画元数据的指定字段内容为中文。
        
        Args:
            key (str): 要翻译的字段名称。
            text (str): 要翻译的文本内容。
        Returns:
            str: 翻译后的文本，如果翻译失败则返回原文本。
        """
        async with self._limiter.acquire(tokens=len(text)):
            prompt = textwrap.dedent("""
                你是一名专业影视元数据翻译器。

                【你的任务】
                - 将输入的文本翻译为 **中文简体**。
                - 保留所有格式（markdown、段落、换行、缩进、符号、标签、HTML、特殊字符）。
                - 不擅自添加、删减、润色内容，只做准确翻译。

                【保持不变】
                以下类型的内容保持原样，不要翻译：
                - 人名、导演、演员名、工作室名
                - 角色名（例如 John Doe / Spider-Man）
                - 专有名词（如 Star Wars, MCU, Netflix）
                - 季号 / 集号（S01E02）
                - TMDB / TVDB 标签或字段名
                - 日期、数字、符号

                【翻译规则】
                - 文学性不强，只追求准确和清晰。
                - 有模糊描述时，保持直译，不要主观扩写。
                - 英文引号、括号、斜杠位置保持一致。
                - 如果原文带有 HTML 标签（如 <i>xxx</i>），内容翻译但标签保留。

                请只输出翻译后的内容，不要提供解释。
            """)

            # prompt = textwrap.dedent(f"""\
            #     请将以下日本动画的元数据信息（片名、导演、主演、发行公司、播出时间、集数、类型、简介等其中一个）准确、专业地翻译成中文。
            #     确保术语一致，信息清晰，表达自然流畅。只返回翻译的文本，不要包含其他内容。
            #     {key}原文内容：\n{text}
            # """)
            logger.info("AI翻译请求，字段：{}，内容：{}", key, text)
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text}
                    ],
                    max_tokens=1000,
                    temperature=self.temperature
                )
                response_text = response.choices[0].message.content
                if response_text and any('\u4e00' <= char <= '\u9fff' for char in response_text):
                    return response_text

                logger.warning("翻译未返回有效的中文文本：{}", response_text)
                return text
            except OpenAIError as e:
                logger.error("AI 翻译期间出错：{}", e)
                return text
