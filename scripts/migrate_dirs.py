"""将旧版项目目录 (./data, ./logs) 迁移至系统标准数据/日志目录的辅助脚本。

用法示例:
    # 预演将要迁移的文件（不实际写入）
    uv run python -m scripts.migrate_dirs --dry-run

    # 复制旧目录文件到新标准目录（保留旧文件，默认跳过目标已存在文件）
    uv run python -m scripts.migrate_dirs

    # 移动旧目录文件到新标准目录，并在存在同名文件时覆盖
    uv run python -m scripts.migrate_dirs --move --overwrite
"""

import argparse
import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from core.config import get_data_dir, get_logs_dir

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class DirMigrationStats:
    """单组目录迁移统计结果"""

    copied_or_moved: int = 0
    skipped_existing: int = 0
    overwritten: int = 0


def migrate_directory(
    src_dir: Path,
    dst_dir: Path,
    *,
    move: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
) -> DirMigrationStats:
    """将 src_dir 下的全部文件迁移到 dst_dir，保留相对目录结构"""
    stats = DirMigrationStats()
    if not src_dir.exists():
        return stats

    if src_dir.resolve() == dst_dir.resolve():
        raise ValueError(f"源目录与目标目录相同: {src_dir}")

    if not dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)

    for src_file in sorted(p for p in src_dir.rglob("*") if p.is_file()):
        rel_path = src_file.relative_to(src_dir)
        dst_file = dst_dir / rel_path
        existed = dst_file.exists()

        if existed and not overwrite:
            stats.skipped_existing += 1
            print(f"  [跳过] {rel_path}（目标已存在）")
            continue

        action = "移动" if move else "复制"
        suffix = "（覆盖）" if existed else ""
        print(f"  [{action}{suffix}] {src_file} -> {dst_file}")

        if not dry_run:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            if move:
                if existed:
                    dst_file.unlink()
                shutil.move(str(src_file), str(dst_file))
            else:
                shutil.copy2(src_file, dst_file)

        if existed:
            stats.overwritten += 1
        else:
            stats.copied_or_moved += 1

    # 移动模式下，若旧目录已完全清空则顺带清理空目录
    if move and not dry_run and src_dir.exists():
        for sub_dir in sorted(
            (p for p in src_dir.rglob("*") if p.is_dir()), reverse=True
        ):
            with contextlib.suppress(OSError):
                sub_dir.rmdir()
        with contextlib.suppress(OSError):
            src_dir.rmdir()

    return stats


def main() -> None:
    """命令行入口"""
    parser = argparse.ArgumentParser(
        description="TellyMeta 旧版 ./data 与 ./logs 目录迁移工具"
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="移动文件而不是复制（默认保留旧目录文件作为备份）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="目标目录存在同名文件时覆盖（默认跳过）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预演输出将要迁移的文件，不执行实际写入",
    )
    parser.add_argument(
        "--data-only",
        action="store_true",
        help="仅迁移 data 目录",
    )
    parser.add_argument(
        "--logs-only",
        action="store_true",
        help="仅迁移 logs 目录",
    )
    args = parser.parse_args()

    if args.data_only and args.logs_only:
        parser.error("--data-only 与 --logs-only 不能同时使用")

    tasks: list[tuple[str, Path, Path]] = []
    if not args.logs_only:
        tasks.append(("data", ROOT_DIR / "data", get_data_dir()))
    if not args.data_only:
        tasks.append(("logs", ROOT_DIR / "logs", get_logs_dir()))

    mode_text = "预演" if args.dry_run else ("移动" if args.move else "复制")
    print(f"=== 目录迁移开始（模式: {mode_text}）===")

    for label, src_dir, dst_dir in tasks:
        print(f"\n[{label}] {src_dir} -> {dst_dir}")
        if not src_dir.exists():
            print("  源目录不存在，已跳过。")
            continue
        stats = migrate_directory(
            src_dir,
            dst_dir,
            move=args.move,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        print(
            f"  小计: 新增={stats.copied_or_moved}, 覆盖={stats.overwritten}, 跳过={stats.skipped_existing}"
        )


if __name__ == "__main__":
    main()
