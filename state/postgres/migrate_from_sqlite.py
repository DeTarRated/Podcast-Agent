"""SQLite → PostgreSQL 安全迁移：保留 id、版本、hash 与账本，迁移后对账。

只读 SQLite；目标 PostgreSQL 使用单事务。非空目标只允许完全相同的幂等复查。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from state.postgres import migrations
from state.postgres.db import connect

TABLE_ORDER = [
    "feed",
    "episode",
    "artifact",
    "discovery_candidate",
    "ledger",
    "review_revision",
    "execution_lock",
    "override",
    "override_application",
    "approval",
    "rerun_request",
    "delivery",
]

BOOLEAN_FIELDS = {
    "enabled", "bootstrap_pending", "classification_relevant", "is_current",
    "auto_approve_paused", "retracted",
}
JSON_FIELDS = {"feed_snapshot", "ranges_json", "override_ids_json", "highlights_json"}
TIMESTAMP_FIELDS = {
    "published_at", "last_checked_at", "created_at", "updated_at", "triggered_at",
    "reviewing_since", "auto_approve_at", "acquired_at", "expires_at",
}


def _timestamp(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _convert(name: str, value):
    if value is None:
        return None
    if name in BOOLEAN_FIELDS:
        return bool(value)
    if name in JSON_FIELDS:
        return Jsonb(json.loads(value) if isinstance(value, str) else value)
    if name in TIMESTAMP_FIELDS:
        return _timestamp(value)
    if name == "amount_est":
        return Decimal(str(value))
    return value


def _counts(conn, table: str) -> int:
    if isinstance(conn, sqlite3.Connection):
        row = conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()
    else:
        row = conn.execute(sql.SQL("SELECT COUNT(*) AS count FROM {}").format(sql.Identifier(table))).fetchone()
    return row[0] if isinstance(row, tuple) else row["count"]


def _critical_mismatches(source, target) -> list[str]:
    checks = {
        "artifact": ["episode_id", "step", "path", "version", "sha256", "status"],
        "ledger": ["episode_id", "purpose", "call_ref", "status", "usage_basis", "amount_est"],
        "approval": ["review_revision_id", "episode_id", "artifact_id", "sha256", "method", "request_id"],
        "delivery": ["approval_id", "episode_id", "artifact_id", "status", "is_current", "sha256"],
        "review_revision": ["episode_id", "base_artifact_id", "rendered_artifact_id", "variant", "status", "is_current"],
    }
    mismatches = []
    for table, fields in checks.items():
        source_rows = {row["id"]: row for row in source.execute(f"SELECT * FROM {table}").fetchall()}
        target_rows = {
            row["id"]: row
            for row in target.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
        }
        if set(source_rows) != set(target_rows):
            mismatches.append(f"{table}:id_set")
            continue
        for row_id, source_row in source_rows.items():
            for field in fields:
                left = source_row[field]
                right = target_rows[row_id][field]
                if field == "amount_est" and left is not None and right is not None:
                    equal = Decimal(str(left)) == Decimal(str(right))
                else:
                    equal = left == right
                if not equal:
                    mismatches.append(f"{table}:{row_id}:{field}")
    return mismatches


def verify(source, target) -> list[str]:
    mismatches = []
    for table in TABLE_ORDER:
        if _counts(source, table) != _counts(target, table):
            mismatches.append(f"{table}:count")
    mismatches.extend(_critical_mismatches(source, target))
    return mismatches


def sync_episode(conn, source_path: Path, episode_id: int) -> dict:
    """入队前幂等同步单期，满足 job 外键；不覆盖已有不同身份的记录。"""
    if not source_path.exists():
        raise RuntimeError(f"SQLite 源库不存在: {source_path}")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        row = source.execute("SELECT * FROM episode WHERE id = ?", (episode_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"SQLite Episode 不存在: {episode_id}")
        columns = row.keys()
        values = [_convert(column, row[column]) for column in columns]
        with conn.transaction():
            was_existing = conn.execute(
                "SELECT 1 FROM episode WHERE id = %s", (episode_id,)
            ).fetchone()
            conn.execute(
                sql.SQL("INSERT INTO episode ({}) VALUES ({}) ON CONFLICT (id) DO NOTHING").format(
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join([sql.Placeholder()] * len(columns)),
                ),
                values,
            )
            existing = conn.execute(
                "SELECT guid,feed FROM episode WHERE id = %s", (episode_id,)
            ).fetchone()
            if existing is None or (existing["guid"], existing["feed"]) != (row["guid"], row["feed"]):
                raise RuntimeError(f"PostgreSQL Episode id 与 SQLite 身份不一致: {episode_id}")
            conn.execute(
                "SELECT setval(pg_get_serial_sequence('episode','id'), (SELECT MAX(id) FROM episode), true)"
            )
        conn.commit()
        return {"episode_id": episode_id, "reused": was_existing is not None}
    finally:
        source.close()


def migrate_from_sqlite(conn, source_path: Path) -> dict:
    if not source_path.exists():
        raise RuntimeError(f"SQLite 源库不存在: {source_path}")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        target_nonempty = any(_counts(conn, table) for table in TABLE_ORDER)
        if target_nonempty:
            mismatches = verify(source, conn)
            if mismatches:
                raise RuntimeError(f"目标库与源库不一致，拒绝混合迁移: {mismatches[:20]}")
            return {"status": "success", "reused": True, "tables": {}, "mismatches": []}

        report = {}
        with conn.transaction():
            for table in TABLE_ORDER:
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
                for row in rows:
                    columns = row.keys()
                    values = [_convert(column, row[column]) for column in columns]
                    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(", ").join(map(sql.Identifier, columns)),
                        sql.SQL(", ").join([sql.Placeholder()] * len(columns)),
                    )
                    conn.execute(statement, values)
                if rows and table != "execution_lock":
                    conn.execute(
                        sql.SQL(
                            """
                            SELECT setval(
                                pg_get_serial_sequence({}, 'id'),
                                (SELECT MAX(id) FROM {}), true
                            )
                            """
                        ).format(sql.Literal(table), sql.Identifier(table))
                    )
                report[table] = len(rows)
            mismatches = verify(source, conn)
            if mismatches:
                raise RuntimeError(f"迁移后对账失败: {mismatches[:20]}")
        conn.commit()
        return {"status": "success", "reused": False, "tables": report, "mismatches": []}
    finally:
        source.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL 原子迁移与对账")
    parser.add_argument("--source", default="var/podcast_agent.db")
    args = parser.parse_args(argv)
    conn = connect()
    try:
        migrations.migrate(conn)
        result = migrate_from_sqlite(conn, Path(args.source))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
