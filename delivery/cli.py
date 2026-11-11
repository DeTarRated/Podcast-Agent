"""交付层·发布、节目库与动态签名的窄 JSON CLI。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

from delivery import commands
from delivery.errors import DeliveryError
from delivery.highlights import HighlightsError
from pipeline.override import OverrideError
from pipeline.scribe import _load_dotenv
from state import db


def _payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("CLI 输入必须是 JSON object")
    return value


def _open(path: Optional[str]):
    if path:
        conn = db.connect(path)
        db.init_schema(conn)
        return conn
    return db.open_default()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="交付与私人播客库 JSON CLI")
    parser.add_argument(
        "command",
        choices=["publish-approved", "mark-highlights-failed", "library", "detail"],
    )
    parser.add_argument("--db")
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    _load_dotenv()
    try:
        payload = _payload()
        now = args.now or datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = _open(args.db)
        try:
            data = commands.dispatch(conn, args.command, payload, now=now)
        finally:
            conn.close()
        print(json.dumps({"status": "success", "data": data}, ensure_ascii=False))
        return 0
    except DeliveryError as exc:
        result = {"status": "failed", "error": {"code": exc.code, "message": exc.message, "details": exc.details}}
    except (KeyError, TypeError, ValueError, HighlightsError, OverrideError, OSError) as exc:
        result = {"status": "failed", "error": {"code": "validation_error", "message": str(exc), "details": {}}}
    except sqlite3.Error:
        result = {"status": "failed", "error": {"code": "internal_error", "message": "本地交付操作失败", "details": {}}}
    print(json.dumps(result, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
