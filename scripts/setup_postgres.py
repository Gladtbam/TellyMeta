"""PostgreSQL 数据库交互式配置与建库向导。

用法:
    uv run python -m scripts.setup_postgres
"""

import asyncio
import os
import re
import secrets
import shutil
import subprocess
from getpass import getpass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

ROOT_DIR = Path(__file__).resolve().parent.parent


def build_postgresql_url(
    host: str, port: int, user: str, password: str, database: str
) -> str:
    """根据分项参数构建经过 URL 编码的 PostgreSQL 异步连接字符串"""
    return URL.create(
        drivername="postgresql+asyncpg",
        username=user,
        password=password or None,
        host=host,
        port=port,
        database=database,
    ).render_as_string(hide_password=False)


def upsert_env_database_url(database_url: str, env_path: Path | None = None) -> Path:
    """将 database_url 写入或更新到 .env 文件中，保留其余配置项"""
    target_path = env_path or (ROOT_DIR / ".env")
    if target_path.exists():
        content = target_path.read_text(encoding="utf-8")
    else:
        example_path = target_path.with_name(".env.example")
        content = (
            example_path.read_text(encoding="utf-8") if example_path.exists() else ""
        )

    new_line = f"database_url = '{database_url}'"
    pattern = re.compile(r"^\s*database_url\s*=.*$", re.MULTILINE)
    if pattern.search(content):
        updated = pattern.sub(new_line, content, count=1)
    else:
        sep = "" if (not content or content.endswith("\n")) else "\n"
        updated = f"{content}{sep}{new_line}\n"

    target_path.write_text(updated, encoding="utf-8")
    return target_path


def _quote_pg_identifier(identifier: str) -> str:
    """安全转义 PostgreSQL 标识符（表名/库名/用户名）"""
    if not identifier or "\x00" in identifier:
        raise ValueError("无效的 PostgreSQL 标识符")
    return '"' + identifier.replace('"', '""') + '"'


def _quote_pg_literal(value: str) -> str:
    """安全转义 PostgreSQL 字符串字面量"""
    if "\x00" in value:
        raise ValueError("密码不能包含空字符")
    return "'" + value.replace("'", "''") + "'"


async def _test_postgres_connection(database_url: str) -> tuple[bool, str]:
    """尝试连接 PostgreSQL 并执行 SELECT 1，返回 (是否成功, 提示信息)"""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, "连接测试成功！"
    except ModuleNotFoundError as e:
        return False, f"缺少 PostgreSQL 驱动 ({e})，请先执行: uv sync --extra postgres"
    except Exception as e:
        return False, f"连接失败: {e}"
    finally:
        await engine.dispose()


async def create_postgresql_user_and_database(
    host: str,
    port: int,
    admin_user: str,
    admin_password: str,
    target_user: str,
    target_password: str,
    target_database: str,
) -> tuple[bool, str]:
    """连接到 postgres 维护库，按需创建目标用户与目标数据库"""
    admin_url = build_postgresql_url(host, port, admin_user, admin_password, "postgres")
    engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )
    try:
        async with engine.connect() as conn:
            user_exists = await conn.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :u"),
                {"u": target_user},
            )
            user_ident = _quote_pg_identifier(target_user)
            if not user_exists:
                if target_password:
                    pwd_lit = _quote_pg_literal(target_password)
                    await conn.execute(
                        text(f"CREATE USER {user_ident} WITH PASSWORD {pwd_lit}")
                    )
                else:
                    await conn.execute(text(f"CREATE USER {user_ident}"))

            db_exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :db"),
                {"db": target_database},
            )
            if not db_exists:
                db_ident = _quote_pg_identifier(target_database)
                await conn.execute(
                    text(f"CREATE DATABASE {db_ident} OWNER {user_ident}")
                )
                return (
                    True,
                    f"数据库 '{target_database}'（所有者 '{target_user}'）创建成功！",
                )
            return True, f"数据库 '{target_database}' 已存在。"
    except ModuleNotFoundError as e:
        return False, f"缺少 PostgreSQL 驱动 ({e})，请先执行: uv sync --extra postgres"
    except Exception as e:
        return False, f"创建用户或数据库失败: {e}"
    finally:
        await engine.dispose()


