"""执行层·账本窄 CLI：供 Node dispatch 适配层经 bash 调用。

只调用 state/ledger.py，不含另一套账本规则；Node 不直接拼 SQL。
用法：
  python -m state.ledger_cli authorize --episode-id 1 --purpose translate --provider deepseek \
      --model deepseek-flash --call-ref translate:1:v1 --usage-basis ... --amount-est ... [--defer-start]
  python -m state.ledger_cli claim --call-ref translate:1:v1
  python -m state.ledger_cli outcome --call-ref translate:1:v1 --status success \
      [--usage-basis "prompt_tokens=..;completion_tokens=.."] [--amount-est ...]
stdout 最后一行为单个 JSON；错误以非零退出。
"""
import argparse
import json
from decimal import Decimal

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
    p_intent.add_argument("--amount-est", type=Decimal, default=None)

    p_authorize = sub.add_parser("authorize")
    p_authorize.add_argument("--episode-id", type=int, required=True)
    p_authorize.add_argument("--purpose", required=True)
    p_authorize.add_argument("--provider", required=True)
    p_authorize.add_argument("--model", required=True)
    p_authorize.add_argument("--call-ref", required=True)
    p_authorize.add_argument("--usage-basis", required=True)
    p_authorize.add_argument("--amount-est", type=Decimal, required=True)
    p_authorize.add_argument("--defer-start", action="store_true")

    p_reauthorize = sub.add_parser("reauthorize")
    p_reauthorize.add_argument("--call-ref", required=True)
    p_reauthorize.add_argument("--usage-basis", required=True)
    p_reauthorize.add_argument("--amount-est", type=Decimal, required=True)

    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--call-ref", required=True)

    p_outcome = sub.add_parser("outcome")
    p_outcome.add_argument("--call-ref", required=True)
    p_outcome.add_argument("--status", required=True)
    p_outcome.add_argument("--usage-basis", default=None)
    p_outcome.add_argument("--amount-est", type=Decimal, default=None)

    p_reconcile = sub.add_parser("reconcile")
    p_reconcile.add_argument("--call-ref", required=True)
    p_reconcile.add_argument("--usage-basis", default=None)
    p_reconcile.add_argument("--amount-est", type=Decimal, default=None)

    p_get = sub.add_parser("get")
    p_get.add_argument("--call-ref", required=True)

    p_totals = sub.add_parser("totals")

    for p in (p_intent, p_authorize, p_reauthorize, p_claim, p_outcome, p_reconcile, p_get, p_totals):
        p.add_argument("--db", default=None)

    args = parser.parse_args(argv)
    conn = db.connect(args.db) if args.db else db.open_default()
    if args.db:
        db.init_schema(conn)
    try:
        if args.command == "authorize":
            from pipeline.cost_guard import authorize_and_record_intent

            authorize_and_record_intent(
                conn,
                episode_id=args.episode_id,
                purpose=args.purpose,
                provider=args.provider,
                model=args.model,
                usage_basis=args.usage_basis,
                amount_est=args.amount_est,
                call_ref=args.call_ref,
                request_started=not args.defer_start,
            )
            print(json.dumps({"status": "success", "data": {"status": "intent", "call_ref": args.call_ref}}, ensure_ascii=False))
            return 0
        elif args.command == "reauthorize":
            ledger.reauthorize_intent(
                conn, args.call_ref,
                amount_est=args.amount_est, usage_basis=args.usage_basis,
            )
            print(json.dumps({"status": "success", "data": {"status": "intent", "call_ref": args.call_ref}}, ensure_ascii=False))
            return 0
        elif args.command == "claim":
            ledger.claim_request(conn, args.call_ref)
            print(json.dumps({"status": "success", "data": {"status": "claimed", "call_ref": args.call_ref}}, ensure_ascii=False))
            return 0
        elif args.command == "intent":
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
            print(json.dumps({"status": "success", "data": {"status": "intent", "call_ref": args.call_ref}}, ensure_ascii=False))
            return 0
        elif args.command == "reconcile":
            # unknown → success 核销；可顺带回填真实 usage
            ledger.reconcile_success(
                conn, args.call_ref, amount_est=args.amount_est, usage_basis=args.usage_basis,
            )
            row = ledger.get_by_ref(conn, args.call_ref)
            print(json.dumps({"status": "success", "data": {
                "status": row["status"], "call_ref": args.call_ref,
                "usage_basis": row["usage_basis"],
            }}, ensure_ascii=False))
            return 0
        elif args.command == "totals":
            success = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(amount_est), 0) AS total "
                "FROM ledger WHERE status = 'success'"
            ).fetchone()
            # 在途/未对账预留也计入单轮额度：unknown 与 intent 不允许被 success 口径漏算。
            open_rows = conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(amount_est), 0) AS total "
                "FROM ledger WHERE status IN ('intent','unknown')"
            ).fetchone()
            by_status = {
                row["status"]: row["calls"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS calls FROM ledger GROUP BY status"
                ).fetchall()
            }
            print(json.dumps({"status": "success", "data": {
                "success_calls": success["calls"],
                "success_total_usd": success["total"],
                "open_calls": open_rows["calls"],
                "open_total_usd": open_rows["total"],
                "by_status": by_status,
            }}, ensure_ascii=False))
            return 0
        elif args.command == "get":
            row = ledger.get_by_ref(conn, args.call_ref)
            print(json.dumps({"status": "success", "data": {
                "status": row["status"] if row else "missing",
                "call_ref": args.call_ref,
                "amount_est": row["amount_est"] if row else None,
                "request_started": row["request_started"] if row else None,
                "usage_basis": row["usage_basis"] if row else None,
            }}, ensure_ascii=False))
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
            print(json.dumps({"status": "success", "data": {
                "status": row["status"] if row else "missing",
                "call_ref": args.call_ref,
                "usage_basis": row["usage_basis"] if row else None,
            }}, ensure_ascii=False))
            return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
