import asyncio
from loadconfig import init_config
from telegram import client
import arr
import notify
import database
import embyaccount
import gencode
import scheduler
import scoremanager


if __name__ == '__main__':
    config = init_config()
    loop = asyncio.get_event_loop()
    tasks = [scheduler.start_scheduler(), database.init_db(), notify.notifyarr.run_webhook()]
    loop.run_until_complete(asyncio.gather(*tasks))
    client.run_until_disconnected()
