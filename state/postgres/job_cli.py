"""PostgreSQL 队列 CLI：供调度器调用，所有任务通过同一领域实现。"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal

from state.postgres import db, job, migrations


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL 任务队列 CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue-episode")
    enqueue.add_argument("episode_id", type=int)
    enqueue.add_argument("guid")
    enqueue.add_argument("--estimated-cost")
    enqueue.add_argument("--budget")
    claim = sub.add_parser("claim")
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--now")
    claim.add_argument("--lease-seconds", type=int, default=300)
    claim.add_argument("--kinds")
    complete = sub.add_parser("complete")
    complete.add_argument("job_id", type=int)
    complete.add_argument("--worker-id", required=True)
    complete.add_argument("--result", default="{}")
    complete.add_argument("--usage")
    complete.add_argument("--now")
    fail = sub.add_parser("fail")
    fail.add_argument("job_id", type=int)
    fail.add_argument("--worker-id", required=True)
    fail.add_argument("--error", required=True)
    fail.add_argument("--retryable", action="store_true", default=False)
    fail.add_argument("--unknown", action="store_true")
    fail.add_argument("--now")
    heartbeat = sub.add_parser("heartbeat")
    heartbeat.add_argument("job_id", type=int)
    heartbeat.add_argument("--worker-id", required=True)
    heartbeat.add_argument("--lease-seconds", type=int, default=300)
    heartbeat.add_argument("--now")
    sub.add_parser("stats")
    parser.add_argument("--dsn")
    args = parser.parse_args(argv)
    conn = db.connect(args.dsn)
    try:
        migrations.migrate(conn)
        if args.command == "enqueue-episode":
            payload = {"episode_id": args.episode_id, "guid": args.guid}
            if args.estimated_cost is not None:
                payload["estimated_cost"] = args.estimated_cost
            result = job.enqueue_job(
                conn,
                kind="episode",
                episode_id=args.episode_id,
                payload=payload,
                idempotency_key=f"episode:{args.episode_id}:auto:v1",
                budget=Decimal(args.budget) if args.budget is not None else None,
            )
        elif args.command == "claim":
            result = job.claim_job(
                conn,
                worker_id=args.worker_id,
                now=args.now,
                lease_seconds=args.lease_seconds,
                kinds=args.kinds.split(",") if args.kinds else None,
            )
        elif args.command == "complete":
            result = job.complete_job(
                conn,
                job_id=args.job_id,
                worker_id=args.worker_id,
                result=json.loads(args.result),
                usage=json.loads(args.usage) if args.usage else None,
                now=args.now,
            )
        elif args.command == "fail":
            result = job.fail_job(
                conn,
                job_id=args.job_id,
                worker_id=args.worker_id,
                error=args.error,
                retryable=args.retryable,
                unknown=args.unknown,
                now=args.now,
            )
        elif args.command == "heartbeat":
            result = {"updated": job.heartbeat_job(
                conn, job_id=args.job_id, worker_id=args.worker_id,
                now=args.now, lease_seconds=args.lease_seconds,
            )}
        else:
            result = job.queue_stats(conn)
        print(json.dumps({"status": "success", "data": result}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
