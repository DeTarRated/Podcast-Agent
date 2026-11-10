"""执行租约与待处理调度测试；全部使用临时 SQLite。

模拟场景：
- 同 Episode 仅一个 owner，同 owner 续约，其他 owner 被拒绝
- 过期租约可接管，release 仅允许持锁者
- discovered 待处理按发布时间与 limit 返回
- v2 → v3 迁移保留 Episode 并创建 execution_lock
"""
import pytest

from state import db, episode, execution
from state.migrations import SCHEMA_VERSION

START = "2026-11-09T12:00:00+00:00"
SOON = "2026-11-09T12:00:30+00:00"
LATER = "2026-11-09T13:00:00+00:00"
EXPIRED = "2026-11-09T19:00:00+00:00"


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "locks.db")
    db.init_schema(connection)
    yield connection
    connection.close()


def test_lock_exclusion_heartbeat_takeover_and_release(conn):
    episode_id = episode.create(conn, guid="g", feed="f")
    first = execution.acquire(conn, episode_id=episode_id, owner="a", now=START, lease_seconds=60)
    assert first["acquired"] is True
    blocked = execution.acquire(conn, episode_id=episode_id, owner="b", now=SOON, lease_seconds=60)
    assert blocked["acquired"] is False
    renewed = execution.acquire(conn, episode_id=episode_id, owner="a", now=SOON, lease_seconds=3600)
    assert renewed["reused"] is True
    assert renewed["expires_at"] == "2026-11-09T13:00:30+00:00"
    assert execution.release(conn, episode_id=episode_id, owner="b") is False
    assert execution.release(conn, episode_id=episode_id, owner="a") is True
    takeover = execution.acquire(conn, episode_id=episode_id, owner="b", now=LATER)
    assert takeover["acquired"] is True

    expiring_episode = episode.create(conn, guid="expiring", feed="f")
    initial = execution.acquire(conn, episode_id=expiring_episode, owner="d", now=START, lease_seconds=1)
    assert initial["acquired"] is True
    takeover = execution.acquire(conn, episode_id=expiring_episode, owner="c", now=START, lease_seconds=1)
    assert takeover["acquired"] is False
    after_expiry = execution.acquire(conn, episode_id=expiring_episode, owner="c", now="2026-11-09T12:00:02+00:00")
    assert after_expiry["acquired"] is True


def test_pending_episodes_order_and_limit(conn):
    older = episode.create(conn, guid="older", feed="f", published_at="2026-11-01T00:00:00+00:00")
    newer = episode.create(conn, guid="newer", feed="f", published_at="2026-11-02T00:00:00+00:00")
    episode.create(conn, guid="no-date", feed="f")
    assert [row["id"] for row in execution.pending_episodes(conn, limit=2)] == [older, newer]


def test_v2_to_v3_migration_preserves_episode(tmp_path):
    conn = db.connect(tmp_path / "v2.db")
    db.init_schema(conn)
    episode_id = episode.create(conn, guid="existing", feed="f")
    conn.execute("DROP TABLE execution_lock")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert episode.get(conn, episode_id)["guid"] == "existing"
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='execution_lock'"
    ).fetchone()[0] == 1
    conn.close()
