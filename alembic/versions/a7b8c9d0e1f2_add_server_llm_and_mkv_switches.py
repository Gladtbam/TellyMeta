"""为 server_instances 表新增服务器级 MKV 清理与 LLM 翻译子开关列

Revision ID: a7b8c9d0e1f2
Revises: f2a3b4c5d6e7
Create Date: 2026-11-24 19:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# 修订标识，供 Alembic 链式迁移使用
revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """在 server_instances 表新增服务器级 MKV 清理与 LLM 翻译子开关列（默认开启以兼容存量配置）"""
    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.add_column(
            sa.Column(
                "enable_mkv_process",
                sa.Boolean(),
                server_default=sa.text("true"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "enable_llm_translate",
                sa.Boolean(),
                server_default=sa.text("true"),
                nullable=False,
            )
        )


def downgrade() -> None:
    """移除 server_instances 表中的服务器级 MKV 清理与 LLM 翻译子开关列"""
    with op.batch_alter_table("server_instances") as batch_op:
        batch_op.drop_column("enable_llm_translate")
        batch_op.drop_column("enable_mkv_process")
