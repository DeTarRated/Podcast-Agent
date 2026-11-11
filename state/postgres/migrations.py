"""PostgreSQL 版本迁移：空库创建最新 schema，已有库必须登记兼容版本。"""
from __future__ import annotations

from pathlib import Path

from state.postgres.db import connect

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "5"


def migrate(conn) -> bool:
    """返回是否执行了新迁移；事务完成后显式提交，连接保持可用。"""
    migrated = False
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        row = conn.execute(
            "SELECT version FROM schema_migration ORDER BY applied_at DESC, version DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            if row["version"] == SCHEMA_VERSION:
                migrated = False
            else:
                if row["version"] == "1":
                    conn.execute("ALTER TABLE job_attempt ALTER COLUMN status SET DEFAULT 'running'")
                    conn.execute("INSERT INTO schema_migration (version) VALUES ('2')")
                    row["version"] = "2"
                if row["version"] == "2":
                    conn.execute("ALTER TABLE ledger ALTER COLUMN amount_est TYPE NUMERIC")
                    conn.execute("ALTER TABLE job ALTER COLUMN budget TYPE NUMERIC")
                    conn.execute("INSERT INTO schema_migration (version) VALUES ('3')")
                    row["version"] = "3"
                if row["version"] == "3":
                    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migration (version) VALUES ('4')")
                    row["version"] = "4"
                if row["version"] == "4":
                    conn.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS job_kind_check")
                    conn.execute(
                        """
                        ALTER TABLE job ADD CONSTRAINT job_kind_check
                        CHECK (kind IN ('episode','download','transcribe','translate','review','highlights','publish','rerun'))
                        """
                    )
                    conn.execute("INSERT INTO schema_migration (version) VALUES (%s)", (SCHEMA_VERSION,))
                    row["version"] = SCHEMA_VERSION
                migrated = True
        else:
            tables = conn.execute(
                """
                SELECT COUNT(*) AS count FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name <> 'schema_migration'
                """
            ).fetchone()
            if tables["count"] != 0:
                raise RuntimeError("存在未登记版本的 PostgreSQL 业务表，拒绝直接覆盖")
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migration (version) VALUES (%s)", (SCHEMA_VERSION,))
            migrated = True
    conn.commit()
    return migrated


def main() -> int:
    conn = connect()
    try:
        migrated = migrate(conn)
        print({"status": "success", "version": SCHEMA_VERSION, "migrated": migrated})
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
