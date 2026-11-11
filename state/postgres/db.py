"""PostgreSQL 连接约定：psycopg 3、dict_row、UTC、本地开发 DSN。"""
from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row


def default_dsn() -> str:
    return os.environ.get(
        "PODCAST_POSTGRES_DSN",
        "postgresql://podcast_agent:podcast-dev@127.0.0.1:5432/podcast_agent",
    )


def connect(dsn: str | None = None):
    return psycopg.connect(
        dsn or default_dsn(),
        row_factory=dict_row,
        connect_timeout=10,
        options="-c TimeZone=UTC",
    )
