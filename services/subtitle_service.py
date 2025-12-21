import os
import pathlib
import re
import shutil
import zipfile

import aiofiles.tempfile
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.config import get_settings
from core.telegram_manager import TelethonClientWarper
from repositories.config_repo import ConfigRepository
from services.user_service import Result

settings = get_settings()

class SubtitleService:
    def __init__(self, app: FastAPI, session: AsyncSession):
        self.config_repo = ConfigRepository(session)
        self._sonarr_client = app.state.sonarr_client
        self._radarr_client = app.state.radarr_client
        self.client: TelethonClientWarper = app.state.telethon_client

    @property
    def sonarr_client(self) -> SonarrClient:
        if self._sonarr_client is None:
            raise RuntimeError("Sonarr 客户端未配置")
        return self._sonarr_client

    @property
    def radarr_client(self) -> RadarrClient:
        if self._radarr_client is None:
            raise RuntimeError("Radarr 客户端未配置")
        return self._radarr_client

    async def handle_file_upload(self, user_id: int, file_path: str, file_name: str) -> Result:
        """处理字幕文件上传
        Args:
            user_id: 用户 ID
            file_path: 文件路径
            file_name: 文件名
        Returns:
            Result: 处理结果
        """
        # Check filename for ID and Type
        # Expected: tvdb-12345.zip or tmdb-12345.zip
        match = re.search(r'^(tvdb|tmdb)-(\d+)\.zip$', file_name, re.IGNORECASE)
        if not match:
            return Result(False, "文件名格式错误。请使用 `tvdb-ID.zip` 或 `tmdb-ID.zip` 命名 (例如 `tvdb-842675.zip`)。")

        media_type = match.group(1).lower()
        media_id = int(match.group(2))
        logger.info("正在处理用户 {}: 类型 {} - media_id {} 的字幕上传", user_id, media_type, media_id)

        try:
            async with aiofiles.tempfile.TemporaryDirectory(prefix=f"sub_upload_{user_id}_") as temp_dir:
                # Unzip
                try:
                    with zipfile.ZipFile(file_path, 'r') as zip_ref:
                        zip_ref.extractall(temp_dir)
                except zipfile.BadZipFile:
                    return Result(False, "无效的 Zip 文件。")

                files_processed = 0
                errors = []

                # 处理提取的文件
                extracted_files = []
                for root, _, files in os.walk(temp_dir):
                    for file in files:
                        # 忽略隐藏文件或 macOS 元数据
                        if file.startswith('.') or file.startswith('__MACOSX'):
                            continue
                        extracted_files.append(os.path.join(root, file))

                if not extracted_files:
                    return Result(False, "压缩包为空。")

                if media_type == 'tvdb': # Sonarr
                    # TVDB ID -> Series -> Episodes
                    series = await self.sonarr_client.get_series_by_tvdb(media_id)
                    if not series or not series.id:
                        return Result(False, f"在 Sonarr 中未找到 TVDB ID 为 {media_id} 的剧集。")

                    episodes = await self.sonarr_client.get_episode_by_series_id(series.id)
                    if not episodes:
                        return Result(False, "未找到该剧集的剧集信息。")

                    # Create map: S{season}E{episode} -> EpisodeFile Path
                    episode_map = {}
                    for ep in episodes:
                        if ep.hasFile and ep.episodeFile and ep.episodeFile.path:
                            key = f"S{ep.seasonNumber}E{ep.episodeNumber}"
                            episode_map[key] = ep.episodeFile.path

                    for sub_file_path in extracted_files:
                        sub_filename = os.path.basename(sub_file_path)
                        # Match SxxExx
                        ep_match = re.search(r'[sS](\d+)[eE](\d+)', sub_filename)
                        if not ep_match:
                            errors.append(f"忽略 {sub_filename}：未找到 SxxExx")
                            continue

                        season_num = int(ep_match.group(1))
                        episode_num = int(ep_match.group(2))
                        key = f"S{season_num}E{episode_num}"

                        if key not in episode_map:
                            errors.append(f"已跳过 {sub_filename}：未找到与 {key} 匹配的剧集文件")
                            continue

                        media_path = episode_map[key]
                        media_dir = os.path.dirname(media_path)
                        media_basename = os.path.splitext(os.path.basename(media_path))[0]

                        # Extract suffix
                        suffix = sub_filename[ep_match.end():]

                        new_sub_name = f"{media_basename}{suffix}"
                        new_sub_path = os.path.join(media_dir, new_sub_name)

                        try:
                            shutil.move(sub_file_path, new_sub_path)
                            os.chmod(new_sub_path, 0o644)
                            files_processed += 1
                        except Exception as e:
                            errors.append(f"移动 {sub_filename} 时出错：{str(e)}")

                elif media_type == 'tmdb': # Radarr
                    # TMDB ID -> Movie -> MovieFile
                    movie = await self.radarr_client.get_movie_by_tmdb(media_id)
                    if not movie:
                        return Result(False, f"在 Radarr 中未找到 TMDB ID 为 {media_id} 的电影。")

                    if not movie.hasFile or not movie.movieFile or not movie.movieFile.path:
                        return Result(False, "该电影尚无文件。")

                    movie_path = movie.movieFile.path
                    media_dir = os.path.dirname(movie_path)
                    media_basename = os.path.splitext(os.path.basename(movie_path))[0]

                    for sub_file_path in extracted_files:
                        sub_filename = os.path.basename(sub_file_path)
                        # For movies, just append the suffixes
                        suffixes = "".join(pathlib.Path(sub_filename).suffixes)
                        if not suffixes:
                            # Fallback if no extension
                            suffixes = ".srt"

                        new_sub_name = f"{media_basename}{suffixes}"
                        new_sub_path = os.path.join(media_dir, new_sub_name)

                        try:
                            shutil.move(sub_file_path, new_sub_path)
                            os.chmod(new_sub_path, 0o644)
                            files_processed += 1
                        except Exception as e:
                            errors.append(f"Error moving {sub_filename}: {str(e)}")

            # Summary
            msg = f"✅ 处理完成。\n成功上传: {files_processed} 个文件。"
            if errors:
                msg += "\n\n⚠️ 部分错误:\n" + "\n".join(errors[:5])
                if len(errors) > 5:
                    msg += f"\n...等 {len(errors)} 个错误"

            return Result(True, msg)

        except Exception as e:
            logger.error(f"Subtitle processing error: {e}")
            return Result(False, f"处理出错: {str(e)}")
