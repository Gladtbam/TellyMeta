import re
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path

import aiofiles.tempfile
from httpx2 import HTTPError
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from clients.radarr_client import RadarrClient
from clients.sonarr_client import SonarrClient
from core.config import get_settings
from models.radarr import MovieResource
from models.sonarr import SeriesResource
from repositories.binding_repo import BindingRepository
from repositories.config_repo import ConfigRepository
from repositories.server_repo import ServerRepository
from services.user_service import Result

settings = get_settings()

ALLOWED_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
MAX_SINGLE_FILE_SIZE = 20 * 1024 * 1024
MAX_TOTAL_EXTRACT_SIZE = 50 * 1024 * 1024


class SubtitleService:
    # 允许 Telegram 重复发送/转存时自动附加的序号后缀，如 tvdb-123 (1).zip、tmdb-456(2).zip
    ZIP_NAME_PATTERN = re.compile(
        r"^(tvdb|tmdb)-(\d+)(?:\s*\(\d+\))*\.zip$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        session: AsyncSession,
        radarr_clients: dict[int, RadarrClient],
        sonarr_clients: dict[int, SonarrClient],
    ):
        self.config_repo = ConfigRepository(session)
        self.binding_repo = BindingRepository(session)
        self.server_repo = ServerRepository(session)
        self.radarr_clients: dict[int, RadarrClient] = radarr_clients
        self.sonarr_clients: dict[int, SonarrClient] = sonarr_clients

    @staticmethod
    def parse_subtitle_zip_filename(file_name: str) -> tuple[str, int] | None:
        """解析字幕压缩包文件名中的媒体类型与外部 ID，兼容 Telegram 自动重命名后缀"""
        match = SubtitleService.ZIP_NAME_PATTERN.search((file_name or "").strip())
        if not match:
            return None
        return match.group(1).lower(), int(match.group(2))

    async def _get_bound_arr_ids(self, media_server_id: int) -> list[int]:
        """获取指定媒体服务器已绑定的去重 Arr 实例 ID 列表"""
        bindings = await self.binding_repo.get_by_media_id(media_server_id)
        return list(dict.fromkeys(b.arr_id for b in bindings))

    async def _format_arr_labels(self, arr_ids: list[int]) -> list[tuple[int, str]]:
        """获取 Arr 实例 ID 与可读名称列表，用于多实例匹配时提示明确目标"""
        labels: list[tuple[int, str]] = []
        for arr_id in arr_ids:
            server = await self.server_repo.get_by_id(arr_id)
            name = server.name if server else f"Arr#{arr_id}"
            labels.append((arr_id, name))
        return labels

    async def find_matching_arr_targets(
        self, media_server_id: int, media_type: str, media_id: int
    ) -> list[tuple[int, str]]:
        """在媒体服务器已绑定的 Arr 实例中查找匹配外部媒体 ID 的实例列表"""
        bound_arr_ids = await self._get_bound_arr_ids(media_server_id)
        if not bound_arr_ids:
            return []

        matched_ids: list[int] = []
        if media_type == "tvdb":
            for arr_id in bound_arr_ids:
                client = self.sonarr_clients.get(arr_id)
                if not client:
                    continue
                try:
                    series = await client.get_series_by_tvdb(media_id)
                    if series and series.id:
                        matched_ids.append(arr_id)
                except (HTTPError, ValueError) as e:
                    logger.warning("查询绑定 Sonarr 实例 {} 失败: {}", arr_id, e)
        elif media_type == "tmdb":
            for arr_id in bound_arr_ids:
                client = self.radarr_clients.get(arr_id)
                if not client:
                    continue
                try:
                    movie = await client.get_movie_by_tmdb(media_id)
                    if movie and movie.id:
                        matched_ids.append(arr_id)
                except (HTTPError, ValueError) as e:
                    logger.warning("查询绑定 Radarr 实例 {} 失败: {}", arr_id, e)

        return await self._format_arr_labels(matched_ids)

    async def handle_file_upload(
        self,
        user_id: int,
        file_path: str,
        file_name: str,
        media_server_id: int | None = None,
        target_arr_id: int | None = None,
    ) -> Result:
        """处理字幕文件上传（限定在所属媒体服务器已绑定的 Arr 实例中定位目标）"""
        if media_server_id is None:
            return Result(False, "未指定所属媒体服务器，无法定位已绑定的 Arr 实例。")

        # 1. 检查文件名格式（兼容 Telegram 重传时附加的 (1) 等副本后缀）
        parsed = self.parse_subtitle_zip_filename(file_name)
        if not parsed:
            return Result(
                False,
                "文件名格式错误。请使用 `tvdb-ID.zip` 或 `tmdb-ID.zip` 命名 (例如 `tvdb-842675.zip`)。",
            )

        media_type, media_id = parsed

        bound_arr_ids = await self._get_bound_arr_ids(media_server_id)
        if not bound_arr_ids:
            return Result(
                False, "该媒体服务器未绑定任何 Sonarr/Radarr 实例，无法上传字幕。"
            )

        logger.info(
            "处理字幕上传: 用户={}, 媒体服={}, 类型={}, ID={}, 指定Arr={}",
            user_id,
            media_server_id,
            media_type,
            media_id,
            target_arr_id,
        )

        try:
            # 2. 根据类型分发处理
            if media_type == "tvdb":
                return await self._handle_series(
                    user_id, media_id, file_path, bound_arr_ids, target_arr_id
                )
            elif media_type == "tmdb":
                return await self._handle_movie(
                    user_id, media_id, file_path, bound_arr_ids, target_arr_id
                )
        except (OSError, ValueError, TypeError) as e:
            logger.exception(f"处理字幕时发生系统错误: {e}")
            return Result(False, f"处理过程中发生系统错误: {str(e)}")

        return Result(False, "不支持的媒体类型")

    async def _handle_series(
        self,
        user_id: int,
        tvdb_id: int,
        zip_path: str,
        bound_arr_ids: list[int],
        target_arr_id: int | None = None,
    ) -> Result:
        """处理剧集字幕 (仅限已绑定的 Sonarr 实例)"""
        candidate_ids = [aid for aid in bound_arr_ids if aid in self.sonarr_clients]
        if not candidate_ids:
            return Result(
                False, "该媒体服务器未绑定可用的 Sonarr 实例，无法上传剧集字幕。"
            )

        if target_arr_id is not None:
            if target_arr_id not in candidate_ids:
                return Result(False, "指定的 Sonarr 实例未绑定到该媒体服务器或未连接。")
            candidate_ids = [target_arr_id]

        matches: list[tuple[int, SonarrClient, SeriesResource]] = []
        for arr_id in candidate_ids:
            client = self.sonarr_clients[arr_id]
            try:
                series = await client.get_series_by_tvdb(tvdb_id)
                if series and series.id:
                    matches.append((arr_id, client, series))
            except (HTTPError, ValueError) as e:
                logger.warning("查询 Sonarr 实例 {} 失败: {}", arr_id, e)
                continue

        if not matches:
            return Result(
                False,
                f"未在该媒体服务器已绑定的 Sonarr 实例中找到 TVDB ID 为 {tvdb_id} 的剧集。",
            )

        if len(matches) > 1:
            labels = await self._format_arr_labels([m[0] for m in matches])
            desc = "、".join(f"{name}(ID:{aid})" for aid, name in labels)
            return Result(
                False,
                f"多个已绑定的 Sonarr 实例均匹配 TVDB ID {tvdb_id}（{desc}），请先明确目标实例后再处理。",
            )

        _, target_client, series = matches[0]
        assert series.id is not None

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
            zip_path, series.title, lambda f: self._process_series_file(f, episode_map)
        )

    def _process_series_file(
        self, sub_file_path: Path, episode_map: dict[str, str]
    ) -> str | None:
        """剧集单文件处理逻辑：返回错误信息或 None(成功)"""
        sub_filename = sub_file_path.name

        # 匹配 SxxExx
        ep_match = re.search(r"[sS](\d+)[eE](\d+)", sub_filename)
        if not ep_match:
            return f"忽略 {sub_filename}：文件名未包含 SxxExx 格式"

        season_num = int(ep_match.group(1))
        episode_num = int(ep_match.group(2))
        key = f"S{season_num}E{episode_num}"

        if key not in episode_map:
            return f"已跳过 {sub_filename}：媒体库中未找到 {key} 对应的视频文件"

        media_path = Path(episode_map[key])
        media_dir = media_path.parent
        # 获取媒体文件的基础名称 (无后缀)，例如 "Show.S01E01"
        media_basename = media_path.stem

        # 截取 SxxExx 之后的部分，例如 ".zh.comment.ass" 或 " - Title.zh.ass"
        remainder = sub_filename[ep_match.end() :]
        suffix_match = re.search(r"(?:\.[^.]+)+$", remainder)
        if suffix_match:
            suffixes = suffix_match.group()
        else:
            # 兜底：如果没提取到后缀，直接取原文件后缀
            suffixes = "".join(sub_file_path.suffixes)

        if not self._is_allowed_extension(suffixes):
            return f"跳过 {sub_filename}: 不支持的字幕格式"

        new_sub_path = media_dir / f"{media_basename}{suffixes}"

        return self._safe_move(sub_file_path, new_sub_path)

    async def _handle_movie(
        self,
        user_id: int,
        tmdb_id: int,
        zip_path: str,
        bound_arr_ids: list[int],
        target_arr_id: int | None = None,
    ) -> Result:
        """处理电影字幕 (仅限已绑定的 Radarr 实例)"""
        candidate_ids = [aid for aid in bound_arr_ids if aid in self.radarr_clients]
        if not candidate_ids:
            return Result(
                False, "该媒体服务器未绑定可用的 Radarr 实例，无法上传电影字幕。"
            )

        if target_arr_id is not None:
            if target_arr_id not in candidate_ids:
                return Result(False, "指定的 Radarr 实例未绑定到该媒体服务器或未连接。")
            candidate_ids = [target_arr_id]

        matches: list[tuple[int, RadarrClient, MovieResource]] = []
        for arr_id in candidate_ids:
            client = self.radarr_clients[arr_id]
            try:
                movie = await client.get_movie_by_tmdb(tmdb_id)
                if movie and movie.id:
                    matches.append((arr_id, client, movie))
            except (HTTPError, ValueError) as e:
                logger.warning("查询 Radarr 实例 {} 失败: {}", arr_id, e)
                continue

        if not matches:
            return Result(
                False,
                f"未在该媒体服务器已绑定的 Radarr 实例中找到 TMDB ID 为 {tmdb_id} 的电影。",
            )

        if len(matches) > 1:
            labels = await self._format_arr_labels([m[0] for m in matches])
            desc = "、".join(f"{name}(ID:{aid})" for aid, name in labels)
            return Result(
                False,
                f"多个已绑定的 Radarr 实例均匹配 TMDB ID {tmdb_id}（{desc}），请先明确目标实例后再处理。",
            )

        _, _, movie = matches[0]

        if not movie.hasFile or not movie.movieFile or not movie.movieFile.path:
            return Result(False, "该电影在库中尚无视频文件，无法上传字幕。")

        movie_path = Path(movie.movieFile.path)
        media_dir = movie_path.parent
        media_basename = movie_path.stem

        # 2. 解压并处理
        return await self._extract_and_process(
            zip_path,
            movie.title,
            lambda f: self._process_movie_file(f, media_dir, media_basename),
        )

    def _process_movie_file(
        self, sub_file_path: Path, media_dir: Path, media_basename: str
    ) -> str | None:
        """电影单文件处理逻辑"""
        sub_filename = sub_file_path.name
        # 获取所有后缀 (如 .chi.srt)
        suffixes = "".join(sub_file_path.suffixes) or ".srt"

        if not self._is_allowed_extension(suffixes):
            return f"跳过 {sub_filename}: 不支持的字幕格式"

        new_sub_path = media_dir / f"{media_basename}{suffixes}"

        return self._safe_move(sub_file_path, new_sub_path)

    def _is_allowed_extension(self, suffix_str: str) -> bool:
        """检查后缀是否在白名单中 (检查最后一个点后的部分)"""
        if not suffix_str:
            return False
        ext = Path(suffix_str).suffix.lower()
        return ext in ALLOWED_EXTENSIONS

    def _safe_move(self, src: Path, dst: Path) -> str | None:
        """安全移动文件：检查存在性、防止覆盖、设置权限"""
        if dst.exists():
            return f"跳过: 目标位置已存在文件 {dst.name}"

        try:
            shutil.move(str(src), str(dst))
            dst.chmod(0o644)
            return None
        except OSError as e:
            return f"文件移动失败: {e}"

    async def _extract_and_process(
        self,
        zip_path: str,
        media_title: str | None,
        process_func: Callable[[Path], str | None],
    ) -> Result:
        """通用解压和遍历逻辑"""
        async with aiofiles.tempfile.TemporaryDirectory() as temp_dir:
            try:
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    total_size = 0
                    for zinfo in zip_ref.infolist():
                        if zinfo.filename.startswith("/") or ".." in zinfo.filename:
                            return Result(
                                False, f"发现不安全的文件路径: {zinfo.filename}"
                            )

                        if zinfo.file_size > MAX_SINGLE_FILE_SIZE:
                            return Result(
                                False,
                                f"文件 {zinfo.filename} 过大 (超过 {MAX_SINGLE_FILE_SIZE//1024//1024}MB)",
                            )

                        total_size += zinfo.file_size

                    if total_size > MAX_TOTAL_EXTRACT_SIZE:
                        return Result(
                            False,
                            f"压缩包解压后总大小过大 (超过 {MAX_TOTAL_EXTRACT_SIZE//1024//1024}MB)",
                        )

                    zip_ref.extractall(temp_dir)
            except zipfile.BadZipFile:
                return Result(False, "无效的 Zip 文件。")

            files_processed = 0
            errors = []

            for full_path in sorted(
                p for p in Path(temp_dir).rglob("*") if p.is_file()
            ):
                if "__MACOSX" in full_path.parts or full_path.name.startswith("."):
                    continue

                try:
                    error = process_func(full_path)
                    if error:
                        errors.append(error)
                    else:
                        files_processed += 1
                except (OSError, ValueError) as e:
                    errors.append(f"处理文件 {full_path.name} 时出错: {str(e)}")

            if files_processed == 0:
                if not errors:
                    return Result(False, "压缩包内未找到有效文件。")
                reasons = "\n".join(f"• {err}" for err in errors)
                return Result(
                    False,
                    f"❌ **字幕写入失败**（写入 0 个，失败/跳过 {len(errors)} 个）\n"
                    f"🎬 媒体: {media_title}\n\n"
                    f"**原因明细**:\n{reasons}",
                )

            if errors:
                reasons = "\n".join(f"• {err}" for err in errors[:5])
                if len(errors) > 5:
                    reasons += f"\n• ...等共 {len(errors)} 个失败/跳过原因"
                msg = (
                    f"⚠️ **字幕部分成功**\n"
                    f"🎬 媒体: {media_title}\n"
                    f"📥 成功写入: {files_processed} 个 | ❌ 失败/跳过: {len(errors)} 个\n\n"
                    f"**主要原因**:\n{reasons}"
                )
                return Result(True, msg)

            msg = (
                f"✅ **字幕处理成功**\n"
                f"🎬 媒体: {media_title}\n"
                f"📥 成功写入: {files_processed} 个文件"
            )
            return Result(True, msg)
