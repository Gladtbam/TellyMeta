import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import aiofiles.os as aio_os
from loguru import logger


async def _probe_mkv_json(
    file_path: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """调用 mkvmerge -J 探测 MKV 元数据，返回 (JSON 数据, 错误信息)"""
    process = await asyncio.create_subprocess_exec(
        "mkvmerge",
        "-J",
        str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        err = (
            stderr.decode(errors="replace").strip()
            or stdout.decode(errors="replace").strip()
        )
        return None, f"mkvmerge -J 执行失败 (code={process.returncode}): {err}"

    try:
        loop = asyncio.get_running_loop()
        mkvinfo_json = await loop.run_in_executor(None, json.loads, stdout.decode())
        return mkvinfo_json, None
    except json.JSONDecodeError as e:
        return None, f"无法解析 mkvmerge JSON 输出: {e}"


async def get_mkv_info(
    episode_path: Path,
    *,
    stability_wait: float = 2.0,
    timeout: float = 600.0,  # noqa: ASYNC109 - 显式保留 timeout 参数以兼容调用方
) -> dict[str, list[dict[str, Any]]] | None:
    """获取 MKV 文件的轨道和附件信息，确保文件存在且大小稳定后再进行处理"""
    start_time = time.time()
    while not await aio_os.path.exists(episode_path):
        if time.time() - start_time > timeout:
            logger.error("超时：文件 {} 从未出现。", episode_path)
            return None
        logger.info("正在等待 {} 存在...", episode_path)
        await asyncio.sleep(min(5.0, max(0.05, stability_wait)))

    last_size = -1
    stable_checks = 0
    stable_checks_required = 3  # 连续3次检查文件大小不变才认为文件稳定
    while stable_checks < stable_checks_required:
        if time.time() - start_time > timeout:
            logger.error("超时：文件 {} 从未稳定。", episode_path)
            return None

        try:
            current_size = await aio_os.path.getsize(episode_path)
        except FileNotFoundError:
            last_size = -1
            stable_checks = 0
            await asyncio.sleep(stability_wait)
            continue

        if current_size == last_size and current_size > 0:
            stable_checks += 1
        else:
            stable_checks = 0

        last_size = current_size
        await asyncio.sleep(stability_wait)

    mkvinfo_json, err = await _probe_mkv_json(episode_path)
    if mkvinfo_json is None:
        logger.error("在 {} 上运行 mkvmerge -J 时出错：{}", episode_path, err)
        return None

    tracks = [
        {
            "id": track.get("id"),
            "type": track.get("type"),
            "language": track.get("properties", {}).get("language"),
            "language_ietf": track.get("properties", {}).get("language_ietf"),
            "name": track.get("properties", {}).get("track_name"),
        }
        for track in mkvinfo_json.get("tracks", [])
    ]
    attachments = [
        {
            "id": attachment.get("id"),
            "name": attachment.get("file_name"),
            "type": attachment.get("content_type"),
        }
        for attachment in mkvinfo_json.get("attachments", [])
    ]
    return {"tracks": tracks, "attachments": attachments}


async def _verify_merged_output(
    output_path: Path, expected_non_sub_count: int
) -> tuple[bool, str | None]:
    """验证独立生成的输出 MKV 文件是否完整可用"""
    if not await aio_os.path.exists(output_path):
        return False, f"输出文件不存在: {output_path}"

    try:
        out_size = await aio_os.path.getsize(output_path)
    except OSError as e:
        return False, f"无法读取输出文件大小: {e}"

    if out_size <= 0:
        return False, "输出文件大小为 0"

    out_json, err = await _probe_mkv_json(output_path)
    if out_json is None:
        return False, f"输出文件格式校验失败: {err}"

    out_tracks = out_json.get("tracks", [])
    if not out_tracks:
        return False, "输出文件未包含任何有效轨道"

    out_non_sub_count = sum(
        1 for track in out_tracks if track.get("type") != "subtitles"
    )
    if out_non_sub_count < expected_non_sub_count:
        return (
            False,
            f"输出文件非字幕轨道数量异常（期望至少 {expected_non_sub_count}，实际 {out_non_sub_count}）",
        )

    return True, None


def _cleanup_temp_file(temp_path: Path) -> None:
    """安全清理临时输出文件，避免残留污染媒体目录"""
    try:
        if temp_path.exists():
            temp_path.unlink()
    except OSError as e:
        logger.warning("清理 MKV 临时文件失败 {}: {}", temp_path, e)


async def mkv_merge(
    episode_path: Path,
    *,
    stability_wait: float = 2.0,
    timeout: float = 600.0,  # noqa: ASYNC109 - 显式保留 timeout 参数以兼容调用方
) -> tuple[bool, str | None]:
    """安全清理 MKV 冗余字幕：先校验路径与目录权限，再生成并验证独立输出文件，最后原子替换原文件

    Args:
        episode_path (Path): MKV 文件本地路径

    Returns:
        tuple[bool, str | None]: (是否成功或无需处理, 失败时的可诊断错误信息)
    """
    if not await aio_os.path.exists(episode_path):
        err = f"目标文件不存在（请检查路径映射）: {episode_path}"
        logger.error(err)
        return False, err

    # 权限预检：目录写/执行权限或文件读写权限不足时，绝不尝试破坏性处理或替换
    parent_dir = episode_path.parent
    if not os.access(parent_dir, os.W_OK | os.X_OK) or not os.access(
        episode_path, os.R_OK | os.W_OK
    ):
        err = f"目录或文件权限不足，已拒绝处理并保持原文件完整: {episode_path}"
        logger.error(err)
        return False, err

    mkv_info = await get_mkv_info(
        episode_path, stability_wait=stability_wait, timeout=timeout
    )
    if mkv_info is None:
        err = f"无法获取 MKV 轨道信息（文件未就绪或损坏），已保持原文件完整: {episode_path}"
        logger.error(err)
        return False, err

    subtitle_tracks = [
        track for track in mkv_info["tracks"] if track.get("type") == "subtitles"
    ]
    if not subtitle_tracks or all(
        track.get("language") in ["chi", "zh"]
        or track.get("language_ietf") in ["zh-Hans", "zh-Hant"]
        for track in subtitle_tracks
    ):
        logger.info("{} 中未找到字幕或只有中文字幕，正在跳过合并", episode_path)
        return True, None

    # 在同目录生成独立临时输出文件，确保校验通过后可同一文件系统原子替换
    output_path = episode_path.with_name(
        f".{episode_path.stem}.merging.{os.getpid()}.{int(time.time())}.mkv"
    )
    cmd = ["mkvmerge", "-o", str(output_path)]

    chinese_subtitle_tracks = [
        track
        for track in subtitle_tracks
        if track.get("language") in ["chi", "zh"]
        or track.get("language_ietf") in ["zh-Hans", "zh-Hant"]
    ]

    if not chinese_subtitle_tracks:
        # 没有中文字幕轨道，仅保留非字幕轨道，并删除所有附件
        cmd.extend(["--no-subtitles", "--no-attachments", str(episode_path)])
    else:
        # 有中文字幕轨道和其它字幕轨道，保留中文字幕轨道、非字幕轨道和附件
        track_ids = ",".join(str(track["id"]) for track in chinese_subtitle_tracks)
        cmd.extend(["--subtitle-tracks", track_ids, str(episode_path)])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except OSError as e:
        _cleanup_temp_file(output_path)
        err = f"启动 mkvmerge 失败（原文件保持完整）: {e}"
        logger.error("{}: {}", episode_path, err)
        return False, err

    if process.returncode != 0:
        _cleanup_temp_file(output_path)
        detail = (
            stderr.decode(errors="replace").strip()
            or stdout.decode(errors="replace").strip()
        )
        err = f"mkvmerge 生成输出文件失败 (code={process.returncode})，原文件保持完整: {detail}"
        logger.error("{}: {}", episode_path, err)
        return False, err

    expected_non_sub_count = sum(
        1 for track in mkv_info["tracks"] if track.get("type") != "subtitles"
    )
    verified, verify_err = await _verify_merged_output(
        output_path, expected_non_sub_count
    )
    if not verified:
        _cleanup_temp_file(output_path)
        err = f"独立输出文件校验失败，原文件保持完整: {verify_err}"
        logger.error("{}: {}", episode_path, err)
        return False, err

    try:
        await aio_os.replace(output_path, episode_path)
        logger.info("已成功生成、验证并安全替换 MKV 文件: {}", episode_path)
        return True, None
    except PermissionError as e:
        _cleanup_temp_file(output_path)
        err = f"权限不足，无法安全替换原文件（原文件保持完整）: {e}"
        logger.error("{}: {}", episode_path, err)
        return False, err
    except OSError as e:
        _cleanup_temp_file(output_path)
        err = f"安全替换原文件失败（原文件保持完整）: {e}"
        logger.error("{}: {}", episode_path, err)
        return False, err
