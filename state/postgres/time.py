"""PostgreSQL 领域共享的 UTC 时间解析。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _now(value: Optional[str | datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("时间必须包含时区")
        return value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)
