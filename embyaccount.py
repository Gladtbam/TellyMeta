'''
Emby 帐户管理
'''
import re
import logging
from datetime import datetime, timedelta
import asyncio
from telethon import events, types
from telegram import client
from loadconfig import init_config
import embyapi
import gencode
import database

config = init_config()

signup_info = {"time": 0, "remain_num": 0.0}      # 注册方法
signup_message = None

@client.on(events.NewMessage(pattern=fr'^/signup(?:{config.telegram.botName})?(\s.*)?$'))
async def signup_method(event):
    '''
    设置注册方法
    管理员可执行设置注册人数（限额注册）或注册时间（限时注册）
    非管理员执行注册
    '''
    global signup_message
    _, *args = event.message.text.split(' ')
    current_time = datetime.now().timestamp()
    user = await database.get_user(event.sender_id)
    signup_value = await database.get_renew_value() * config.other.ratio
    try:
        if event.sender_id in config.other.adminId:
            if len(args) > 0:
                if re.match(r'^\d+$', args[0]):
                    signup_info["remain_num"] = args[0]
                    signup_message = await client.send_message(config.telegram.chatID, f'开启注册, 剩余 {signup_info["remain_num"]} 个名额')
                elif re.match(r'^(\d+[hms])+$', args[0]):
                    last_time = re.match(r'(\d+h)?(\d+m)?(\d+s)?', args[0])
                    if last_time is not None:
                        hours = int(last_time.group(1)[:-1]) if last_time.group(1) else 0
                        minutes = int(last_time.group(2)[:-1]) if last_time.group(2) else 0
                        seconds = int(last_time.group(3)[:-1]) if last_time.group(3) else 0
                        signup_info["time"] = current_time + (timedelta(hours=hours) + timedelta(minutes=minutes) + timedelta(seconds=seconds)).total_seconds()
                    dt_object = datetime.fromtimestamp(float(signup_info["time"]))
                    signup_message = await client.send_message(config.telegram.chatID, f'开启注册, 时间截至 {dt_object.strftime("%Y-%m-%d %H:%M:%S")}')
                await client.pin_message(config.telegram.chatID, signup_message, notify=True)
            else:
                await signup(event, event.sender_id)
        else:
            if signup_info['remain_num'] > 0:
                await signup(event, event.sender_id)
                signup_info['remain_num'] -= 1
                await client.edit_message(config.telegram.chatID, signup_message, f"开启注册, 剩余注册人数: {signup_info['remain_num']}")
            elif signup_info['time'] > current_time:
                await signup(event, event.sender_id)
            elif user is not None and user.Score >= signup_value:
                await signup(event, event.sender_id)
                await database.change_score(event.sender_id, -signup_value)
            else:
                await event.reply(f'注册失败, 积分不足, 当前积分: {user.Score if user is not None else 0}, 注册所需积分: {signup_value}')
    except ImportError as e:
        logging.error("signup_method: %s", e)
    finally:
        await asyncio.sleep(10)
        await event.delete()
        # # await event.message.delete()
        if (signup_info['remain_num'] == 0 or signup_info['time'] < current_time) and signup_message is not None:
            await signup_message.delete()
        # raise events.StopPropagation

async def signup(event, TelegramId):
    '''注册'''
    message = None
    try:
        user = await event.client.get_entity(TelegramId)
        TelegramName = user.username
        BlockMedia = ("Japan")

        if TelegramName is None:
            message = await event.reply('注册失败, 请先设置 Telegram 用户名')
            return False
        else:
            emby = await database.get_emby(TelegramId)
            if emby is None:
                EmbyId = await embyapi.new_user(TelegramName)
                if EmbyId is not None:
                    await embyapi.user_policy(EmbyId, BlockMeida=BlockMedia)
                    Pw = await embyapi.post_password(EmbyId)
                    _bool = await database.create_emby(TelegramId, EmbyId, TelegramName)
                    if _bool:
                        message = await event.reply(f'注册成功, \nEMBY ID: `{EmbyId}`\n用户名: `{TelegramName}`\n初始密码: `{Pw}`\n\n请及时修改密码')
                        return True
                    else:
                        message = await event.reply('注册失败, ⚠️数据库错误，请联系管理员')
                        return False
                else:
                    message = await event.reply('注册失败, 无法创建账户，请联系管理员')
                    return False
            else:
                message = await event.reply('用户已存在')
                return False
    except ImportError as e:
        logging.error("signup: %s", e)
        return False
    finally:
        await asyncio.sleep(10)
        await event.delete()
        if message is not None:
            await message.delete()
        # raise events.StopPropagation

