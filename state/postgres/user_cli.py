"""多用户账号、授权、会话与预算管理 CLI；密码只从指定环境变量读取。"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal

from state.postgres import migrations
from state.postgres.db import connect
from state.postgres.user import (
    audit,
    authenticate,
    can_access,
    create_session,
    create_user,
    grant_permission,
    set_budget,
    validate_session,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="多用户账号与授权管理")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user")
    create.add_argument("--email", required=True)
    create.add_argument("--role", default="viewer")
    grant = sub.add_parser("grant")
    grant.add_argument("--user-id", type=int, required=True)
    grant.add_argument("--resource-type", required=True)
    grant.add_argument("--resource-id", type=int, required=True)
    grant.add_argument("--permission", default="read")
    session = sub.add_parser("create-session")
    session.add_argument("--user-id", type=int, required=True)
    validate = sub.add_parser("validate-session")
    validate.add_argument("--token", required=True)
    budget = sub.add_parser("set-budget")
    budget.add_argument("--scope", required=True)
    budget.add_argument("--period", required=True)
    budget.add_argument("--user-id", type=int)
    budget.add_argument("--episode-id", type=int)
    budget.add_argument("--max-amount", type=Decimal)
    budget.add_argument("--max-running", type=int)
    parser.add_argument("--dsn")
    args = parser.parse_args(argv)
    conn = connect(args.dsn)
    try:
        migrations.migrate(conn)
        if args.command == "create-user":
            password = os.environ.get("PODCAST_NEW_USER_PASSWORD", "")
            result = create_user(conn, email=args.email, password=password, role=args.role)
        elif args.command == "grant":
            result = grant_permission(
                conn,
                user_id=args.user_id,
                resource_type=args.resource_type,
                resource_id=args.resource_id,
                permission=args.permission,
            )
        elif args.command == "create-session":
            result = create_session(conn, user_id=args.user_id)
        elif args.command == "validate-session":
            result = validate_session(conn, token=args.token)
        else:
            result = set_budget(
                conn,
                scope=args.scope,
                period=args.period,
                user_id=args.user_id,
                episode_id=args.episode_id,
                max_amount=args.max_amount,
                max_running=args.max_running,
            )
        if result and "token" in result:
            result["token_hash_note"] = "原始 token 仅此返回一次，数据库只存 hash"
        print(json.dumps({"status": "success", "data": result}, ensure_ascii=False, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
