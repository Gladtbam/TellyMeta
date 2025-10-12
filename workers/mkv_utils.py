import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiofiles.os as aio_os
from loguru import logger


async def get_mkv_info(episode_path: Path) -> None | dict[str, list[dict[str, Any]]]:
    """获取 MKV 文件的轨道和附件信息，确保文件存在且大小稳定后再进行处理"""
    start_time = time.time()
    while not await aio_os.path.exists(episode_path):
        if time.time() - start_time > 600:  # 超过10分钟仍未找到文件，退出循环
            logger.error("超时：文件 {} 从未出现。", episode_path)
            return None
        logger.info("正在等待 {} 存在...", episode_path)
        await asyncio.sleep(5)

    last_size = -1
    stable_checks = 0
    stable_checks_required = 3  # 连续3次检查文件大小不变才认为文件稳定
    while stable_checks < stable_checks_required:
        if time.time() - start_time > 600:  # 超过10分钟仍未找到文件，退出循环
            logger.error("超时：文件 {} 从未稳定。", episode_path)
            return None

        try:
            current_size = await aio_os.path.getsize(episode_path)
        except FileNotFoundError:
            last_size = -1
            stable_checks = 0
            await asyncio.sleep(2)
            continue

        if current_size == last_size and current_size > 0:
            stable_checks += 1
        else:
            stable_checks = 0

        last_size = current_size
        await asyncio.sleep(2)  # 等待2秒后再次检查文件大小

    process = await asyncio.create_subprocess_exec(
        'mkvmerge', '-J', str(episode_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error("在 {} 上运行 mkvmerge -J 时出错：{}", episode_path, stderr.decode().strip())
        return None

    try:
        loop = asyncio.get_running_loop()
        mkvinfo_json = await loop.run_in_executor(None, json.loads, stdout.decode())
        # mkvinfo_json = json.loads(stdout.decode())
    except json.JSONDecodeError as e:
        logger.error("无法解码 mkvmerge JSON 输出：{}", e)
        return None

    tracks = [
        {
            "id": track.get("id"),
            "type": track.get("type"),
            "language": track.get("properties", {}).get("language"),
            "language_ietf": track.get("properties", {}).get("language_ietf"),
            "name": track.get("properties", {}).get("track_name"),
        } for track in mkvinfo_json.get("tracks", [])
    ]
    attachments = [
        {
            "id": attachment.get("id"),
            "name": attachment.get("file_name"),
            "type": attachment.get("content_type"),
        } for attachment in mkvinfo_json.get("attachments", [])
    ]
    return {"tracks": tracks, "attachments": attachments}

async def mkv_merge(episode_path: Path) -> None:
    """合并 MKV 文件，保留中文字幕轨道、非字幕轨道和附件，删除其它语言的字幕轨道
    如果没有中文字幕轨道，则删除所有字幕轨道和附件
    Args:
        episode_path (Path): MKV 文件路径"""
    mkv_info = await get_mkv_info(episode_path)
    if mkv_info is None:
        logger.error("无法获取 {} 的 MKV 信息", episode_path)
        return

    output_path = episode_path.with_suffix('.merged.mkv')
    # 处理轨道
    subtitle_tracks = [track for track in mkv_info["tracks"] if track.get("type") == "subtitles"]

    if not subtitle_tracks or all(track.get("language") in ["chi", "zh"] or track.get("language_ietf") in ["zh-Hans", "zh-Hant"] for track in subtitle_tracks):
        logger.info("{} 中未找到字幕或只有中文字幕，正在跳过合并", episode_path)
        return

    cmd = ['mkvmerge', '-o', str(output_path)]

    chinese_subtitle_tracks = [track for track in subtitle_tracks if track.get("language") in ["chi", "zh"] or track.get("language_ietf") in ["zh-Hans", "zh-Hant"]]

    if not chinese_subtitle_tracks:
    # 没有中文字幕轨道，仅保留非字幕轨道，并删除所有附件
        cmd.extend(["--no-subtitles", "--no-attachments", str(episode_path)])
    else:
        # 有中文字幕轨道和其它字幕轨道，保留中文字幕轨道、非字幕轨道和附件
        track_ids = ",".join(str(track["id"]) for track in chinese_subtitle_tracks)
        cmd.extend(["--subtitle-tracks", track_ids, str(episode_path)])

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error("在 {} 上运行 mkvmerge 时出错：{}", episode_path, stderr.decode().strip())
        return

    try:
        # output_path.rename(episode_path)  # 重命名输出文件为原始文件名
        await aio_os.rename(output_path, episode_path)
        logger.info("已成功将 {} 合并到 {}", episode_path, output_path)
    except Exception as e:
        logger.error("将 {} 重命名为 {} 时出错：{}", output_path, episode_path, e)
