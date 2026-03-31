# 🌌 TellyMeta

**TellyMeta** 是一个集成了 Emby/Jellyfin、Sonarr 和 Radarr 的 **Telegram 媒体库管理机器人**。

旨在简化媒体库的日常运营工作，提供自动求片、通知推送、账号管理以及积分社区等功能，帮助管理员更高效地维护媒体库与用户社区。

[功能特性](#-功能特性) • [部署指南](#-部署指南) • [指令手册](#-指令手册)

---

## ✨ 功能特性

### 🤖 自动化处理
* **多实例支持**：同时接管多个 Sonarr/Radarr 和 Emby/Jellyfin 服务端。
* **智能求片系统**：支持按标题或 ID（TMDB/TVDB）搜索，支持去重检测。
* **AI 翻译与缓存**：集成 OpenAI，自动将 TMDB/TVDB 的元数据翻译为中文。内置 API 缓存机制，显著提升元数据加载速度并节省额度。支持速率限制以防止 API 封禁。
* **消息通知**：实时推送下载完成、入库通知，采用 Jinja2 沙箱模板系统，支持 HTML 自定义，可展示 HDR/Dolby 等媒体信息。
* **字幕助手**：支持交互式 Zip 压缩包上传，Bot 自动识别对应的剧集/电影并重命名导入。具备完善的安全检测流程（路径溢出、大小限制）。

### 💎 用户与积分
* **积分体系**：通过群组签到、活跃发言获取积分，用于兑换注册邀请码、账号续期或**抵扣求片费用**。
* **账号托管**：Emby/Jellyfin 账号的自动注册、密码重置、有效期管理以及到期自动封禁。
* **自动清理机制**：**自动识别并注销已离开 Telegram 群组用户的媒体库账号**，确保资源被有效分配给活跃用户。
* **入群验证**：内置图形验证码。支持管理员邀请**免验证**功能，提升人工邀请的流畅度。

### 🛡️ 管理与安全
* **可视化配置**：通过 **小程序** 即可在 Telegram 界面管理服务器连接、通知渠道、注册模式、NSFW 策略等，无需修改配置文件。
* **高级注册模式**：支持积分、限额、限时、外部验证（支持自定义 JSON/正则解析器）等多种入库门槛。
* **安全加强**：Webhooks 支持 Token 鉴权，模板渲染采用安全沙箱环境，文件上传具备严格审计。
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

#### 5. systemd 服务

```bash
cat << EOF | sudo tee /etc/systemd/system/tellymeta.service
[Unit]
Description=TellyMeta
After=syslog.target network.target

[Service]
WorkingDirectory=/opt/TellyMeta
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
User=<建议使用 Emby/Jellyfin 服务用户>
Group=<建议使用 Emby/Jellyfin 服务用户组>
UMask=0002
Restart=on-failure
RestartSec=5
Type=simple
ExecStart=uv run main.py
SuccessExitStatus=143

[Install]
WantedBy=multi-user.target
EOF
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tellymeta
```

---

## ⚙️ 基础配置说明 (.env)

无论哪种部署方式，都需要配置 `.env` 文件。

```ini
# --- 基础配置 ---
# 日志级别 (DEBUG/INFO/WARNING/ERROR)
log_level=INFO
# 服务运行端口
port=5080
# 你的时区
timezone=Asia/Shanghai
# 代理设置 (可选)
# 格式: socks5://user:password@host:port / http://user:password@host:port
proxy=socks5://user:password@host:port

# --- Telegram 配置 (必填) ---
# 从 https://my.telegram.org 获取
telegram_api_id=123456
telegram_api_hash=your_api_hash
# 从 @BotFather 获取
telegram_bot_token=your_bot_token
# 你的 Bot 用户名 (必需带 @，例如 @YourTellyBot)
telegram_bot_name=@YourBotName
# 管理员中心群组 ID (Bot 需加入该群组并通过 /chat_id 获取)
telegram_chat_id=-100xxxxxxxxxx
# 你的外网 HTTPS 访问地址 (用于开启 Telegram 核心设置面板，必需项)
# 例如: https://telly.yourdomain.com (结尾不要加斜杠)
telegram_webapp_url=https://your-public-domain.com

```

> **⚠️ 重要提示**: 
> 通过反向代理（如 Nginx / NPM / Caddy）为 `telegram_webapp_url` 配置 **HTTPS** 证书，否则无法正常使用小程序功能。

## 🤖 小程序入口设置

Telegram BotFather 选择 `telegram_bot_name` 设置的机器人，点击 `Mini Apps` -> `Menu Button` 或 `Main App` -> `URL` 填入 `telegram_webapp_url/webapp/miniapp.html`。

### Nginx 配置

```
location / {
        # 代理到 TellyMeta 的端口 (默认为 5080)
        # 如果 Nginx 和 TellyMeta 在同一台宿主机，使用 127.0.0.1
        # 如果在 Docker 网络内部互通，使用容器名 (例如 http://tellymeta:5080)
        proxy_pass http://127.0.0.1:5080;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
```
---

## 📝 指令手册

### 👤 用户指令
| 指令 | 描述 |
| :--- | :--- |
| `/start` | 启动机器人：如果开启验证，需进行验证码验证 |
| `/checkin` | **每日签到**：获取积分（仅限群组内） |
| `/help` | 获取帮助信息 |
| `/chat_id` | 获取群组 ID：需在群组中使用 |

### 👮 管理员指令
> 大部分管理指令支持**回复**某条消息来指定目标用户。

| 指令 | 描述 |
| :--- | :--- |
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
* ...更多事件请查看 `templates/` 目录。

配置 Sonarr/Radarr/Emby/Jellyfin 的 Webhook 地址为：

* 小程序 -> 服务器详情中查看。

## 🤝 贡献

欢迎提交 Pull Request 或 Issue 来改进这个项目！

## 📄 许可证

[MIT License](LICENSE)