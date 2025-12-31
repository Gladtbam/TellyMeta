# 🌌 TellyMeta

**TellyMeta** 是一个集成了 Emby/Jellyfin、Sonarr 和 Radarr 的 **Telegram 媒体库管理机器人**。

旨在简化媒体库的日常运营工作，提供自动求片、通知推送、账号管理以及积分社区等功能，帮助管理员更高效地维护媒体库与用户社区。

[功能特性](#-功能特性) • [部署指南](#-部署指南) • [命令列表](#-命令列表)

---

## ✨ 核心功能

### 🤖 自动化处理
* **多实例支持**：同时接管多个 Sonarr/Radarr 和 Emby/Jellyfin 服务端。支持将不同 Telegram 按钮（媒体库）绑定至特定实例，实现请求的精准路由。
* **求片系统**：用户发送电影/剧集名称，Bot 自动搜索并展示详情，支持去重检测，一键发起下载请求。
* **AI 翻译**：集成 OpenAI，自动将 TMDB/TVDB 的外文元数据（简介、标题等）翻译为中文。
* **消息通知**：实时推送下载完成、入库通知，支持 HTML 模板自定义，可展示 HDR/Dolby 等媒体信息。
* **字幕助手**：支持用户直接发送 Zip 压缩包，Bot 自动识别对应的剧集/电影并重命名导入。

### 💎 用户与积分
* **积分体系**：通过群组签到、活跃发言获取积分，用于兑换注册邀请码或账号续期。
* **账号托管**：Emby/Jellyfin 账号的自动注册、密码重置、有效期管理以及到期自动封禁。
* **邀请机制**：管理员可生成一次性注册码或续期码，便于分发。
* **入群验证**：内置图形验证码，辅助拦截广告账号。

### 🛡️ 管理后台
* **审批流**：求片请求实时推送到管理群，管理员可直接在 Telegram 中批准或拒绝。
* **可视化配置**：通过 `/settings` 命令即可在 Telegram 界面管理服务器连接、通知渠道、NSFW 策略等，无需修改配置文件。
* **用户管控**：支持踢出、封禁、警告用户，并联动删除媒体库账号。

## 🛠️ 技术栈

* **核心框架**: Python 3.11+, FastAPI, SQLAlchemy (Async), Telethon
* **媒体服务**: Sonarr, Radarr, Emby, Jellyfin
* **辅助工具**: qBittorrent, OpenAI, MKVToolNix (可选)

---

## 🚀 部署指南

### 方式一：Docker 部署 (推荐)

最简单、最稳定的部署方式，适合大多数用户。

1.  **创建目录与配置**
    创建 `docker-compose.yaml` 文件：

    ```yaml
    services:
      tellymeta:
        image: ghcr.io/gladtbam/tellymeta:latest
        container_name: tellymeta
        restart: unless-stopped
        ports:
          - "5080:5080"
        volumes:
          - ./data:/app/data
          - ./logs:/app/logs
          - ./templates:/app/templates # 挂载自定义通知模板
        environment:
          - TZ=Asia/Shanghai
        env_file:
          - .env
    ```

2.  **环境变量配置**
    创建 `.env` 文件并填入基础信息（参考下文配置说明）。

3.  **启动服务**
    ```bash
    docker compose up -d
    ```

---

### 方式二：Python 直接部署 (非 Docker)

适合开发者或习惯使用宿主机环境的用户。

**前置要求**：
* Python 3.11 或更高版本
* [uv](https://github.com/astral-sh/uv) (推荐) 或 pip

#### 1. 克隆代码
```bash
git clone https://github.com/gladtbam/TellyMeta.git
cd TellyMeta
```

#### 2. 安装依赖

**使用 uv (推荐，速度极快):**
```bash
# 同步环境并安装依赖
uv sync
```

**使用 pip:**
```bash
pip install .
```

#### 3. 配置环境
复制示例配置文件：
```bash
cp .env.example .env
```
编辑 `.env` 文件，填入你的 Telegram API 信息等。

#### 4. 运行
```bash
# 如果使用 uv
uv run main.py

# 如果使用 pip (确保在虚拟环境中)
python main.py
```

---

## ⚙️ 基础配置说明 (.env)

无论哪种部署方式，都需要配置 `.env` 文件。

```ini
# --- 基础配置 ---
log_level=INFO
# 你的时区
timezone=Asia/Shanghai
# 服务端口
port=5080

# --- Telegram 配置 (必填) ---
# 从 my.telegram.org 获取
telegram_api_id=123456
telegram_api_hash=your_api_hash
# 从 @BotFather 获取
telegram_bot_token=your_bot_token
# 你的 Bot 用户名 (带@)
telegram_bot_name=@YourBotName
# 管理员群组 ID (Bot 需在群内)
telegram_chat_id=-100xxxxxxxxxx 

# --- 媒体服务 (可选) ---
# 建议留空，Bot 启动后使用 /settings 指令在 Telegram 内可视化配置更方便。
```

---

## 📝 指令手册

### 👤 用户指令
| 指令 | 描述 |
| :--- | :--- |
| `/start` | 启动机器人：如果开启验证，需进行验证码验证 |
| `/me` | **个人中心**：查看积分、账号状态、求片、上传字幕 |
| `/checkin` | **每日签到**：获取积分（仅限群组内） |
| `/signup` | **注册账号**：仅在开放注册模式下可用 |
| `/code <code>` | **兑换码**：使用注册码注册或续期码续期 |
| `/help` | 获取帮助信息 |
| `/chat_id` | 获取群组 ID：需在群组中使用 |

### 👮 管理员指令
> 大部分管理指令支持**回复**某条消息来指定目标用户。

| 指令 | 描述 |
| :--- | :--- |
| `/settings` | **打开系统控制面板** (核心入口) |
| `/info` | 查看被回复用户的详细信息 (ID, 积分, 警告数) |
| `/warn` | 警告用户 (扣除积分) |
| `/kick` | 踢出用户并删除关联媒体账号 |
| `/ban` | 封禁用户并删除关联媒体账号 |
| `/del` | 仅删除用户的媒体账号 |
| `/change <分>` | 手动修改用户积分 (+加分 / -减分) |
| `/settle` | 手动触发活跃度积分结算 |

---

## 🔔 通知模板 (Webhooks)

TellyMeta 采用纯文件驱动的 Jinja2 模板系统。你可以在 `templates/` 目录下自定义通知样式。

支持的事件与模板文件对应关系：
* `request_submit.j2`: 用户提交求片
* `sonarr_download.j2`: 剧集下载完成
* `radarr_download.j2`: 电影下载完成
* `emby_library_new.j2`: 媒体入库通知
* ...更多事件请查阅文档。

配置 Sonarr/Radarr/Emby 的 Webhook 地址为：
`http://your-tellymeta-ip:5080/webhook/[sonarr|radarr|emby]?server_id=[ID]`

*(server_id 可在 `/settings` -> 服务器详情中查看)*

## 🤝 贡献

欢迎提交 Pull Request 或 Issue 来改进这个项目！

## 📄 许可证

[MIT License](LICENSE)