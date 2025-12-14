# TellyMeta

**TellyMeta** 是一个强大的个人媒体管理工具，旨在通过 Telegram Bot 简化媒体（电影和剧集）的搜索和请求流程。它深度集成了 Sonarr 和 Radarr，支持自动化的媒体库管理和审批工作流。

## ✨ 功能特性

*   **🤖 智能交互体验**：
    *   **自动求片**：直接发送电影/剧集名称进行搜索，支持一键请求。
    *   **入群验证**：集成 Captcha 验证码，防止广告账号骚扰。
*   **🎥 媒体库深度集成**：
    *   **无缝对接**：支持 Sonarr (剧集) 和 Radarr (电影)。
    *   **中文优化**：优先获取中文元数据 (TMDB/TVDB)，智能匹配展示。
    *   **查重机制**：请求时自动检测库中是否已存在，避免重复下载。
*   **💎 用户与积分体系**：
    *   **账号自动化**：Emby 账号自动开通、密码重置、到期续费。
    *   **积分生态**：通过签到、群组活跃获取积分，用于兑换邀请码或账号续期。
    *   **邀请机制**：支持生成注册码和续期码，通过积分流转实现社区自治。
*   **� 实用工具箱**：
    *   **字幕上传**：支持发送 Zip 包自动重命名并通过 Sonarr/Radarr 导入媒体目录。
    *   **个性化设置**：用户可自助开关 NSFW 内容过滤。
    *   **线路查询**：一键获取最新的媒体服务器访问地址。
*   **👮 强大的管理后台**：
    *   **审批流**：管理员可一键批准或拒绝求片请求。
    *   **用户管理**：踢出、封禁、警告、删号等全套管理指令。
    *   **系统配置**：完全通过 Telegram 界面配置机器人参数，无需重启。

## 🛠️ 技术栈

本项目基于 Python 3.11+ 开发，主要使用的库包括：
*   **FastAPI**: 高性能 Web 框架。
*   **Telethon**: 强大的 Telegram MTProto 客户端。
*   **SQLAlchemy (Async)**: 异步数据库 ORM。
*   **AioSQLite**: 异步 SQLite 数据库支持。
*   **Loguru**: 优雅的日志记录。

## 🚀 安装指南

### 前置要求

*   Python 3.11 或更高版本
*   Telegram API ID 和 API Hash (可从 [my.telegram.org](https://my.telegram.org) 获取)
*   Telegram Bot Token (通过 BotFather 获取)

### 1. 克隆仓库

```bash
git clone https://github.com/your-username/TellyMeta.git
cd TellyMeta
```

### 2. 安装依赖

推荐使用 `uv` 进行包管理，也可以使用 `pip`。

使用 `uv` (推荐):
```bash
uv sync
```

或者使用 `pip`:
```bash
pip install .
```

### 3. 配置环境

复制示例配置文件并进行修改：

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入必要的配置信息：

```ini
# Telegram 配置
telegram_api_id=your_api_id
telegram_api_hash=your_api_hash
telegram_bot_token=your_bot_token
telegram_bot_name=@your_bot_name

# Sonarr 配置
sonarr_url=http://localhost:8989
sonarr_api_key=your_sonarr_api_key

# Radarr 配置
radarr_url=http://localhost:7878
radarr_api_key=your_radarr_api_key

# TMDB 配置 (用于获取元数据)
tmdb_api_key=your_tmdb_api_key
```

## ▶️ 运行

确保所有配置正确后，启动服务：

```bash
python main.py
```

## 📖 使用指南

### 🎬 求片流程
1.  **搜索**: 私聊 Bot 发送 `/me` 点击“开始求片”，或直接发送 `/start` 开始流程。
2.  **选择**: Bot 会列出搜索结果，点击按钮选择你想看的媒体。
3.  **请求**: 确认请求后，消息将推送到管理群等待审核。
4.  **观看**: 管理员批准后，媒体将自动加入下载队列。

### 🔑 账号注册与使用
1.  **注册**: 发送 `/signup` (如开放) 或 使用 `/code <邀请码>` 进行注册。注册成功后将获得 Emby 账号密码。
2.  **个人中心**: 发送 `/me` 查看积分、续期账号、修改密码或管理 NSFW 设置。
3.  **积分**: 在群组内发送 `/checkin` 每日签到，或多多参与讨论获取活跃积分。

### 📤 字幕上传
直接向 Bot 发送 Zip 格式的字幕压缩包，文件名需符合规范：
*   **剧集**: `tvdb-ID.zip` (如 `tvdb-12345.zip`) -> 自动匹配 SxxExx
*   **电影**: `tmdb-ID.zip` (如 `tmdb-67890.zip`)
Bot 会自动解压并将其分发到对应的媒体文件夹中。

## 📝 指令列表

### 👤 用户指令

| 指令 | 说明 | 备注 |
| :--- | :--- | :--- |
| `/start` | 启动机器人 | 如果开启验证，需进行验证码验证 |
| `/help` | 获取帮助信息 | 查看所有可用指令 |
| `/me` | 个人中心 | 查看 Emby 账户、积分、发起求片、上传字幕等 |
| `/checkin` | 每日签到 | 仅在管理员绑定的群组内有效 |
| `/code <激活码>` | 使用激活码 | 用于注册账户或续期 |
| `/signup` | 注册账户 | 仅在开放注册时可用 |
| `/chat_id` | 获取群组 ID | 需在群组中使用 |

### 👮 管理员指令

> 部分管理指令需要 **回复** 目标用户的消息才能生效。

| 指令 | 说明 | 备注 |
| :--- | :--- | :--- |
| `/settings` | **打开管理面板** | 核心配置入口 (管理管理员、通知、媒体库绑定等) |
| `/info` | 查看用户信息 | 需回复用户消息 |
| `/warn` | 警告用户 | 扣除积分并增加警告计数 (需回复) |
| `/del` | 删除账户 | 删除用户的 Emby 账户 (需回复) |
| `/kick` | 踢出用户 | 踢出群组并删除账户 (需回复) |
| `/ban` | 封禁用户 | 封禁并删除账户 (需回复) |
| `/change <数值>` | 修改积分 | 手动增减用户积分, 如 `/change 100` (需回复) |
| `/settle` | 结算积分 | 手动触发活跃度积分结算 |

## 🤝 贡献

欢迎提交 Pull Request 或 Issue 来改进这个项目！

## 📄 许可证

[MIT License](LICENSE)
