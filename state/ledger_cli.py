"""执行层·账本窄 CLI：供 Node dispatch 适配层经 bash 调用。

只调用 state/ledger.py，不含另一套账本规则；Node 不直接拼 SQL。
用法：
  python -m state.ledger_cli intent  --episode-id 1 --purpose translate --provider deepseek \
      --model deepseek-flash --call-ref translate:1:v1 [--usage-basis ...] [--amount-est ...]
  python -m state.ledger_cli outcome --call-ref translate:1:v1 --status success \
      [--usage-basis "prompt_tokens=..;completion_tokens=.."] [--amount-est ...]
stdout 最后一行为单个 JSON；错误以非零退出。
"""
import argparse
import json
import os
import sys
from pathlib import Path

# 允许以模块方式运行时定位 .env（PODCAST_AGENT_DB 可能来自 .env）
from pipeline.scribe import _load_dotenv
from state import db, ledger


def main(argv=None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="账本窄 CLI（供 Node dispatch 调用）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_intent = sub.add_parser("intent")
    p_intent.add_argument("--episode-id", type=int, default=None)
    p_intent.add_argument("--purpose", required=True)
    p_intent.add_argument("--provider", default=None)
    p_intent.add_argument("--model", default=None)
    p_intent.add_argument("--call-ref", required=True)
    p_intent.add_argument("--usage-basis", default=None)
    p_intent.add_argument("--amount-est", type=float, default=None)

    p_outcome = sub.add_parser("outcome")
    p_outcome.add_argument("--call-ref", required=True)
    p_outcome.add_argument("--status", required=True)
    p_outcome.add_argument("--usage-basis", default=None)
    p_outcome.add_argument("--amount-est", type=float, default=None)

    p_reconcile = sub.add_parser("reconcile")
    p_reconcile.add_argument("--call-ref", required=True)
    p_reconcile.add_argument("--usage-basis", default=None)

    p_get = sub.add_parser("get")
    p_get.add_argument("--call-ref", required=True)

    for p in (p_intent, p_outcome, p_reconcile, p_get):
        p.add_argument("--db", default=None)

    args = parser.parse_args(argv)
    conn = db.connect(args.db) if args.db else db.open_default()
    if args.db:
        db.init_schema(conn)
    try:
        if args.command == "intent":
            ledger.record_intent(
                conn,
                episode_id=args.episode_id,
                purpose=args.purpose,
                provider=args.provider,
                model=args.model,
                usage_basis=args.usage_basis or "",
                amount_est=args.amount_est,
                call_ref=args.call_ref,
            )
            print(json.dumps({"status": "intent", "call_ref": args.call_ref}, ensure_ascii=False))
            return 0
        elif args.command == "reconcile":
            # unknown → success 核销；可顺带回填真实 usage
            ledger.reconcile_success(conn, args.call_ref)
            if args.usage_basis:
                conn.execute("UPDATE ledger SET usage_basis = ? WHERE call_ref = ?",
                             (args.usage_basis, args.call_ref))
                conn.commit()
            row = ledger.get_by_ref(conn, args.call_ref)
            print(json.dumps({"status": row["status"], "call_ref": args.call_ref,
                              "usage_basis": row["usage_basis"]}, ensure_ascii=False))
            return 0
        elif args.command == "get":
            row = ledger.get_by_ref(conn, args.call_ref)
            print(json.dumps({"status": row["status"] if row else "missing",
                              "call_ref": args.call_ref,
                              "usage_basis": row["usage_basis"] if row else None}, ensure_ascii=False))
            return 0
        else:
            ledger.record_outcome(
                conn,
                args.call_ref,
                status=args.status,
                amount_est=args.amount_est,
                usage_basis=args.usage_basis,
            )
            row = ledger.get_by_ref(conn, args.call_ref)
            print(json.dumps({
                "status": row["status"] if row else "missing",
                "call_ref": args.call_ref,
                "usage_basis": row["usage_basis"] if row else None,
            }, ensure_ascii=False))
            return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
