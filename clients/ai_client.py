import textwrap

from loguru import logger
from openai import AsyncOpenAI


class AIClientWarper:
    def __init__(self, base_url, api_key, model) -> None:
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.model = model

    async def translate(self, key: str, text: str):
        """使用AI翻译日本动画元数据的指定字段内容为中文。
        
        Args:
            ai_client (AsyncOpenAI): 异步OpenAI客户端实例。
            key (str): 要翻译的字段名称。
            text (str): 要翻译的文本内容。
        Returns:
            str: 翻译后的文本，如果翻译失败则返回原文本。
        """
        prompt = textwrap.dedent(f"""\
            请将以下日本动画的元数据信息（片名、导演、主演、发行公司、播出时间、集数、类型、简介等其中一个）准确、专业地翻译成中文。
            确保术语一致，信息清晰，表达自然流畅。只返回翻译的文本，不要包含其他内容。
            {key}原文内容：\n{text}
        """)
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {'role': 'system', 'content': '您是一位专门研究日本动漫元数据的专业翻译。'},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1000,
                temperature=0.5
            )
            response_text = response.choices[0].message.content
            if response_text and any('\u4e00' <= char <= '\u9fff' for char in response_text):
                return response_text

            logger.warning("Translation did not return valid Chinese text: {}", response_text)
            return text
        except Exception as e:
            logger.error("Error during AI translation: {}", e)
            return text
