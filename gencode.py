'''
创建/解析码
当前时间时间戳和N个0填充成16位密钥，使用AES加密，Base64编码后生成码
'''
import base64
import logging
from time import time
import asyncio
from Crypto.Cipher import AES
from telethon import events, Button
from telegram import client
from database import get_code, create_code, get_user, get_renew_value, change_score
from loadconfig import init_config

config = init_config()

async def generate_code(s):
    '''生成码'''
    timestamp = int(time())
    key = (str(timestamp) + '0' * (16 - len(str(timestamp)))).encode('utf-8')
    cipher = AES.new(key, AES.MODE_EAX)
    ciphertext, tag = cipher.encrypt_and_digest(s.encode('utf-8'))
    encoded = base64.b64encode(cipher.nonce + ciphertext).decode('utf-8')
    code = '-'.join([encoded[i:i+4] for i in range(0, len(encoded), 4)])
    try:
        await create_code(code, timestamp, base64.b64encode(tag).decode('utf-8'))
        return code
    except ImportError as e:
        logging.error("创建码失败：%s", e)
        return None

async def decrypt_code(code):
    '''解析码'''
    try:
        _code = await get_code(code)
        if _code is not None:
            timestamp = _code.TimeStamp
            tag = base64.b64decode(_code.Tag.encode('utf-8'))
            key = (str(timestamp) + '0' * (16 - len(str(timestamp)))).encode('utf-8')
            encoded = code.replace('-', '')
            decoded = base64.b64decode(encoded.encode('utf-8'))
            nonce = decoded[:16]
            ciphertext = decoded[16:]
            cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
            return plaintext.decode('utf-8')
        else:
            return None
    except ValueError as ve:
        logging.error("数据被篡改： %s", ve)
        return None
    except ImportError as e:
        logging.error("解析码失败：%s", e)
        return None

@client.on(events.CallbackQuery(data='code_create'))
async def creating_code(event):
    '''接收生成码请求，返回生成码类型选择按钮'''
    keyboard = [
            Button.inline('生成注册码', data='signup_code'),
            Button.inline('生成续期码', data='renew_code'),
    ]
    message = None
    try:
        message = await event.respond('请选择生成的码类型\n使用前请先查看 WiKi, 否则造成的损失自付', buttons=keyboard)
    except ImportError as e:
        logging.error("生成码请求失败：%s", e)
    finally:
        await asyncio.sleep(10)
        await event.delete()
        if message is not None:
            await message.delete()
        raise events.StopPropagation

@client.on(events.CallbackQuery(pattern=r'.*_code$'))
async def right_code(event):
    '''接收生成码类型选择，生成码'''
    try:
        s = event.data.decode().split('_')[0]
        user = await get_user(event.sender_id)
        value = await get_renew_value()
        await event.answer(f"正在生成 {'续期码' if s == 'renew' else '注册码'}")
        if event.sender_id in config.other.adminId or (user is not None and user.Score >= value):
            code = await generate_code(s)
            if code is not None:
                await client.send_message(event.sender_id, f"{'续期码' if s == 'renew' else '注册码'}生成成功\n`{code}`")
                await change_score(event.sender_id, -(value if event.sender_id not in config.other.adminId else 0))
            else:
                await event.respond(f"{'续期码' if s == 'renew' else '注册码'}生成失败")
        else:
            await event.respond("积分不足, 生成失败")
    except ImportError as e:
        logging.error("生成码失败：%s", e)
    finally:
        await asyncio.sleep(10)
        await event.delete()
        raise events.StopPropagation
