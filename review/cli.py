"""审核层·Node 与 cron 的窄 JSON CLI；不包含 HTTP 或另一套领域规则。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Optional

from review import queries, service
from state import db, ledger_report


def _payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("CLI 输入必须是 JSON object")
    return value


def _open(path: Optional[str]) -> sqlite3.Connection:
    if path:
        conn = db.connect(path)
        db.init_schema(conn)
        return conn
    return db.open_default()


def _dispatch(conn: sqlite3.Connection, command: str, payload: dict, now: str) -> dict:
    if command == "usage-report":
        return ledger_report.usage_report(
            conn,
            episode_id=payload.get("episode_id"),
            purpose=payload.get("purpose"),
            status=payload.get("status"),
            provider=payload.get("provider"),
            limit=payload.get("limit", 500),
        )
    if command == "inbox":
        return queries.inbox(conn)
    if command == "detail":
        return queries.detail(conn, payload["episode_id"])
    if command == "media-info":
        return queries.media_info(conn, payload["episode_id"])
    if command == "create-revision":
        return service.create_review_revision(
            conn,
            episode_id=payload["episode_id"],
            base_artifact_id=payload["base_artifact_id"],
            rendered_artifact_id=payload.get("rendered_artifact_id"),
            variant=payload["variant"],
            paused=bool(payload.get("paused", False)),
            now=now,
        )
    if command == "save-override":
        return service.save_override(
            conn,
            episode_id=payload["episode_id"],
            base_revision_id=payload["base_revision_id"],
            base_entry_number=payload["base_entry_number"],
            source_artifact_id=payload["source_artifact_id"],
            start_ms=payload["start_ms"],
            end_ms=payload["end_ms"],
            source_text=payload["source_text"],
            after_text=payload.get("after_text", ""),
            supersedes_override_id=payload.get("supersedes_override_id"),
            retracted=bool(payload.get("retracted", False)),
            request_id=payload["request_id"],
            now=now,
        )
    if command == "select-variant":
        return service.select_variant(
            conn,
            episode_id=payload["episode_id"],
            base_revision_id=payload["base_revision_id"],
            variant=payload["variant"],
            now=now,
        )
    if command == "hold":
        return service.hold_review(
            conn, episode_id=payload["episode_id"], base_revision_id=payload["base_revision_id"]
        )
    if command == "resume":
        return service.resume_review(
            conn,
            episode_id=payload["episode_id"],
            base_revision_id=payload["base_revision_id"],
            now=now,
        )
    if command == "approve":
        return service.approve(
            conn,
            episode_id=payload["episode_id"],
            base_revision_id=payload["base_revision_id"],
            method=payload.get("method", "manual"),
            request_id=payload["request_id"],
            now=now,
        )
    if command == "reject":
        return service.reject(
            conn,
            episode_id=payload["episode_id"],
            base_revision_id=payload["base_revision_id"],
            reason=payload["reason"],
            ranges=payload["ranges"],
            request_id=payload["request_id"],
            now=now,
        )
    if command == "revise-published":
        return service.revise_published(
            conn,
            episode_id=payload["episode_id"],
            base_revision_id=payload.get("base_revision_id"),
            now=now,
        )
    if command == "auto-approve-due":
        return service.auto_approve_due(conn, now=now)
    raise ValueError(f"未知 review command: {command}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="审核领域 JSON CLI")
    parser.add_argument("command")
    parser.add_argument("--db")
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    try:
        payload = _payload()
        now = service._canonical_time(args.now or service.utcnow_iso())
        conn = _open(args.db)
        try:
            data = _dispatch(conn, args.command, payload, now)
        finally:
            conn.close()
        if args.command in {"save-override", "select-variant"} and data.get("has_conflicts"):
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": {
                            "code": "override_conflict",
                            "message": "修改已保存，但存在需要人工处理的映射冲突",
                            "details": data,
                        },
                    },
                    ensure_ascii=False,
                )
            )
            return 1
        print(json.dumps({"status": "success", "data": data}, ensure_ascii=False))
        return 0
    except service.ReviewError as exc:
        result = {
            "status": "failed",
            "error": {"code": exc.code, "message": exc.message, "details": exc.details},
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "status": "failed",
            "error": {
                "code": "validation_error",
                "message": f"CLI 输入或状态校验失败: {exc}",
                "details": {},
            },
        }
    except (sqlite3.Error, OSError):
        result = {
            "status": "failed",
            "error": {"code": "internal_error", "message": "本地审核操作失败", "details": {}},
        }
    print(json.dumps(result, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
