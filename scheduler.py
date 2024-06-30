'''
定时任务
'''
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loadconfig import init_config
from telegram import client
from scoremanager import calculate_ratio, user_msg_count
import database
import embyapi

config = init_config()
scheduler = AsyncIOScheduler()

@scheduler.scheduled_job('cron', hour='0', minute='15', second='0')
async def ban_users():
    '''
    禁用过期用户的定时任务
    每天凌晨0点15分执行一次
    '''
    try:
        logging.info("Starting ban users job")
        embyIds = await database.limit_emby_ban()
        if embyIds is not None:
            _bool = await embyapi.ban_emby_user(embyIds)
            if _bool:
                logging.info(f"Banned {len(embyIds)} users")
            else:
                logging.error("Error banning users")
        else:
            logging.info("No users to ban")
    except ImportError as e:
        logging.error("Error banning users: %s", e)
    finally:
        logging.info("Ban users job finished")

@scheduler.scheduled_job('cron', hour='0', minute='30', second='0')
async def delete_ban_users():
    '''
    删除禁用用户的定时任务
    每天凌晨0点30分执行一次
    '''
    try:
        logging.info("Starting delete ban users job")
        embyIds = await database.limit_emby_delete()
        if embyIds is not None:
            _bool = await embyapi.delete_ban_user(embyIds)
            if _bool:
                logging.info(f"Deleted {len(embyIds)} users")
            else:
                logging.error("Error deleting users")
        else:
            logging.info("No users to delete")
    except ImportError as e:
        logging.error("Error deleting users: %s", e)
    finally:
        logging.info("Delete ban users job finished")

@scheduler.scheduled_job('cron', hour='0', minute='5', second='0')
async def delete_code():
    '''
    删除过期的注册码的定时任务
    每天凌晨0点5分执行一次
    '''
    try:
        logging.info("Starting delete code job")
        _bool = await database.delete_limit_code()
        if _bool:
            logging.info("Deleted expired codes")
        else:
            logging.error("Error deleting code")
    except ImportError as e:
        logging.error("Error deleting code: %s", e)
    finally:
        logging.info("Delete code job finished")

@scheduler.scheduled_job('cron', hour='8,20', minute='0', second='0')
async def settle_score():
    '''
    结算积分的定时任务
    每天早上8点和晚上8点结算一次积分
    '''
    try:
        logging.info("Starting settle score job")
        UserRatio, TotalScore = await calculate_ratio()
        userScore = await database.settle_score(UserRatio, TotalScore)
        if userScore is not None:
            message = await client.send_message(config.telegram.ChatID, f"积分结算完成, 共结算 {TotalScore} 分\n\t结算后用户积分如下:\n")
            for userId, userValue in userScore.items():
                user = await client.get_entity(userId)
                username = user.first_name + ' ' + user.last_name if user.last_name else user.first_name
                # message += f"[{username}](tg://user?id={userId}) 获得 {userValue} 分\n"
                message = await client.edit_message(message, message.text + f"\n[{username}](tg://user?id={userId}) 获得: {userValue} 积分")
            # await client.send_message(config.telegram.ChatID, message, parse_mode='Markdown')
            user_msg_count.clear()
        else:
            await client.send_message(config.telegram.ChatID, "无可结算积分")
            logging.info("No users to settle")
    except ImportError as e:
        logging.error("Error settling score: %s", e)
    finally:
        logging.info("Settle score job finished")

@scheduler.scheduled_job('cron', minute='0', second='10')
async def server_status():
    '''
    获取服务器状态的定时任务
    每小时获取一次服务器状态, 并发送到Telegram群
    '''
    messages = None
    try:
        logging.info("Starting server status job")
        probe_info = await embyapi.get_server_info()
        session_list = await embyapi.session_list()
        if probe_info is not None and session_list is not None:
            message = f'''
当前在线人数: {session_list}
系统负载: {probe_info['result'][0]['status']['Load5']}
CPU负载: {"{:.3f}%".format(probe_info['result'][0]['status']['CPU'])}
内存使用率: {"{:.3f}%".format((probe_info['result'][0]['status']['MemUsed'] / probe_info['result'][0]['host']['MemTotal']) * 100)}
实时下载: {"{:.2f} Mbps".format(probe_info['result'][0]['status']['NetInSpeed'] * 8 / 1_000_000)}
实时上传: {"{:.2f} Mbps".format(probe_info['result'][0]['status']['NetOutSpeed'] * 8 / 1_000_000)}

**积分注册开启, 当前注册积分**: {int(await database.get_renew_value() * config.other.Ratio)}
'''
            messages = await client.send_message(config.telegram.ChatID, message, parse_mode='Markdown')
        else:
            logging.error("Error getting server status")
    except ImportError as e:
        logging.error("Error getting server status: %s", e)
    finally:
        await asyncio.sleep(3599)
        if messages is not None:
            await messages.delete()

async def start_scheduler():
    '''启动定时任务'''
    try:
        if not scheduler.running:
            scheduler.start()
            print('Press Ctrl+C to exit')
        else:
            logging.info("Scheduler already running")
    except ImportError as e:
        logging.error("Error starting scheduler: %s", e)
