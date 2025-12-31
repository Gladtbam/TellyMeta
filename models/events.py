from enum import StrEnum

class NotificationEvent(StrEnum):
    """
    系统内部通知事件枚举
    对应 templates/ 目录下的 .j2 文件名
    """
    # --- 求片系统事件 ---
    REQUEST_SUBMIT = "request_submit"       # 用户提交求片
    REQUEST_APPROVED = "request_approved"   # 管理员批准
    REQUEST_REJECTED = "request_rejected"   # 管理员拒绝
    REQUEST_COMPLETED = "request_completed" # 影片入库/完成 (发给用户)

    # --- Sonarr Webhook ---
    SONARR_DOWNLOAD = "sonarr_download"     # 下载/导入完成
    SONARR_SERIES_ADD = "sonarr_series_add" # 新增剧集

    # --- Radarr Webhook ---
    RADARR_DOWNLOAD = "radarr_download"     # 下载/导入完成
    RADARR_MOVIE_ADD = "radarr_movie_add"   # 新增电影

    # --- Emby/Jellyfin Webhook ---
    EMBY_LIBRARY_NEW = "emby_library_new"   # 新增媒体
    JELLYFIN_LIBRARY_NEW = "jellyfin_library_new"

    # --- 系统事件 ---
    SYSTEM_TEST = "system_test"
