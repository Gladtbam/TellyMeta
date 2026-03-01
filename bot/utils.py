import asyncio
import io
import operator
from random import choice, randint

from loguru import logger
from PIL import Image, ImageDraw, ImageFont
from telethon import errors, events


async def _delayed_delete(message, delay: int = 10) -> None:
    """延迟删除消息"""
    await asyncio.sleep(delay)
    await safe_delete(message)

async def safe_respond(event, msg: str, delete_after: int = 10):
    """安全发送消息并在指定时间后删除"""
    try:
        message = await event.respond(
            msg,
            parse_mode='markdown'
        )
        asyncio.create_task(_delayed_delete(message, delete_after))
        return message
    except errors.FloodWaitError as e:
        logger.error("发送消息失败: {}", e)

async def safe_reply(event, msg: str, delete_after: int = 10):
    """安全回复消息并在指定时间后删除"""
    try:
        message = await event.reply(
            msg,
            parse_mode='markdown'
        )
        asyncio.create_task(_delayed_delete(message, delete_after))
        return message
    except errors.FloodWaitError as e:
        logger.error("发送消息失败: {}", e)

async def safe_respond_keyboard(event, msg: str, keyboard, delete_after: int = 60):
    """安全发送带按钮的消息并在指定时间后删除"""
    try:
        message = await event.respond(
            msg,
            buttons=keyboard,
            parse_mode='markdown'
        )
        asyncio.create_task(_delayed_delete(message, delete_after))
        return message
    except errors.FloodWaitError as e:
        logger.error("发送消息失败: {}", e)

async def safe_delete(message) -> None:
    """安全删除消息，忽略由于消息不存在或无权限导致的错误"""
    if not message:
        return
    try:
        await message.delete()
    except (errors.MessageDeleteForbiddenError, errors.MessageNotModifiedError, ValueError):
        # 忽略无法删除或未修改的错误
        pass
    except errors.RPCError as e:
        # 记录其他 RPC 错误但不要崩溃
        logger.warning(f"删除消息失败 (RPCError): {e}")

def generate_captcha():
    """生成一个简单的数学验证码图片，返回答案和图片数据"""
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

async def get_user_input_or_cancel(conv, cancel_button_msg_id: int) -> str | None:
    """
    通用 Conversation 辅助函数：等待用户输入文本，同时监听特定消息上的取消按钮。
    
    Args:
        conv: Telethon conversation 对象
        cancel_button_msg_id (int): 包含取消按钮的消息 ID
    
    Returns:
        str: 用户输入的文本
        None: 用户点击了取消，或超时，或输入了 '/' 开头的命令
    """
    # 定义过滤器：只监听点击了 cancel_button_msg_id 消息上按钮的事件
    def filter_button(e):
        return e.message_id == cancel_button_msg_id

    # 创建两个并发任务
    # 1. 等待用户发送文本消息
    task_response = asyncio.create_task(conv.get_response())
    # 2. 等待用户点击取消按钮
    task_cancel = asyncio.create_task(
        conv.wait_event(events.CallbackQuery(func=filter_button))
    )

    done, pending = await asyncio.wait(
        [task_response, task_cancel],
        return_when=asyncio.FIRST_COMPLETED
    )

    # 清理未完成的任务
    for task in pending:
        task.cancel()

    if task_cancel in done:
        # 用户点击了取消按钮
        try:
            event = task_cancel.result()
            await event.answer("已取消")
            await safe_delete(event) # 删除提示消息
        except Exception:
            pass
        return None

    if task_response in done:
        # 用户发送了消息
        try:
            msg = task_response.result()
            # 检查是否是命令，如果是命令则视为中断
            if msg.text and msg.text.startswith('/'):
                await conv.send_message(f"检测到新命令 {msg.text.split()[0]}，当前会话已结束。")
                return None
            return msg.text.strip() if msg.text else None
        except Exception:
            return None

    return None
