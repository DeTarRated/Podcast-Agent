"""审核与发布 state 契约测试；全部使用临时 SQLite。

模拟场景：
- 新库创建最新 schema 与 user_version，Episode 支持 rejected
- 旧三表库迁移保留 Episode、artifact、ledger，并生成默认暂停的审核修订
- 缺少审核产物或 artifact 身份重复的旧库拒绝迁移且不留下半套表
- artifact 身份、当前审核修订与当前发布的唯一约束
- update_status / manifest 的 commit=False 可由外层事务回滚
"""
import sqlite3

import pytest

from state import db, episode, manifest
from state.migrations import MigrationError, SCHEMA_VERSION

LEGACY_SCHEMA = """
CREATE TABLE episode (
    id INTEGER PRIMARY KEY,
    guid TEXT NOT NULL,
    feed TEXT NOT NULL,
    feed_snapshot TEXT,
    title TEXT,
    published_at TEXT,
    duration_secs REAL,
    enclosure TEXT,
    audio_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered','downloaded','transcribed','translated',
                          'reviewing','approved','delivered','failed','parked')),
    crash_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (guid, feed)
);
CREATE TABLE artifact (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_artifact_episode ON artifact(episode_id);
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER REFERENCES episode(id),
    purpose TEXT NOT NULL,
    triggered_at TEXT NOT NULL DEFAULT (datetime('now')),
    provider TEXT,
    model TEXT,
    usage_basis TEXT,
    amount_est REAL,
    call_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'intent' CHECK (status IN ('intent','success','failed','unknown'))
);
"""


def _legacy_connection(tmp_path):
    conn = db.connect(tmp_path / "legacy.db")
    conn.executescript(LEGACY_SCHEMA)
    return conn


def _insert_legacy_episode(conn, *, status="reviewing"):
    cur = conn.execute(
        """
        INSERT INTO episode (id, guid, feed, status, updated_at)
        VALUES (7, 'legacy-guid', 'legacy-feed', ?, '2026-11-01T00:00:00+00:00')
        """,
        (status,),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_legacy_artifact(conn, episode_id, artifact_id, step, path=None):
    conn.execute(
        """
        INSERT INTO artifact (id, episode_id, step, kind, path, version, sha256, status)
        VALUES (?, ?, ?, 'srt', ?, 1, 'abc', 'done')
        """,
        (artifact_id, episode_id, step, path or f"var/episodes/{episode_id}/{step}.srt"),
    )


def test_new_schema_version_and_rejected_status(tmp_path):
    conn = db.connect(tmp_path / "new.db")
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    episode_id = episode.create(conn, guid="g", feed="f")
    episode.update_status(conn, episode_id, "rejected")
    assert episode.get(conn, episode_id)["status"] == "rejected"
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"review_revision", "override", "override_application", "approval", "delivery"} <= tables
    conn.close()


def test_legacy_migration_preserves_data_and_pauses_reviewing(tmp_path):
    conn = _legacy_connection(tmp_path)
    episode_id = _insert_legacy_episode(conn)
    _insert_legacy_artifact(conn, episode_id, 11, "review", "var/episodes/7/en_zh_fix.srt")
    _insert_legacy_artifact(conn, episode_id, 12, "split", "var/episodes/7/en_zh_split.srt")
    conn.execute(
        """
        INSERT INTO ledger (episode_id, purpose, call_ref, status)
        VALUES (?, 'transcribe', 'legacy-call', 'success')
        """,
        (episode_id,),
    )
    conn.commit()

    db.init_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert episode.get(conn, episode_id)["guid"] == "legacy-guid"
    assert [row["id"] for row in manifest.list_for(conn, episode_id)] == [11, 12]
    assert conn.execute("SELECT status FROM ledger WHERE call_ref='legacy-call'").fetchone()[0] == "success"
    revision = conn.execute(
        "SELECT * FROM review_revision WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    assert revision["base_artifact_id"] == 12
    assert revision["rendered_artifact_id"] == 12
    assert revision["variant"] == "split"
    assert revision["auto_approve_paused"] == 1
    assert revision["auto_approve_at"] is None
    assert revision["reviewing_since"] == "2026-11-01T00:00:00+00:00"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    db.init_schema(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM review_revision WHERE episode_id = ?", (episode_id,)
    ).fetchone()[0] == 1
    conn.close()


def test_migration_rejects_reviewing_without_artifact(tmp_path):
    conn = _legacy_connection(tmp_path)
    _insert_legacy_episode(conn)
    with pytest.raises(MigrationError, match="缺少 done"):
        db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM episode WHERE id=7").fetchone()[0] == "reviewing"
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='review_revision'"
    ).fetchone()[0] == 0
    conn.close()