@client.on(events.NewMessage(pattern=fr'^/code({config.telegram.botName})?\s+(.*)$'))
async def code_check(event):
    '''接收 Telegram 用户的 码'''
    _, *args = event.message.text.split(' ')
    message = None
    try:
        if len(args) > 0:
            if event.is_private or event.sender_id in config.other.adminId:
                await code(event, args[0])
            else:
                message = await event.reply(f'请私聊 {config.telegram.botName} 机器人')
        else:
            message = await event.reply('请回复 “码”')
    except ImportError as e:
        logging.error("code_check: %s", e)
    finally:
        await asyncio.sleep(10)
        await event.delete()
        if message is not None:
            await message.delete()
        # raise events.StopPropagation

async def code(event, code):
    '''处理 码'''
    message = None
    try:
        plaintext = await gencode.decrypt_code(code)
        if plaintext is not None:
            if plaintext == 'signup':
                await signup(event, event.sender_id)
                await database.delete_code(code)
            elif plaintext == 'renew':
                emby = await database.get_emby(event.sender_id)
                if emby is not None:
                    remain_day = emby.LimitDate.date() - datetime.now().date()
                    if remain_day.days <= 7:
                        await database.update_limit_date(event.sender_id)
                        await database.delete_code(code)
                        if emby.Ban is True:
                            await embyapi.user_policy(emby.EmbyId, BlockMeida=("Japan"))
                        message = await event.reply('续期成功')
                    else:
                        message = await event.reply(f'离到期还有 {remain_day.days} 天\n目前小于 7 天才允许续期')
                else:
                    message = await event.reply('用户不存在, 请注册')
            else:
                message = await event.reply(f'不存在对应的：{plaintext}, 码无效, 请联系管理员')
        else:
            message = await event.reply('码无效')
    except ImportError as e:
        logging.error("code: %s", e)
    finally:
        await asyncio.sleep(30)
        await event.delete()
        if message is not None:
            await message.delete()
        # raise events.StopPropagation

@client.on(events.NewMessage(pattern=fr'^/del({config.telegram.botName})?$'))
async def delete(event):
    '''删除用户指令'''
    messages = None
    try:
        if event.sender_id in config.other.adminId:
            if event.reply_to_msg_id is not None:
                message = await event.get_reply_message()
                if isinstance(message, types.Message) and isinstance(message.from_id, types.PeerUser):
                    user_id = message.from_id.user_id
                    emby = await database.get_emby(user_id)
                    if emby is not None:
                        _bool_db = await database.delete_emby(user_id)
                        _bool_emby = await embyapi.delete_emby_user(emby.EmbyId)
                        if _bool_db and _bool_emby:
                            messages = await event.reply(f'用户 {emby.EmbyId} 删除成功')
                        else:
                            messages = await event.reply(f'用户 {emby.EmbyId} 删除失败, 原因: db: {_bool_db}, emby: {_bool_emby}')
                    else:
                        messages = await event.reply('用户不存在')
                else:
                    messages = await event.reply('请回复一个用户')
            else:
                messages = await event.reply('请回复一个用户')
        else:
            messages = await event.reply('非管理员, 权限不足')
            await database.change_warning(event.sender_id)
    except ImportError as e:
        logging.error("delete: %s", e)
    finally:
        await asyncio.sleep(10)
        await event.delete()
        if messages is not None:
            await messages.delete()
        raise events.StopPropagation

