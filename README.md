# 🌌 TellyMeta

**TellyMeta** 是一个集成了 Emby/Jellyfin、Sonarr、Radarr、qBittorrent 与 Nginx 流量采集能力的 **Telegram 媒体库管理与社区运营机器人**。

旨在简化媒体库的日常运营工作，提供自动求片、AI 元数据翻译、通知推送、账号全生命周期托管，以及可独立自由组合的**积分、播放时长保活、流量配额与当期流量包**三大运营体系。

[功能特性](#-功能特性) • [部署指南](#-部署指南) • [数据目录与数据库](#-数据目录与数据库) • [指令手册](#-指令手册)

---

## ✨ 功能特性

### 🤖 自动化处理
* **多实例支持**：同时接管多个 Sonarr/Radarr 与 Emby/Jellyfin 服务端，支持媒体库绑定、路径映射及多话题（Forum Topic）分流通知。
* **智能求片系统**：支持按标题或 TMDB/TVDB ID 搜索与去重检测，各媒体服务器可独立配置求片开关与定价模式（`0` 积分免费求片、固定积分或按续费积分比例）。
* **AI 翻译与 NFO 维护**：集成 OpenAI 将元数据自动翻译为中文并缓存；支持 `movie.nfo.j2` / `tvshow.nfo.j2` / `episode.nfo.j2` 元数据重建与生成。
* **消息通知**：基于 Jinja2 沙箱模板推送求片审批、下载完成、入库与告警通知，支持自定义 HTML 与音视频规格展示；Webhook 支持 `X-Webhook-Token` 与 `Authorization: Bearer` 鉴权。
* **字幕助手与清理**：支持交互式 Zip 压缩包上传与剧集/电影自动匹配导入（含路径穿越与体积限制审计）；可选集成 MKVToolNix 自动清理内封多余字幕轨道。

### 💎 账号托管与三体系独立运营
> **积分、播放时长统计、Nginx 流量采集**支持各自独立开关与任意组合，关闭任一上层开关时保留持久化配置，并在前端界面、Bot 指令与私聊通知中自动隐藏已关闭能力。

* **自然月账期与事件驱动调度**：支持按服务器配置「有效自然月数」，基于账户 `anchor_day` 锚定自然月周期；采用 APScheduler 事件驱动精准调度器与每日兜底巡检，覆盖到期提醒、自动保活、过期封禁、删号清理及周期重置。
* **积分与 9 项细粒度子开关**：支持群组每日 `/checkin`（含连续签到奖励与幸运签到延寿）、活跃发言积分结算，以及动态中位数或固定续费积分模式；内置 **9 项细粒度子开关**（`签到积分`、`群聊发言积分`、`积分自注册`、`积分续期`、`积分求片`、`积分生成激活码`、`积分兑换流量包`、`刷屏扣分`、`警告扣分`），同时保持免费求片、激活码兑换与仅记次警告独立运作。
* **播放时长统计、观看度与自动保活**：通过播放状态机与 Emby 按需心跳精确统计有效播放时长；支持配置单服时长保活门槛与用户可控的「自动保活」开关。用户在账号详情页通过 **「观看度」百分比进度条**（未达标不虚高显示 `100%`）查看保活进度并免积分续期，管理端保留精确时长统计。
* **Nginx 流量配额、告警与当期流量包**：内置 asyncio UDP 监听接收 Nginx JSON syslog 报文，支持直连流、HLS/DASH 分片、下载流统计与 HLS 清单 `PlaySessionId` 预热归因；支持单服月基础流量配额（GiB）、80%/100% 超流告警及 **超流自动停用与安全解封**（与过期封禁严格隔离）。用户可按服兑换**当期流量包**，管理员可调整单账号**当期临时调额**，两者均在周期重置时清零，不跨周期囤积。
* **退群自动清理与入群验证**：支持退群用户媒体账号自动清理，以及入群图形验证码（含管理员邀请免验证）。

### 🛡️ 管理与安全
* **Telegram Mini App 管理后台**：支持在小程序内统一管理服务器连接与独立策略、系统开关与 9 项积分子开关、AI/元数据/调度参数、用户详情卡片（含单账号流量临时调额）等。
* **高级注册与外部验证**：支持默认（积分/邀请码）、开放、名额限制、限时开放、关闭及外部验证（自定义解析表达式）注册模式，并支持为各服务器配置用户协议（TOS）。
* **安全加强**：Webhook Token 鉴权、Jinja2 沙箱渲染、非交互关键动作幂等保护及静态资源 `no-cache` + `ETag` 强协商缓存。

## 🛠️ 技术栈

* **核心框架**: Python 3.15, FastAPI, SQLAlchemy (Async), Telethon, APScheduler
* **数据库**: SQLite (`aiosqlite`) / PostgreSQL (`asyncpg` + `psycopg`)，按需选择依赖
* **媒体与反代**: Sonarr, Radarr, Emby, Jellyfin, Nginx (JSON syslog UDP 流量上报)
* **辅助工具**: qBittorrent, OpenAI, MKVToolNix (可选)

---

## 🚀 部署指南

### 方式一：Docker 部署 (推荐)

最简单、最稳定的部署方式，适合大多数用户。

> [!NOTE]
> **镜像标签说明**：
> * `ghcr.io/gladtbam/tellymeta:latest`：内置 SQLite 与 PostgreSQL 全部数据库驱动。
> * `ghcr.io/gladtbam/tellymeta:sqlite`：仅内置 SQLite 驱动，体积更精简。
> * `ghcr.io/gladtbam/tellymeta:postgres`：仅内置 PostgreSQL 驱动。

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
          # - "5140:5140/udp" # 如需接收宿主机或其他节点发送的 Nginx syslog 流量日志可放开此 UDP 端口
        volumes:
          - ./data:/var/lib/tellymeta
          - ./logs:/var/log/tellymeta
          - ./templates:/app/templates # 挂载自定义通知模板
        environment:
          - TZ=Asia/Shanghai
        env_file:
          - .env
    ```

> [!WARNING]
> **从 v1 升级到 v2**：容器内默认数据与日志挂载路径已从 `/app/data`、`/app/logs` 变更为 `/var/lib/tellymeta` 与 `/var/log/tellymeta`。升级前请务必修改 `docker-compose.yaml` 中的挂载路径，否则将无法读取旧版数据库与 Session 文件。

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
* Python 3.15
* [uv](https://github.com/astral-sh/uv) (推荐) 或 pip

#### 1. 克隆代码
```bash
git clone https://github.com/gladtbam/TellyMeta.git
cd TellyMeta
```

#### 2. 安装依赖

> [!IMPORTANT]
> **从 v1 升级到 v2**：自 2.0.0 起，数据库驱动改为可选依赖（extras）。更新代码后不能再直接运行 `uv sync`，请根据所用数据库显式指定 `--extra` 或 `--all-extras`，否则会因缺少驱动无法启动。

**使用 uv (推荐，速度极快):**
```bash
# 仅使用 SQLite
uv sync --extra sqlite

# 仅使用 PostgreSQL
uv sync --extra postgres

# 同时安装 SQLite 与 PostgreSQL 驱动（如需执行数据库迁移）
uv sync --all-extras

# 生产部署若无需安装 ruff / pyright 等开发依赖，可追加 --no-dev
uv sync --no-dev --extra sqlite
```

**使用 pip:**
```bash
# 按需选择 [sqlite]、[postgres] 或 [sqlite,postgres]
pip install ".[sqlite]"
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

## 🗄️ 数据目录与数据库

### 默认数据与日志路径

2.0.0 起不再默认写入项目根目录下的 `./data` 与 `./logs`，而是遵循各操作系统标准路径（可通过 `.env` 中的 `data_dir` 与 `log_dir` 自定义覆盖）：

| 运行环境 | 默认数据目录 (`data_dir`) | 默认日志目录 (`log_dir`) |
| :--- | :--- | :--- |
| **Linux (`root` / Docker)** | `/var/lib/tellymeta` | `/var/log/tellymeta` |
| **Linux (非 root 用户 / XDG)** | `~/.local/share/tellymeta` | `~/.local/state/tellymeta/logs` |
| **macOS** | `~/Library/Application Support/TellyMeta` | `~/Library/Logs/TellyMeta` |
| **Windows** | `%LOCALAPPDATA%\TellyMeta\data` | `%LOCALAPPDATA%\TellyMeta\logs` |

> [!CAUTION]
> **从 v1 迁移非 Docker 部署数据时，请先停止 TellyMeta 服务并做好备份！**
>
> 可使用内置脚本将旧版 `./data` 与 `./logs` 迁移到当前系统标准目录：
> ```bash
> uv run python -m scripts.migrate_dirs
> ```
> 若使用 `systemd` 以特定用户（如 `jellyfin`）运行服务，请**以同一服务用户身份**执行迁移（或迁移后修正目录属主与权限），避免出现目录位置不一致或只读数据库错误；也可直接在 `.env` 中显式指定 `data_dir` 与 `log_dir` 沿用旧目录。

### 数据库配置与迁移工具

* **初始化 PostgreSQL（可选）**：如需使用 PostgreSQL，可运行交互式向导创建数据库、用户并自动写入 `.env` 中的 `database_url`：
  ```bash
  uv run python -m scripts.setup_postgres
  ```
* **SQLite ↔ PostgreSQL 数据迁移**：支持在两种数据库之间进行全表数据迁移（执行前请安装 `--all-extras`）：
  ```bash
  uv run python -m scripts.migrate_db
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

# --- 存储与数据库 (可选) ---
# 自定义数据目录与日志目录（留空则使用系统标准路径）
# data_dir=/var/lib/tellymeta
# log_dir=/var/log/tellymeta
# 数据库连接串（留空则默认使用 data_dir 下的 SQLite）
# PostgreSQL 示例: postgresql+asyncpg://user:password@localhost:5432/tellymeta
# database_url=

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

> [!IMPORTANT]
> 请通过反向代理（如 Nginx / NPM / Caddy）为 `telegram_webapp_url` 配置 **HTTPS** 证书，否则无法正常使用小程序功能。

## 🤖 小程序入口设置

在 `@BotFather` 中选择 `telegram_bot_name` 对应的机器人，进入 `Mini Apps` -> `Main App`（以及可选的 `Menu Button`），将 `URL` 填入 `telegram_webapp_url/webapp/miniapp.html`。

小程序采用按功能拆分的轻量页面结构（`static/`）：
* **个人中心 (`miniapp.html`)**：展示当前启用体系的个人状态摘要、兑换码入口、已注册媒体账号概览（服务器信息、用户协议与管理入口）及可注册媒体服务列表。
* **账号详情 (`account.html`)**：查看媒体用户名、到期日期、观看度百分比进度条、当期流量与流量包余额、下次重置日期，以及自动保活、续期、兑换流量包、生成邀请码、求片、字幕上传与注销操作。
* **求片页面 (`request.html`)**：搜索媒体并按所属服务器独立求片规则提交请求。
* **管理配置与用户卡片 (`settings.html` / `user_info.html`)**：管理员配置系统与三体系开关（含 9 项积分子开关）、媒体服务器与独立策略、AI/元数据/调度参数，以及单用户管理与当期流量临时调额。

> [!TIP]
> 群组内使用 `/info` 打开用户详情卡片依赖 `Main App` 入口（`startapp` 参数跳转），请确保 `Main App` 已正确配置。

### Nginx 配置

#### 1. 小程序与 Webhook 反向代理
```nginx
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

#### 2. 流媒体流量采集 JSON syslog 上报（可选）
若在管理后台开启了「Nginx 流量采集与配额」（默认监听 `0.0.0.0:5140/udp`，可在设置中调整监听地址、端口与内存刷盘间隔），请在 Nginx 主配置的 `http` 块中定义 `log_format`，并在 Emby/Jellyfin 对应的 `server`（或 `location`）块启用 `access_log syslog:...` 推送。

TellyMeta 会按 **`server_id` -> `server_name`（后台配置的服务器名称） -> `host`（反代域名与后台 URL 匹配）** 的顺序自动归属服务器；单服默认可直接按 `host` 匹配：

```nginx
# 在 http {} 中定义（默认由 host 自动匹配后台对应服务器）
log_format tellymeta_traffic escape=json
    '{"timestamp":"$time_iso8601",'
    '"http_ip":"$remote_addr",'
    '"method":"$request_method",'
    '"url":"$request_uri",'
    '"status":$status,'
    '"body_bytes_sent":$body_bytes_sent,'
    '"host":"$http_host",'
    '"http_range":"$http_range",'
    '"http_user_agent":"$http_user_agent",'
    '"http_x_emby_token":"$http_x_emby_token",'
    '"http_x_mediabrowser_token":"$http_x_mediabrowser_token",'
    '"http_x_emby_device_id":"$http_x_emby_device_id",'
    '"http_x_mediabrowser_device_id":"$http_x_mediabrowser_device_id",'
    '"http_authorization":"$http_authorization",'
    '"http_x_emby_authorization":"$http_x_emby_authorization"}';

# 在对应 Emby/Jellyfin 的 server {} 块中启用：
access_log syslog:server=127.0.0.1:5140,facility=local7,tag=tellymeta tellymeta_traffic;
```

若单台 Nginx 反代多台媒体服务器，且外网域名与后台内网地址不一致或需显式指定归属，可在 JSON 中额外附带 `server_id` 或 `server_name`：

```nginx
# 方式一（推荐）：在 http {} 用 map 按域名映射到 TellyMeta 后台服务器 ID，共用同一份 log_format
map $http_host $tellymeta_server_id {
    default "";
    "emby1.example.com"    "1";
    "jellyfin.example.com" "2";
}

log_format tellymeta_multi escape=json
    '{"timestamp":"$time_iso8601",'
    '"server_id":"$tellymeta_server_id",'
    '"http_ip":"$remote_addr",'
    '"method":"$request_method",'
    '"url":"$request_uri",'
    '"status":$status,'
    '"body_bytes_sent":$body_bytes_sent,'
    '"host":"$http_host",'
    '"http_range":"$http_range",'
    '"http_user_agent":"$http_user_agent",'
    '"http_x_emby_token":"$http_x_emby_token",'
    '"http_x_mediabrowser_token":"$http_x_mediabrowser_token",'
    '"http_x_emby_device_id":"$http_x_emby_device_id",'
    '"http_x_mediabrowser_device_id":"$http_x_mediabrowser_device_id",'
    '"http_authorization":"$http_authorization",'
    '"http_x_emby_authorization":"$http_x_emby_authorization"}';

# 在各个媒体站点的 server {} 块中引用：
# access_log syslog:server=127.0.0.1:5140,facility=local7,tag=tellymeta tellymeta_multi;

# 方式二：若多服共享同域名或需按 server_name 标识，可在 http {} 为各服分别声明固定格式
log_format tellymeta_emby1 escape=json
    '{"timestamp":"$time_iso8601",'
    '"server_id":1,'
    '"server_name":"Emby主服",'
    '"http_ip":"$remote_addr",'
    '"method":"$request_method",'
    '"url":"$request_uri",'
    '"status":$status,'
    '"body_bytes_sent":$body_bytes_sent,'
    '"host":"$http_host",'
    '"http_range":"$http_range",'
    '"http_user_agent":"$http_user_agent",'
    '"http_x_emby_token":"$http_x_emby_token",'
    '"http_x_mediabrowser_token":"$http_x_mediabrowser_token",'
    '"http_x_emby_device_id":"$http_x_emby_device_id",'
    '"http_x_mediabrowser_device_id":"$http_x_mediabrowser_device_id",'
    '"http_authorization":"$http_authorization",'
    '"http_x_emby_authorization":"$http_x_emby_authorization"}';

# 在对应 server {} 或 location {} 块中单独指定：
# access_log syslog:server=127.0.0.1:5140,facility=local7,tag=tellymeta tellymeta_emby1;
```
---

## 📝 指令手册

### 👤 用户指令
> 群组 `/help` 与指令可用性会随全局开关及积分细粒度子开关自动隐藏已禁用能力。

| 指令 | 描述 |
| :--- | :--- |
| `/start` | 启动机器人：若开启入群验证且未通过，发送图形验证码挑战；已通过则展示帮助信息 |
| `/checkin` | **每日签到**：获取签到与连签/幸运奖励（仅限群组内，随「签到积分」开关动态显示） |
| `/help` | 获取当前可用指令帮助信息（私聊使用） |
| `/chat_id` | 获取当前群组 ID：需在群组中使用 |

### 👮 管理员指令
> 大部分用户管控指令支持**回复**目标用户的消息执行。

| 指令 | 描述 |
| :--- | :--- |
| `/info` | 查看被回复用户的详细信息（私聊与群组支持打开小程序卡片，无 WebApp 地址时自动降级为文本展示） |
| `/warn` | 警告用户（独立累计警告次数；当「全局积分」与「警告扣分」子开关均开启时连带扣除积分） |
| `/kick` | 踢出群组并删除关联媒体账号 |
| `/ban` | 仅在群组中封禁用户 |
| `/del` | 仅删除用户的媒体账号 |
| `/change <分>` | 手动修改用户积分 (`+` 加分 / `-` 减分，需开启全局积分) |
| `/settle` | 手动触发活跃度积分结算（需开启全局积分） |
| `/bind <server_id> <notify\|request>` | 在论坛群组的具体**话题 (Topic)** 内发送，将指定服务器的常规通知或求片通知绑定到当前话题 |

---

## 🔔 通知模板 (Webhooks)

TellyMeta 采用纯文件驱动的沙箱化 Jinja2 模板系统，你可以在 `templates/` 目录下自定义通知与 NFO 模板：

* 求片通知：`request_submit.j2`、`request_approved.j2`、`request_completed.j2`、`request_rejected.j2`
* 下载与入库通知：`sonarr_series_add.j2`、`sonarr_download.j2`、`radarr_movie_add.j2`、`radarr_download.j2`、`emby_library_new.j2`、`jellyfin_library_new.j2`
* 元数据 NFO 模板：`movie.nfo.j2`、`tvshow.nfo.j2`、`episode.nfo.j2`

Webhook 地址及鉴权请求头（`X-Webhook-Token` / `Authorization: Bearer`）可在「小程序 -> 系统设置 -> 服务器详情」中一键复制。

## 🔌 上游 API 版本与模型维护

TellyMeta 对接的媒体服务器 REST API 模型（`models/radarr.py`、`models/sonarr.py`、`models/jellyfin.py`、`models/emby.py`）由 [scripts/generate_openapi_models.py](scripts/generate_openapi_models.py) 基于各服务官方 OpenAPI 文档按需裁剪并自动生成；各服务的 Webhook 回调模型则独立保存在 `models/*_webhook.py` 中手写维护。

### 当前适配的 API 版本

| 服务 | OpenAPI 标题 | 当前适配版本 | OpenAPI 规范 | 上游文档来源 |
| :--- | :--- | :--- | :--- | :--- |
| **Radarr** | `Radarr` | `3.0.0` (API v3) | `3.0.4` | [Radarr openapi.json](https://raw.githubusercontent.com/Radarr/Radarr/develop/src/Radarr.Api.V3/openapi.json) |
| **Sonarr** | `Sonarr` | `3.0.0` (API v3) | `3.0.1` | [Sonarr openapi.json](https://raw.githubusercontent.com/Sonarr/Sonarr/main/src/Sonarr.Api.V3/openapi.json) |
| **Jellyfin** | `Jellyfin API` | `12.4.0` (Stable) | `3.0.4` | [Jellyfin openapi-stable.json](https://api.jellyfin.org/openapi/jellyfin-openapi-stable.json) |
| **Emby** | `Emby Server REST API` | `4.10.2.0` | `3.0.1` | [Emby openapi_v3.json](https://raw.githubusercontent.com/MediaBrowser/Emby.SDK/refs/heads/master/Resources/OpenApi/openapi_v3.json) |

### 模型同步与自动化

* **自动同步 (GitHub Actions)**：仓库配置了 [.github/workflows/sync-openapi-models.yml](.github/workflows/sync-openapi-models.yml)，每周自动检测上游 OpenAPI 变更并创建更新 PR，也支持在 Actions 页面手动触发（可选全部或单个服务）。
* **本地手动更新**（需可用 `uvx` 或 `datamodel-codegen`）：
  ```bash
  # 使用内置官方 OpenAPI 地址一键更新指定服务模型
  uv run python -m scripts.generate_openapi_models radarr
  uv run python -m scripts.generate_openapi_models sonarr
  uv run python -m scripts.generate_openapi_models jellyfin
  uv run python -m scripts.generate_openapi_models emby
  ```

### 开发依赖与代码检查

`pyproject.toml` 的 `[dependency-groups] dev` 中配置了 `ruff` 与 `pyright`（`uv sync` 默认安装，生产环境可通过 `--no-dev` 跳过）：

```bash
# 类型检查（已在 pyproject.toml 配置 venvPath，无需手动指定解释器）
uv run pyright

# 代码 Lint 与格式化
uv run ruff check .
uv run ruff format .
uv run ruff format --check .
```

---

## 🤝 贡献

欢迎提交 Pull Request 或 Issue 来改进这个项目！

## 📄 许可证

[MIT License](LICENSE)