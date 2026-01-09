from pathlib import Path

from jinja2 import (Environment, FileSystemLoader, TemplateNotFound,
                    select_autoescape)
from loguru import logger


class TemplateManager:
    _instance = None
    _env: Environment | None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TemplateManager, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """初始化 Jinja2 环境"""
        template_dir = Path.cwd() / "templates"
        if not template_dir.exists():
            template_dir = Path(__file__).parent.parent / "templates"

        if not template_dir.exists():
            logger.warning("未找到模板目录: {}", template_dir)

        self._env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(['html', 'xml']),
            enable_async=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        logger.info("模板引擎初始化完成，目录: {}", template_dir)

    @property
    def env(self) -> Environment:
        """
        获取 Jinja2 Environment 实例。
        如果未初始化则抛出错误（满足类型检查器的非空要求）。
        """
        if self._env is None:
            raise RuntimeError("TemplateManager 尚未初始化！")
        return self._env

    async def render(self, template_name: str, context: dict) -> str | None:
        """通用渲染方法"""
        try:
            template = self.env.get_template(template_name)
            return await template.render_async(context)
        except TemplateNotFound:
            logger.info("缺少通知模板文件：{}", template_name)
            return None
        except Exception as e:
            logger.error("渲染模板 {} 失败: {}", template_name, e)
            return None

template_manager = TemplateManager()
