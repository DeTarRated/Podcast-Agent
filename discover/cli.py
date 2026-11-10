"""发现层·feed 管理与轮询 CLI；输出单个 JSON，不包含模型或下载调用。"""
from __future__ import annotations

import argparse
import json
import sqlite3

from discover import poller
from discover.classify import DeepSeekClassifier
from pipeline.scribe import _load_dotenv
from state import db, feed as feed_state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RSS 发现与 Episode 入队")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("feed-add", parents=[common])
    add.add_argument("--name", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--show")
    add.add_argument("--process-existing", action="store_true", help="显式允许处理首轮存量节目")
    disable = sub.add_parser("feed-disable", parents=[common])
    disable.add_argument("feed_id", type=int)
    sub.add_parser("feed-list", parents=[common])
    poll = sub.add_parser("poll", parents=[common])
    poll.add_argument("--feed-id", type=int)
    poll.add_argument("--now")
    poll.add_argument("--classify", action="store_true", help="对新候选启用 Chelsea 付费分类")
    args = parser.parse_args(argv)
    _load_dotenv()
    conn = db.connect(args.db) if args.db else db.open_default()
    if args.db:
        db.init_schema(conn)
    try:
        if args.command == "feed-add":
            feed_id = feed_state.add(
                conn, name=args.name, feed_url=args.url, show_name=args.show,
                process_existing=args.process_existing,
            )
            result = {"status": "success", "feed_id": feed_id}
        elif args.command == "feed-disable":
            feed_state.disable(conn, args.feed_id)
            result = {"status": "success", "feed_id": args.feed_id, "enabled": False}
        elif args.command == "feed-list":
            result = {
                "status": "success",
                "items": [dict(row) for row in feed_state.list_feeds(conn)],
            }
        else:
            classifier = DeepSeekClassifier.from_env() if args.classify else None
            result = poller.poll_all(
                conn, now=args.now, feed_id=args.feed_id, classifier=classifier
            )
    except (ValueError, sqlite3.Error, poller.DiscoveryError) as exc:
        result = {"status": "failed", "error": str(exc)}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
