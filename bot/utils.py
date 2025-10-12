import asyncio
import io
import operator
from random import choice, randint

from loguru import logger
from PIL import Image, ImageDraw, ImageFont
from telethon import errors


async def safe_respond(event, msg: str, delete_after: int = 10) -> None:
    """安全发送消息并在指定时间后删除"""
    try:
        message = await event.respond(
            msg,
            parse_mode='markdown'
        )
        await asyncio.sleep(delete_after)
        await message.delete()
    except errors.FloodWaitError as e:
        logger.error("发送消息失败: {}", e)

async def safe_reply(event, msg: str, delete_after: int = 10) -> None:
    """安全回复消息并在指定时间后删除"""
    try:
        message = await event.reply(
            msg,
            parse_mode='markdown'
        )
        await asyncio.sleep(delete_after)
        await message.delete()
    except errors.FloodWaitError as e:
        logger.error("发送消息失败: {}", e)

async def safe_respond_keyboard(event, msg: str, keyboard, delete_after: int = 60) -> None:
    """安全发送带按钮的消息并在指定时间后删除"""
    try:
        message = await event.respond(
            msg,
            buttons=keyboard,
            parse_mode='markdown'
        )
        await asyncio.sleep(delete_after)
        await message.delete()
    except errors.FloodWaitError as e:
        logger.error("发送消息失败: {}", e)

def generate_captcha():
    num1, num2 = randint(1, 50), randint(1, 50)
    operators = choice([('+', operator.add), ('-', operator.sub), ('*', operator.mul)])

    if operators[0] == '-' and num1 < num2:
        num1, num2 = num2, num1  # 确保结果为非负数

    question = f"{num1} {operators[0]} {num2} = ?"
    answer = str(operators[1](num1, num2))

    background_color = (255, 255, 255)
    image = Image.new('RGB', (100, 35), background_color)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    # 绘制干扰元素（噪点和线）
    for _ in range(150):
        x, y = randint(0, 200), randint(0, 70)
        draw.point((x, y), fill=(randint(0, 255), randint(0, 255), randint(0, 255)))
    for _ in range(5):
        x1, y1 = randint(0, 200), randint(0, 70)
        x2, y2 = randint(0, 200), randint(0, 70)
        draw.line((x1, y1, x2, y2), fill=(randint(0, 255), randint(0, 255), randint(0, 255)), width=1)

    # 绘制问题文本
    text_color = (0, 0, 0)
    text_position = (20, 20)
    draw.text(text_position, question, font=font, fill=text_color)

    image_data = io.BytesIO()
    image.save(image_data, format='PNG')
    image_data.seek(0)
    image_data.name = 'captcha.png'

    return answer, image_data
