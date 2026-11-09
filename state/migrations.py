"""跨层数据契约·SQLite 版本迁移。

新库直接创建最新 schema；旧三表库升级到审核与发布契约。
迁移不读取或修改工作区文件，既有 reviewing 记录默认暂停自动审批。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_LEGACY_TABLES = {"episode", "artifact", "ledger"}


class MigrationError(RuntimeError):
    """无法安全迁移；信息用于对账，不静默丢弃或重排旧数据。"""


def _schema_statements() -> list[str]:
    """按 sqlite3.complete_statement 拆分，避免 executescript 隐式提交。"""
    statements: list[str] = []
    current = ""
    for line in SCHEMA_PATH.read_text(encoding="utf-8").splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            if current.strip():
                statements.append(current)
            current = ""
    if current.strip():
        raise MigrationError("schema.sql 末尾不是完整 SQL 语句")
    return statements


def _execute_latest_schema(conn: sqlite3.Connection) -> None:
    for statement in _schema_statements():
        conn.execute(statement)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _assert_no_duplicate_artifacts(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT episode_id, step, path, version, COUNT(*) AS amount
        FROM artifact
        GROUP BY episode_id, step, path, version
        HAVING COUNT(*) > 1
        LIMIT 5
        """
    ).fetchall()
    if rows:
        detail = [
            {
                "episode_id": row[0],
                "step": row[1],
                "path": row[2],
                "version": row[3],
                "count": row[4],
            }
            for row in rows
        ]
        raise MigrationError(f"artifact 存在重复身份，需先人工对账: {detail}")


def _create_episode_replacement(conn: sqlite3.Connection) -> None:
    episode_statement = next(
        statement
        for statement in _schema_statements()
        if "CREATE TABLE IF NOT EXISTS episode (" in statement
    )
    conn.execute(
        episode_statement.replace(
            "CREATE TABLE IF NOT EXISTS episode", "CREATE TABLE episode_new", 1
        )
    )
    columns = (
        "id, guid, feed, feed_snapshot, title, published_at, duration_secs, "
        "enclosure, audio_sha256, status, crash_count, created_at, updated_at"
    )
    conn.execute(
        f"INSERT INTO episode_new ({columns}) SELECT {columns} FROM episode"
    )


def _backfill_review_revisions(conn: sqlite3.Connection) -> None:
    """为旧 reviewing Episode 建立暂停的当前修订；缺产物则拒绝迁移。"""
    rows = conn.execute(
        """
        SELECT
            e.id,
            e.updated_at,
            (
                SELECT a.id FROM artifact a
                WHERE a.episode_id = e.id AND a.step = 'split' AND a.status = 'done'
                ORDER BY a.version DESC, a.id DESC LIMIT 1
            ) AS split_id,
            (
                SELECT a.id FROM artifact a
                WHERE a.episode_id = e.id AND a.step = 'review' AND a.status = 'done'
                ORDER BY a.version DESC, a.id DESC LIMIT 1
            ) AS review_id
        FROM episode e
        WHERE e.status = 'reviewing'
        ORDER BY e.id
        """
    ).fetchall()
    missing = [int(row[0]) for row in rows if row[2] is None and row[3] is None]
    if missing:
        raise MigrationError(
            f"旧 reviewing Episode 缺少 done 的 split/review artifact，需先对账: {missing}"
        )
    for row in rows:
        artifact_id = row[2] if row[2] is not None else row[3]
        variant = "split" if row[2] is not None else "review"
        conn.execute(
            """
            INSERT INTO review_revision (
                episode_id, base_artifact_id, rendered_artifact_id, variant,
                status, is_current, reviewing_since,
                auto_approve_at, auto_approve_paused, supersedes_revision_id
            ) VALUES (?, ?, ?, ?, 'reviewing', 1, ?, NULL, 1, NULL)
            """,
            (row[0], artifact_id, artifact_id, variant, row[1]),
        )


def _migrate_legacy_to_v1(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise MigrationError("迁移前存在未提交事务；请先提交或回滚")
    _assert_no_duplicate_artifacts(conn)
    foreign_keys = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    try:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        _create_episode_replacement(conn)
        conn.execute("DROP TABLE episode")
        conn.execute("ALTER TABLE episode_new RENAME TO episode")
        _execute_latest_schema(conn)
        _backfill_review_revisions(conn)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MigrationError(f"迁移后外键检查失败: {violations[:5]}")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")


def migrate(conn: sqlite3.Connection) -> None:
    """将空库或受支持旧库迁移到最新版本；重复调用无副作用。"""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        raise MigrationError(f"数据库版本 {version} 高于代码支持版本 {SCHEMA_VERSION}")
    if version != 0:
        raise MigrationError(f"不支持的数据库版本: {version}")

    tables = _table_names(conn)
    if not tables:
        _execute_latest_schema(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        return
    if not _LEGACY_TABLES.issubset(tables):
        raise MigrationError(f"无法识别的旧库表结构: {sorted(tables)}")
    if "review_revision" in tables:
        raise MigrationError("user_version=0 但已存在 review_revision，需先检查迁移状态")
    _migrate_legacy_to_v1(conn)
