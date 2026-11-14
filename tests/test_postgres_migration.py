"""真实样本 SQLite → 本地 PostgreSQL 迁移测试；源库只读，目标为独立测试库。

模拟场景：
- 保留 Episode、产物、审核、审批、账本与当前交付，迁移后逐表对账
- 重复迁移返回 reused，不重复插入
- 保留原 id 供现有引用和未来入队使用
- 新 Episode 单期幂等同步后可满足 job 外键并入队

默认跳过；需 PODCAST_TEST_POSTGRES=1 与本地测试库 DSN。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from psycopg import sql

from state.postgres import db, migrations
from state.postgres.migrate_from_sqlite import TABLE_ORDER, migrate_from_sqlite, sync_episode, verify

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


def test_single_episode_sync_is_idempotent_and_allows_job_enqueue(tmp_path):
    from state import db as sqlite_db
    from state import episode

    source_path = tmp_path / "single.db"
    source = sqlite_db.connect(source_path)
    sqlite_db.init_schema(source)
    episode_id = episode.create(
        source,
        guid="new-episode-guid",
        feed="https://example.com/feed.xml",
        feed_snapshot=json.dumps({"id": 1, "name": "Feed"}),
        title="New episode",
        published_at="2026-11-13T00:00:00+00:00",
        enclosure="https://example.com/episode.mp3",
    )
    source.close()

    conn = db.connect()
    try:
        migrations.migrate(conn)
        identifiers = sql.SQL(", ").join(map(sql.Identifier, TABLE_ORDER + ["job"]))
        conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(identifiers))
        conn.commit()
        assert sync_episode(conn, source_path, episode_id)["reused"] is False
        assert sync_episode(conn, source_path, episode_id)["reused"] is True

        env = dict(os.environ)
        env["PODCAST_AGENT_DB"] = str(source_path)
        proc = subprocess.run(
            [
                sys.executable, "-m", "state.postgres.job_cli", "enqueue-episode",
                str(episode_id), "new-episode-guid", "--estimated-cost", "0.5", "--budget", "1",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["status"] == "success"
        assert payload["data"]["reused"] is False
        queued = conn.execute(
            "SELECT episode_id,status,budget FROM job WHERE episode_id = %s", (episode_id,)
        ).fetchone()
        assert queued["status"] == "ready"
        assert str(queued["budget"]) == "1"
    finally:
        conn.close()
