import os
import pathlib
import re
import shutil
import zipfile
from collections.abc import Callable

import aiofiles.tempfile
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.config import get_settings
from repositories.config_repo import ConfigRepository
from services.user_service import Result

settings = get_settings()

ALLOWED_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup'}
MAX_SINGLE_FILE_SIZE = 20 * 1024 * 1024
MAX_TOTAL_EXTRACT_SIZE = 50 * 1024 * 1024

class SubtitleService:
    def __init__(
        self,
        session: AsyncSession,
        radarr_clients: dict[int, RadarrClient],
        sonarr_clients: dict[int, SonarrClient]
    ):
        self.config_repo = ConfigRepository(session)
        self.radarr_clients: dict[int, RadarrClient] = radarr_clients
        self.sonarr_clients: dict[int, SonarrClient] = sonarr_clients

    async def handle_file_upload(self, user_id: int, file_path: str, file_name: str) -> Result:
        """处理字幕文件上传（入口分发）"""
        # 1. 检查文件名格式
        match = re.search(r'^(tvdb|tmdb)-(\d+)\.zip$', file_name, re.IGNORECASE)
        if not match:
            return Result(False, "文件名格式错误。请使用 `tvdb-ID.zip` 或 `tmdb-ID.zip` 命名 (例如 `tvdb-842675.zip`)。")

        media_type = match.group(1).lower()
        media_id = int(match.group(2))

        logger.info(f"处理字幕上传: 用户={user_id}, 类型={media_type}, ID={media_id}")

        try:
            # 2. 根据类型分发处理
            if media_type == 'tvdb':
                return await self._handle_series(user_id, media_id, file_path)
            elif media_type == 'tmdb':
                return await self._handle_movie(user_id, media_id, file_path)
        except (OSError, ValueError, TypeError) as e:
            logger.exception(f"处理字幕时发生系统错误: {e}")
            return Result(False, f"处理过程中发生系统错误: {str(e)}")

        return Result(False, "不支持的媒体类型")

    async def _handle_series(self, user_id: int, tvdb_id: int, zip_path: str) -> Result:
        """处理剧集字幕 (Sonarr)"""
        target_client = None
        series = None

        for client in self.sonarr_clients.values():
            try:
                series = await client.get_series_by_tvdb(tvdb_id)
                if series and series.id:
                    target_client = client
                    break
            except Exception as e:
                logger.warning(f"查询 Sonarr 实例失败: {e}")
                continue

        if not target_client or not series or not series.id:
            return Result(False, f"未在任何已启用的 Sonarr 实例中找到 TVDB ID 为 {tvdb_id} 的剧集。")

        episodes = await target_client.get_episode_by_series_id(series.id)
        if not episodes:
            return Result(False, "未找到该剧集的集数信息。")

        # 建立映射: S{season}E{episode} -> EpisodeFile Path
        episode_map = {}
        for ep in episodes:
            if ep.hasFile and ep.episodeFile and ep.episodeFile.path:
                key = f"S{ep.seasonNumber}E{ep.episodeNumber}"
                episode_map[key] = ep.episodeFile.path

        return await self._extract_and_process(
            zip_path,
            series.title,
            lambda f: self._process_series_file(f, episode_map)
        )

    def _process_series_file(self, sub_file_path: str, episode_map: dict[str, str]) -> str | None:
        """剧集单文件处理逻辑：返回错误信息或 None(成功)"""
        sub_filename = os.path.basename(sub_file_path)

        # 匹配 SxxExx
        ep_match = re.search(r'[sS](\d+)[eE](\d+)', sub_filename)
        if not ep_match:
            return f"忽略 {sub_filename}：文件名未包含 SxxExx 格式"

        season_num = int(ep_match.group(1))
        episode_num = int(ep_match.group(2))
        key = f"S{season_num}E{episode_num}"

        if key not in episode_map:
            return f"已跳过 {sub_filename}：媒体库中未找到 {key} 对应的视频文件"

        media_path = episode_map[key]
        media_dir = os.path.dirname(media_path)
        # 获取媒体文件的基础名称 (无后缀)，例如 "Show.S01E01"
        media_basename = os.path.splitext(os.path.basename(media_path))[0]

        # 截取 SxxExx 之后的部分，例如 ".zh.comment.ass" 或 " - Title.zh.ass"
        remainder = sub_filename[ep_match.end():]
        suffix_match = re.search(r'(?:\.[^.]+)+$', remainder)
        if suffix_match:
            suffixes = suffix_match.group()
        else:
            # 兜底：如果没提取到后缀，直接取原文件后缀
            suffixes = "".join(pathlib.Path(sub_filename).suffixes)

        if not self._is_allowed_extension(suffixes):
            return f"跳过 {sub_filename}: 不支持的字幕格式"

        new_sub_name = f"{media_basename}{suffixes}"
        new_sub_path = os.path.join(media_dir, new_sub_name)

        return self._safe_move(sub_file_path, new_sub_path)

    async def _handle_movie(self, user_id: int, tmdb_id: int, zip_path: str) -> Result:
        """处理电影字幕 (Radarr)"""
        # 1. 在所有 Radarr 实例中查找
        target_client = None
        movie = None

        for client in self.radarr_clients.values():
            try:
                movie = await client.get_movie_by_tmdb(tmdb_id)
                if movie and movie.id:
                    target_client = client
                    break
            except Exception as e:
                logger.warning(f"查询 Radarr 实例失败: {e}")
                continue

        if not target_client or not movie:
            return Result(False, f"未在任何已启用的 Radarr 实例中找到 TMDB ID 为 {tmdb_id} 的电影。")

        if not movie.hasFile or not movie.movieFile or not movie.movieFile.path:
            return Result(False, "该电影在库中尚无视频文件，无法上传字幕。")

        movie_path = movie.movieFile.path
        media_dir = os.path.dirname(movie_path)
        media_basename = os.path.splitext(os.path.basename(movie_path))[0]

        # 2. 解压并处理
        return await self._extract_and_process(
            zip_path,
            movie.title,
            lambda f: self._process_movie_file(f, media_dir, media_basename)
        )

    def _process_movie_file(self, sub_file_path: str, media_dir: str, media_basename: str) -> str | None:
        """电影单文件处理逻辑"""
        sub_filename = os.path.basename(sub_file_path)
        # 获取所有后缀 (如 .chi.srt)
        suffixes = "".join(pathlib.Path(sub_filename).suffixes)
        if not suffixes:
            suffixes = ".srt"

        if not self._is_allowed_extension(suffixes):
            return f"跳过 {sub_filename}: 不支持的字幕格式"

        new_sub_name = f"{media_basename}{suffixes}"
        new_sub_path = os.path.join(media_dir, new_sub_name)

        return self._safe_move(sub_file_path, new_sub_path)

    def _is_allowed_extension(self, suffix_str: str) -> bool:
        """检查后缀是否在白名单中 (检查最后一个点后的部分)"""
        if not suffix_str:
            return False
        ext = pathlib.Path(suffix_str).suffix.lower()
        return ext in ALLOWED_EXTENSIONS

    def _safe_move(self, src: str, dst: str) -> str | None:
        """安全移动文件：检查存在性、防止覆盖、设置权限"""
        if os.path.exists(dst):
            return f"跳过: 目标位置已存在文件 {os.path.basename(dst)}"

        try:
            shutil.move(src, dst)
            os.chmod(dst, 0o644)
            return None
        except OSError as e:
            return f"文件移动失败: {e}"

    async def _extract_and_process(
        self,
        zip_path: str,
        media_title: str | None,
        process_func: Callable[[str], str | None]) -> Result:
        """通用解压和遍历逻辑"""
        async with aiofiles.tempfile.TemporaryDirectory() as temp_dir:
            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    total_size = 0
                    for zinfo in zip_ref.infolist():
                        if zinfo.filename.startswith('/') or '..' in zinfo.filename:
                            return Result(False, f"发现不安全的文件路径: {zinfo.filename}")

                        if zinfo.file_size > MAX_SINGLE_FILE_SIZE:
                            return Result(False, f"文件 {zinfo.filename} 过大 (超过 {MAX_SINGLE_FILE_SIZE//1024//1024}MB)")

                        total_size += zinfo.file_size

                    if total_size > MAX_TOTAL_EXTRACT_SIZE:
                        return Result(False, f"压缩包解压后总大小过大 (超过 {MAX_TOTAL_EXTRACT_SIZE//1024//1024}MB)")

                    zip_ref.extractall(temp_dir)
            except zipfile.BadZipFile:
                return Result(False, "无效的 Zip 文件。")

            files_processed = 0
            errors = []

            for root, _, files in os.walk(temp_dir):
                for file in files:
                    if file.startswith('.') or file.startswith('__MACOSX'):
                        continue

                    full_path = os.path.join(root, file)
                    try:
                        error = process_func(full_path)
                        if error:
                            errors.append(error)
                        else:
                            files_processed += 1
                    except (OSError, ValueError) as e:
                        errors.append(f"处理文件 {file} 时出错: {str(e)}")

            if files_processed == 0 and not errors:
                return Result(False, "压缩包内未找到有效文件。")

            msg = f"✅ **字幕处理完成**\n🎬 媒体: {media_title}\n📥 成功上传: {files_processed} 个文件"
            if errors:
                msg += "\n\n⚠️ **部分错误**:\n" + "\n".join(errors[:5])
                if len(errors) > 5:
                    msg += f"\n...等 {len(errors)} 个错误"

            return Result(True, msg)
