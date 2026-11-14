"""SQLite 与 PostgreSQL 双向数据迁移脚本。

用法示例:
    # 将默认 SQLite 迁移到 .env 中配置的 PostgreSQL
    uv run python -m scripts.migrate_db

    # 显式指定源库与目标库（支持 SQLite 文件路径或完整 URL）
    uv run python -m scripts.migrate_db \\
        --source /var/lib/tellymeta/tellymeta.db \\
        --target postgresql+asyncpg://user:pass@127.0.0.1:5432/tellymeta

    # 清空目标库已有数据后重新迁移
    uv run python -m scripts.migrate_db --truncate
"""

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import BigInteger, Integer, literal_column, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from core.database import (
    DATA_DIR,
    DATABASE_URL,
    is_sqlite_url,
    normalize_async_database_url,
)
from models.orm import Base

ROOT_DIR = Path(__file__).resolve().parent.parent


def resolve_database_url(raw: str | None, default_url: str) -> str:
    """将命令行传入的文件路径或数据库 URL 解析为规范化的异步连接字符串"""
    value = (raw or "").strip()
    if not value:
        return default_url
    if "://" not in value:
        sqlite_path = Path(value).expanduser().resolve()
        return f"sqlite+aiosqlite:///{sqlite_path}"
    return normalize_async_database_url(value)


def mask_database_url(url: str) -> str:
    """隐藏数据库 URL 中的密码，避免日志或终端输出泄露敏感凭据"""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except ArgumentError, ValueError:
        return url