def test_migration_rejects_duplicate_artifact_identity(tmp_path):
    conn = _legacy_connection(tmp_path)
    episode_id = _insert_legacy_episode(conn, status="transcribed")
    _insert_legacy_artifact(conn, episode_id, 1, "review", "same.srt")
    _insert_legacy_artifact(conn, episode_id, 2, "review", "same.srt")
    conn.commit()
    with pytest.raises(MigrationError, match="重复身份"):
        db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM artifact").fetchone()[0] == 2
    conn.close()


def test_artifact_identity_unique(tmp_path):
    conn = db.connect(tmp_path / "unique.db")
    db.init_schema(conn)
    episode_id = episode.create(conn, guid="g", feed="f")
    manifest.add(conn, episode_id=episode_id, step="review", kind="srt", path="same.srt")
    with pytest.raises(sqlite3.IntegrityError):
        manifest.add(conn, episode_id=episode_id, step="review", kind="srt", path="same.srt")
    conn.close()


def _artifact(conn, episode_id, step, path):
    artifact_id = manifest.add(conn, episode_id=episode_id, step=step, kind="srt", path=path)
    manifest.finish(conn, artifact_id, sha256="a" * 64)
    return artifact_id


def _revision(conn, episode_id, artifact_id, *, current, status="approved"):
    cur = conn.execute(
        """
        INSERT INTO review_revision (
            episode_id, base_artifact_id, rendered_artifact_id, variant,
            status, is_current, reviewing_since
        ) VALUES (?, ?, ?, 'split', ?, ?, '2026-11-01T00:00:00+00:00')
        """,
        (episode_id, artifact_id, artifact_id, status, int(current)),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_current_review_revision_unique(tmp_path):
    conn = db.connect(tmp_path / "current-review.db")
    db.init_schema(conn)
    episode_id = episode.create(conn, guid="g", feed="f")
    first = _artifact(conn, episode_id, "review", "a.srt")
    second = _artifact(conn, episode_id, "split", "b.srt")
    _revision(conn, episode_id, first, current=True, status="reviewing")
    with pytest.raises(sqlite3.IntegrityError):
        _revision(conn, episode_id, second, current=True, status="reviewing")
    conn.close()


def test_current_delivery_unique(tmp_path):
    conn = db.connect(tmp_path / "current-delivery.db")
    db.init_schema(conn)
    episode_id = episode.create(conn, guid="g", feed="f")
    first = _artifact(conn, episode_id, "review", "a.srt")
    second = _artifact(conn, episode_id, "split", "b.srt")
    revision_one = _revision(conn, episode_id, first, current=True)
    revision_two = _revision(conn, episode_id, second, current=False)
    approvals = []
    for revision, artifact, request in (
        (revision_one, first, "approve-a"),
        (revision_two, second, "approve-b"),
    ):
        cur = conn.execute(
            """
            INSERT INTO approval (review_revision_id, episode_id, artifact_id, sha256, method, request_id)
            VALUES (?, ?, ?, ?, 'manual', ?)
            """,
            (revision, episode_id, artifact, "a" * 64, request),
        )
        approvals.append(int(cur.lastrowid))
    for approval, artifact, current in ((approvals[0], first, 1), (approvals[1], second, 1)):
        if current == 1 and approval == approvals[1]:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO delivery (approval_id, episode_id, artifact_id, status, is_current, sha256)
                    VALUES (?, ?, ?, 'delivered', ?, ?)
                    """,
                    (approval, episode_id, artifact, current, "a" * 64),
                )
        else:
            conn.execute(
                """
                INSERT INTO delivery (approval_id, episode_id, artifact_id, status, is_current, sha256)
                VALUES (?, ?, ?, 'delivered', ?, ?)
                """,
                (approval, episode_id, artifact, current, "a" * 64),
            )
    conn.close()


def test_commit_false_allows_outer_rollback(tmp_path):
    conn = db.connect(tmp_path / "rollback.db")
    db.init_schema(conn)
    episode_id = episode.create(conn, guid="g", feed="f")
    conn.execute("BEGIN")
    episode.update_status(conn, episode_id, "reviewing", commit=False)
    artifact_id = manifest.add(
        conn, episode_id=episode_id, step="review", kind="srt", path="a.srt", commit=False
    )
    manifest.finish(conn, artifact_id, sha256="abc", commit=False)
    conn.rollback()
    assert episode.get(conn, episode_id)["status"] == "discovered"
    assert manifest.list_for(conn, episode_id) == []
    conn.close()
