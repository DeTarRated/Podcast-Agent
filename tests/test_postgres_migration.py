"""真实样本 SQLite → 本地 PostgreSQL 迁移测试；源库只读，目标为独立测试库。

模拟场景：
- 保留 Episode、产物、审核、审批、账本与当前交付，迁移后逐表对账
- 重复迁移返回 reused，不重复插入
- 保留原 id 供现有引用和未来入队使用

默认跳过；需 PODCAST_TEST_POSTGRES=1 与本地测试库 DSN。
"""
import os
from pathlib import Path

import pytest
from psycopg import sql

from state.postgres import db, migrations
from state.postgres.migrate_from_sqlite import TABLE_ORDER, migrate_from_sqlite, verify

pytestmark = pytest.mark.skipif(
    os.environ.get("PODCAST_TEST_POSTGRES") != "1",
    reason="需要显式启用本地 PostgreSQL 迁移测试库",
)


def test_real_sample_migration_is_complete_and_idempotent():
    source_path = Path("var/podcast_agent.db")
    if not source_path.exists():
        pytest.skip("真实样本 SQLite 不存在")
    conn = db.connect()
    try:
        migrations.migrate(conn)
        identifiers = sql.SQL(", ").join(map(sql.Identifier, TABLE_ORDER))
        conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(identifiers))
        conn.commit()
        result = migrate_from_sqlite(conn, source_path)
        assert result["reused"] is False
        assert result["mismatches"] == []
        assert result["tables"]["episode"] >= 2
        assert result["tables"]["artifact"] > 0
        assert result["tables"]["approval"] > 0
        assert result["tables"]["delivery"] > 0
        source = __import__('sqlite3').connect(f"file:{source_path}?mode=ro", uri=True)
        source.row_factory = __import__('sqlite3').Row
        try:
            assert verify(source, conn) == []
        finally:
            source.close()
        second = migrate_from_sqlite(conn, source_path)
        assert second["reused"] is True
    finally:
        conn.close()
