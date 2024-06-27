
# Bot 自定义命令菜单
>
> 私聊 @BotFather, Edit Bot - Edit Commands  

```text  
help - [私聊]帮助
checkin - 签到
signup - 注册, 仅开放注册时使用
code - [私聊]使用注册码注册, 或者使用续期码续期。例: /code 123
del - [管理员]删除 Emby 账户, 需回复一个用户
me - [私聊]查看 Emby 账户 和 个人 信息(包含其它工具)
warn - [管理员]警告
info - [管理员]查看用户信息
settle - [管理员]手动结算积分
change - [管理员]手动修改积分, 正数加负数减
```
  
**对于管理员: 使用 /signup 整数开启限额注册, /signup 时间(h m s)开启限时注册。例如: /signup 10；/signup 1h2m**  

# 功能
- [X] 积分管理
- [X] 用户管理
- [X] "码"
- [X] 字幕上传
- [X] 求片
- [X] 通知(半成品)

# 如何使用

1. 数据库配置：新建一个数据库，安装程序启动时的提示填充`数据库名`, `操作数据库的用户名和密码`, `数据库地址`, `数据库端口`  ，程序会自行创建数据库表

2. 启动程序
``` bash
apt install python3-full    # Debian/Ubuntu
git clone https://github.com/Gladtbam/EmbyBot.git
cd EmbyBot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```
**首次启动请务必执行 `python main.py`，根据程序提示填充配置**

## Systemd
> vim /etc/systemd/system/embybot.service

```
[Unit]
Description=Emby Bot From EmbyHub by Gladtbam
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/EmbyBot/
Environment="PATH=/opt/EmbyBot/.venv/bin"
ExecStart=/opt/EmbyBot/.venv/bin/python3 /opt/EmbyBot/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

# loadconfig 程序配置解释

<table>
    <tr>
        <td rowspan="5">DataBase</td>
        <td>Host</td>
        <td>数据库地址，默认localhost</td>
    </tr>
    <tr>
        <td>Port</td>
        <td>数据库端口，默认3066</td>
    </tr>
    <tr>
        <td>User</td>
        <td>可操作该数据库的用户的用户名</td>
    </tr>
    <tr>
        <td>Password</td>
        <td>密码</td>
    </tr>
    <tr>
        <td>DatabaseName</td>
        <td>数据库名</td>
    </tr>
    <tr>
        <td rowspan="7">Telegram</td>
        <td>Token</td>
        <td>机器人的Token，从@BotFather获取</td>
    </tr>
    <tr>
        <td>ApiId</td>
        <td rowspan="2">API_ID和API_HASH，从https://my.telegram.org/apps 获取</td>
    </tr>
    <tr>
        <td>ApiHash</td>
    </tr>
    <tr>
        <td>BotName</td>
        <td>机器人的用户名，比如@EdHubot</td>
    </tr>
    <tr>
        <td>ChatID</td>
        <td>聊天群组ID，比如-123456789</td>
    </tr>
    <tr>
        <td>RequiredChannel</td>
        <td>求片频道，比如@emby_hub_request</td>
    </tr>
    <tr>
        <td>NotifyChannel</td>
        <td>通知频道，比如@embyhub</td>
    </tr>
    <tr>
        <td rowspan="2">Emby</td>
        <td>Host</td>
        <td>Emby 地址</td>
    </tr>
    <tr>
        <td>ApiKey</td>
        <td>Emby API</td>
    </tr>
    <tr>
        <td rowspan="3">Probe</td>
        <td>Host</td>
        <td rowspan="3">哪吒探针</td>
    </tr>
    <tr>
        <td>Token</td>
    </tr>
    <tr>
        <td>Id</td>
    </tr>
    <tr>
        <td rowspan="4">Other</td>
        <td>AdminId</td>
        <td>机器人管理员的Telegram Id列表</td>
    </tr>
    <tr>
        <td>OMDBApiKey</td>
        <td>OMDB</td>
    </tr>
    <tr>
        <td>Ratio</td>
        <td>注册积分=续期积分*Ratio</td>
    </tr>
    <tr>
        <td>Wiki</td>
        <td>Wiki地址</td>
    </tr>
    <tr>
        <td rowspan="2">Lidarr</td>
        <td>Host</td>
        <td  rowspan="2">音乐自动化(ToDo,未完成)</td>
    </tr>
    <tr>
        <td>ApiKey</td>
    </tr>
    <tr>
        <td rowspan="2">Radarr</td>
        <td>Host</td>
        <td rowspan="2">电影自动化</td>
    </tr>
    <tr>
        <td>ApiKey</td>
    </tr>
    <tr>
        <td rowspan="2">Sonarr</td>
        <td>Host</td>
        <td rowspan="2">剧集自动化</td>
    </tr>
    <tr>
        <td>ApiKey</td>
    </tr>
    <tr>
        <td rowspan="2">SonarrAnime</td>
        <td>Host</td>
        <td rowspan="2">动画自动化</td>
    </tr>
    <tr>
        <td>ApiKey</td>
    </tr>
</table>

> **AdminId 存在BUG，其无法正常生成为列表，请自行修改为列表的格式，示例如下**
```
other:
  AdminId:
    - 管理员1_ID
    - 管理员2_ID
```

>> 求片功能依赖于Sonarr和Radarr

# 选择启用功能

该库并没有写功能选择的功能，但可以通过 `注释装饰器` 关闭不需要的功能：  

```python
示例：
关闭签到功能
# @client.on(events.NewMessage(pattern=fr'^/checkin({config.telegram.BotName})?$')) 

关闭定时积分结算
# @scheduler.scheduled_job('cron', hour='8,20', minute='0', second='0')
```

# 注册码、续期码

码的生成是通过时间戳加密指定字符串  

在本服中，注册码的字符串是`signup`，续期码是`renew`，通过加密这两个字符串得到不同功能的码，使用时在将码解析回字符串来区分码的功能，因此，还可以加密其它字符串来做不同的功能