def _try_local_postgres_peer_create(
    host: str,
    port: int,
    target_user: str,
    target_password: str,
    target_database: str,
) -> tuple[bool, str]:
    """Linux/macOS 本机环境尝试通过免密 postgres 系统账户 (psql peer 认证) 建库"""
    if os.name == "nt" or host not in ("127.0.0.1", "localhost", "::1"):
        return False, "非本机 POSIX 环境，跳过免密本地建库"
    if not shutil.which("psql"):
        return False, "未找到 psql 命令，跳过免密本地建库"

    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if shutil.which("sudo"):
        prefix = ["sudo", "-n", "-u", "postgres"]
    elif is_root and shutil.which("runuser"):
        prefix = ["runuser", "-u", "postgres", "--"]
    else:
        return False, "当前环境不支持免密切换至 postgres 系统用户"

    def _run_psql(sql: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *prefix,
                "psql",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-p",
                str(port),
                "-tAc",
                sql,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    user_lit = _quote_pg_literal(target_user)
    user_ident = _quote_pg_identifier(target_user)
    db_lit = _quote_pg_literal(target_database)
    db_ident = _quote_pg_identifier(target_database)

    check_user = _run_psql(f"SELECT 1 FROM pg_roles WHERE rolname = {user_lit}")
    if check_user.returncode != 0:
        return (
            False,
            (check_user.stderr or check_user.stdout or "psql 执行失败").strip(),
        )

    if check_user.stdout.strip() != "1":
        if target_password:
            pwd_lit = _quote_pg_literal(target_password)
            res = _run_psql(f"CREATE USER {user_ident} WITH PASSWORD {pwd_lit}")
        else:
            res = _run_psql(f"CREATE USER {user_ident}")
        if res.returncode != 0:
            return False, (res.stderr or res.stdout or "创建用户失败").strip()

    check_db = _run_psql(f"SELECT 1 FROM pg_database WHERE datname = {db_lit}")
    if check_db.returncode != 0:
        return False, (check_db.stderr or check_db.stdout or "查询数据库失败").strip()

    if check_db.stdout.strip() != "1":
        res = _run_psql(f"CREATE DATABASE {db_ident} OWNER {user_ident}")
        if res.returncode != 0:
            return False, (res.stderr or res.stdout or "创建数据库失败").strip()
        return (
            True,
            f"已通过本机 postgres 账户创建数据库 '{target_database}'（所有者 '{target_user}'）！",
        )

    return True, f"数据库 '{target_database}' 已存在。"


def configure_postgresql_database(env_path: Path | None = None) -> str:
    """交互式辅助配置 PostgreSQL：先询问是否建库，再收集连接信息并写入 .env"""
    print("=== TellyMeta PostgreSQL 数据库配置向导 ===")
    should_create = input(
        "是否需要创建 PostgreSQL 数据库？[y/N]: "
    ).strip().lower() in (
        "y",
        "yes",
    )

    host = input("数据库地址 [127.0.0.1]: ").strip() or "127.0.0.1"

    while True:
        port_raw = input("端口 [5432]: ").strip() or "5432"
        if port_raw.isdigit() and 1 <= int(port_raw) <= 65535:
            port = int(port_raw)
            break
        print("端口无效，请输入 1-65535 之间的数字。")

    user = input("用户名 [tellymeta]: ").strip() or "tellymeta"
    generated_password = False
    if should_create:
        password = getpass("密码（留空将自动生成随机密码）: ")
        if not password:
            password = secrets.token_urlsafe(18)
            generated_password = True
            print(f"已生成随机密码: {password}")
    else:
        password = getpass("密码（留空表示无密码）: ")

    database = input("数据库名 [tellymeta]: ").strip() or "tellymeta"

    if should_create:
        created, create_msg = _try_local_postgres_peer_create(
            host=host,
            port=port,
            target_user=user,
            target_password=password,
            target_database=database,
        )
        if not created:
            print(f"[INFO] {create_msg}，请提供有建库权限的 PostgreSQL 管理员账号：")
            admin_user = input("管理员用户名 [postgres]: ").strip() or "postgres"
            admin_password = getpass("管理员密码（留空表示无密码）: ")
            created, create_msg = asyncio.run(
                create_postgresql_user_and_database(
                    host=host,
                    port=port,
                    admin_user=admin_user,
                    admin_password=admin_password,
                    target_user=user,
                    target_password=password,
                    target_database=database,
                )
            )
        print(f"[{'OK' if created else 'WARN'}] {create_msg}")

    db_url = build_postgresql_url(host, port, user, password, database)
    ok, msg = asyncio.run(_test_postgres_connection(db_url))
    print(f"[{'OK' if ok else 'WARN'}] {msg}")

    if not ok:
        confirm = input("连接测试未通过，是否仍写入 .env？[y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("已取消写入 .env。")
            return db_url

    written_path = upsert_env_database_url(db_url, env_path=env_path)
    print(f"已更新配置文件: {written_path}")
    if generated_password:
        print("请妥善保存上述随机密码（已编码写入 .env 的 database_url 中）。")
    print(
        "提示：请确保已通过 `uv sync --extra postgres` 安装依赖，启动主程序时会自动执行建表迁移。"
    )
    return db_url


if __name__ == "__main__":
    configure_postgresql_database()
