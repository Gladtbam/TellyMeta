import html
import re
from collections.abc import Callable
from string.templatelib import Interpolation, Template, convert

# 匹配 Telethon Markdown 中连续的格式分隔符（**、__、~~）
_MD_DELIM_RE = re.compile(r"\*{2,}|_{2,}|~{2,}")


class TelegramMessage:
    """Telegram 消息 t-string 模板渲染器，统一处理公共缩进剥离与动态插值转义。"""

    @staticmethod
    def escape_markdown(text: str) -> str:
        """转义动态文本中的 Telethon Markdown 分隔符，防止破坏消息格式。

        Args:
            text: 待插入消息模板的动态字符串。

        Returns:
            安全转义后的字符串。
        """
        if not text:
            return text
        # Telethon Markdown 不支持反斜杠转义，使用零宽空格打断连续分隔符并替换反引号
        escaped = text.replace("`", "'").replace("](", "]\u200b(")
        return _MD_DELIM_RE.sub(lambda m: "\u200b".join(m.group(0)), escaped)

    @staticmethod
    def escape_html(text: str) -> str:
        """转义动态文本中的 HTML 特殊字符。"""
        return html.escape(text)

    @staticmethod
    def _format_interpolation(
        interp: Interpolation, escaper: Callable[[str], str]
    ) -> str:
        """按转换标志与格式说明符格式化单个插值，默认使用指定转义器。"""
        value = convert(interp.value, interp.conversion)
        if interp.format_spec == "raw":
            return str(value)
        formatted = (
            format(value, interp.format_spec) if interp.format_spec else str(value)
        )
        return escaper(formatted)

    @staticmethod
    def _dedent_template_strings(strings: tuple[str, ...]) -> list[str]:
        """在不破坏多行插值的前提下，对模板静态文本片段执行公共缩进剥离。"""
        # 用非空白占位符拼接以准确识别包含插值的非空行缩进
        probe = "X".join(strings)
        margin: str | None = None
        for line in probe.splitlines():
            stripped = line.lstrip(" \t")
            if not stripped:
                continue
            indent = line[: len(line) - len(stripped)]
            if margin is None:
                margin = indent
            elif indent.startswith(margin):
                continue
            elif margin.startswith(indent):
                margin = indent
            else:
                common_len = 0
                for a, b in zip(margin, indent, strict=False):
                    if a != b:
                        break
                    common_len += 1
                margin = margin[:common_len]

        if not margin:
            return list(strings)

        margin_len = len(margin)
        dedented: list[str] = []
        at_line_start = True

        for segment in strings:
            parts = segment.splitlines(keepends=True)
            if not parts:
                dedented.append("")
                continue

            out: list[str] = []
            for idx, part in enumerate(parts):
                is_line_start = at_line_start if idx == 0 else True
                if is_line_start:
                    content = part.rstrip("\r\n")
                    ending = part[len(content) :]
                    if not content.strip(" \t"):
                        # 纯空白行直接清空空白，仅保留换行符
                        out.append(ending)
                    elif content.startswith(margin):
                        out.append(part[margin_len:])
                    else:
                        out.append(part)
                else:
                    out.append(part)

            at_line_start = segment.endswith(("\n", "\r"))
            dedented.append("".join(out))

        return dedented

    @classmethod
    def _render_template(cls, template: Template, escaper: Callable[[str], str]) -> str:
        """渲染 t-string 模板：剥离公共缩进并按指定转义器安全格式化插值。"""
        strings = cls._dedent_template_strings(template.strings)
        parts: list[str] = []
        for idx, text in enumerate(strings):
            parts.append(text)
            if idx < len(template.interpolations):
                parts.append(
                    cls._format_interpolation(template.interpolations[idx], escaper)
                )
        return "".join(parts)

    @classmethod
    def render_markdown(cls, template: Template) -> str:
        """渲染用于 Telegram Markdown 的 t-string 模板，自动剥离公共缩进并转义动态插值。

        使用 `{value:raw}` 可跳过对已格式化 Markdown 片段的转义。

        Args:
            template: Python t-string 模板对象。

        Returns:
            可直接发送给 Telethon `parse_mode="markdown"` 的消息字符串。
        """
        return cls._render_template(template, cls.escape_markdown)

    @classmethod
    def render_html(cls, template: Template) -> str:
        """渲染用于 Telegram HTML 的 t-string 模板，自动剥离公共缩进并转义 HTML 特殊字符。

        使用 `{value:raw}` 可跳过对已格式化 HTML 片段的转义。

        Args:
            template: Python t-string 模板对象。

        Returns:
            可直接发送给 Telethon `parse_mode="html"` 的消息字符串。
        """
        return cls._render_template(template, cls.escape_html)