def run_target_alembic_migrations(target_url: str) -> None:
    """在目标数据库上执行 Alembic 迁移至最新版本"""
    alembic_cfg = Config(str(ROOT_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(ROOT_DIR / "alembic"))
    alembic_cfg.attributes["sqlalchemy.url"] = target_url
    command.upgrade(alembic_cfg, "head")


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """统一处理跨方言行数据（如将带时区的 datetime 转为 naive datetime）"""
    normalized: dict[str, Any] = {}
    for key, val in row.items():
        if isinstance(val, datetime) and val.tzinfo is not None:
            normalized[key] = val.replace(tzinfo=None)
        else:
            normalized[key] = val
    return normalized


async def _reset_postgres_sequences(target_conn: Any) -> None:
    """重置 PostgreSQL 自增主键序列，防止迁移后新插入记录发生主键冲突"""
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if (
                col.primary_key
                and isinstance(col.type, (Integer, BigInteger))
                and col.autoincrement is not False
            ):
                seq_name = await target_conn.scalar(
                    text("SELECT pg_get_serial_sequence(:table_name, :col_name)"),
                    {"table_name": table.name, "col_name": col.name},
                )
                if not seq_name:
                    continue
                quoted_table = f'"{table.name.replace(chr(34), chr(34) * 2)}"'
                quoted_col = f'"{col.name.replace(chr(34), chr(34) * 2)}"'
                await target_conn.execute(
                    text(
                        f"SELECT setval(:seq_name, COALESCE((SELECT MAX({quoted_col}) FROM {quoted_table}), 1), "
                        f"(SELECT MAX({quoted_col}) IS NOT NULL FROM {quoted_table}))"
                    ),
                    {"seq_name": seq_name},
                )


async def migrate_database(
    source_url: str,
    target_url: str,
    *,
    truncate: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """将源库全部 ORM 表数据按外键拓扑顺序迁移至目标库，返回各表迁移行数"""
    if source_url == target_url:
        raise ValueError("源数据库与目标数据库不能相同")

    source_engine = create_async_engine(source_url, pool_pre_ping=True)
    target_engine = create_async_engine(target_url, pool_pre_ping=True)
    tables = list(Base.metadata.sorted_tables)
    counts: dict[str, int] = {}

    try:
        async with source_engine.connect() as src_conn:
            for table in tables:
                row_count = await src_conn.scalar(
                    select(literal_column("COUNT(*)")).select_from(table)
                )
                counts[table.name] = int(row_count or 0)

            if dry_run:
                return counts

            async with target_engine.begin() as dst_conn:
                # 先检查目标库是否已有数据
                non_empty_tables: list[str] = []
                for table in tables:
                    dst_count = await dst_conn.scalar(
                        select(literal_column("COUNT(*)")).select_from(table)
                    )
                    if int(dst_count or 0) > 0:
                        non_empty_tables.append(f"{table.name}({dst_count})")

                if non_empty_tables and not truncate:
                    raise RuntimeError(
                        "目标数据库以下表已存在数据: "
                        + ", ".join(non_empty_tables)
                        + "。如需覆盖请添加 --truncate 参数。"
                    )

                if truncate:
                    for table in reversed(tables):
                        await dst_conn.execute(table.delete())

                for table in tables:
                    if counts[table.name] == 0:
                        continue
                    result = await src_conn.execute(select(table))
                    rows = [_normalize_row(dict(r)) for r in result.mappings().all()]

                    # 识别自引用可空外键（如 server_instances.qb_server_id），先置空插入再回填，避免同表行序导致的外键冲突
                    self_fk_cols = [
                        fk.parent.name
                        for fk in table.foreign_keys
                        if fk.column.table.name == table.name and fk.parent.nullable
                    ]
                    pk_cols = [col.name for col in table.primary_key.columns]
                    deferred_updates: list[dict[str, Any]] = []
                    if self_fk_cols and pk_cols:
                        insert_rows: list[dict[str, Any]] = []
                        for row in rows:
                            if any(row.get(col) is not None for col in self_fk_cols):
                                deferred_updates.append(
                                    {
                                        **{pk: row[pk] for pk in pk_cols},
                                        **{col: row[col] for col in self_fk_cols},
                                    }
                                )
                                row_copy = dict(row)
                                for col in self_fk_cols:
                                    row_copy[col] = None
                                insert_rows.append(row_copy)
                            else:
                                insert_rows.append(row)
                    else:
                        insert_rows = rows

                    # asyncpg 单条语句最多支持 32767 个绑定参数，按列数动态计算安全批大小
                    col_count = max(1, len(table.columns))
                    batch_size = max(1, min(500, 30000 // col_count))
                    for idx in range(0, len(insert_rows), batch_size):
                        await dst_conn.execute(
                            table.insert(), insert_rows[idx : idx + batch_size]
                        )

                    for upd in deferred_updates:
                        stmt = table.update()
                        for pk in pk_cols:
                            stmt = stmt.where(table.c[pk] == upd[pk])
                        stmt = stmt.values(**{col: upd[col] for col in self_fk_cols})
                        await dst_conn.execute(stmt)

                if not is_sqlite_url(target_url):
                    await _reset_postgres_sequences(dst_conn)

        return counts
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


def main() -> None:
    """命令行入口"""
    default_sqlite_url = f"sqlite+aiosqlite:///{DATA_DIR / 'tellymeta.db'}"
    parser = argparse.ArgumentParser(
        description="TellyMeta SQLite 与 PostgreSQL 双向数据迁移工具"
    )
    parser.add_argument(
        "--source",
        default=None,
        help="源数据库 URL 或 SQLite 文件路径（默认使用系统标准数据目录下的 tellymeta.db）",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="目标数据库 URL 或 SQLite 文件路径（默认读取 .env 中的 database_url）",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="迁移前清空目标库现有数据",
    )
    parser.add_argument(
        "--skip-alembic",
        action="store_true",
        help="跳过在目标库上执行 alembic upgrade head",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计源库各表记录数，不执行实际写入",
    )
    args = parser.parse_args()

    source_url = resolve_database_url(args.source, default_sqlite_url)
    target_url = resolve_database_url(args.target, DATABASE_URL)

    print(f"源数据库: {mask_database_url(source_url)}")
    print(f"目标数据库: {mask_database_url(target_url)}")

    if not args.dry_run and not args.skip_alembic:
        print("正在目标数据库执行 Alembic 表结构迁移...")
        run_target_alembic_migrations(target_url)

    counts = asyncio.run(
        migrate_database(
            source_url,
            target_url,
            truncate=args.truncate,
            dry_run=args.dry_run,
        )
    )

    mode_label = "预演统计" if args.dry_run else "迁移完成"
    print(f"=== {mode_label} ===")
    for table_name, count in counts.items():
        print(f"  {table_name}: {count} 条")


if __name__ == "__main__":
    main()