@client.on(events.CallbackQuery(data='renew'))
async def renew(event):
    '''接收 续期 按钮'''
    message = None
    try:
        emby = await database.get_emby(event.sender_id)
        if emby is not None:
            remain_day = emby.LimitDate.date() - datetime.now().date()
            if remain_day.days <= 7:
                user = await database.get_user(event.sender_id)
                played_ratio = await embyapi.user_playlist(emby.EmbyId, emby.LimitDate.strftime("%Y-%m-%d"))
                if played_ratio is not None:
                    if played_ratio >= 1:
                        renew_value = 0
                    else:
                        renew_value = int(await database.get_renew_value()) * (1 - (0.5 * played_ratio))
                    if user is not None and user.Score >= renew_value:
                        await database.update_limit_date(event.sender_id)
                        await database.change_score(event.sender_id, -renew_value)
                        if emby.Ban is True:
                            await embyapi.user_policy(emby.EmbyId, BlockMeida=("Japan"))
                        message = await event.respond(f'续期成功, 扣除积分: {renew_value}')
                    else:
                        message = await event.respond(f'续期失败, 积分不足, 当前积分: {user.Score if user is not None else 0}, 续期所需积分: {renew_value}')
                else:
                    message = await event.respond('续期失败, 未查询到观看度, 请稍后重试')
            else:
                message = await event.respond(f'离到期还有 {remain_day.days} 天\n目前小于 7 天才允许续期')
        else:
            message = await event.respond('用户不存在, 请注册')
    except ImportError as e:
        logging.error("renew: %s", e)
    finally:
        await asyncio.sleep(20)
        await event.delete()
        if message is not None:
            await message.delete()
        # raise events.StopPropagation

@client.on(events.CallbackQuery(data='nfsw'))
async def nfsw(event):
    '''接收 NSFW 按钮'''
    message = None
    try:
        emby = await database.get_emby(event.sender_id)
        if emby is not None:
            emby_info = await embyapi.get_user_info(emby.EmbyId)
            if len(emby_info['Policy']['BlockedMediaFolders']) > 0:
                await embyapi.user_policy(emby.EmbyId, BlockMeida=())
                message = await event.respond('NSFW On')
            else:
                await embyapi.user_policy(emby.EmbyId, BlockMeida=("Japan"))
                message = await event.respond('NSFW Off')
        else:
            message = await event.respond('用户不存在, 请注册')
    except ImportError as e:
        logging.error("nfsw: %s", e)
    finally:
        await asyncio.sleep(10)
        await event.delete()
        if message is not None:
            await message.delete()
        raise events.StopPropagation

@client.on(events.CallbackQuery(data='forget_password'))
async def forget_password(event):
    '''接收 忘记密码 按钮'''
    message = None
    try:
        emby = await database.get_emby(event.sender_id)
        if emby is not None:
            _bool = await embyapi.post_password(emby.EmbyId, ResetPassword=True)
            if _bool:
                Pw = await embyapi.post_password(emby.EmbyId)
                await event.respond(f'密码已重置:\n `{Pw}`\n请及时修改密码')
            else:
                message = await event.respond('密码重置失败')
        else:
            message = await event.respond('用户不存在, 请注册')
    except ImportError as e:
        logging.error("forget_password: %s", e)
    finally:
        await asyncio.sleep(60)
        await event.delete()
        if message is not None:
            await message.delete()

@client.on(events.CallbackQuery(data='query_renew'))
async def query_renew(event):
    '''接收 查询续期积分 按钮'''
    message = None
    try:
        value = await database.get_renew_value()
        message = await event.respond(f'当前续期积分: {value}')
    except ImportError as e:
        logging.error("query_renew: %s", e)
    finally:
        await asyncio.sleep(10)
        await event.delete()
        if message is not None:
            await message.delete()
        raise events.StopPropagation
