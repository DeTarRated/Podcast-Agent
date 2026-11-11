"""审核层·长驻 Python 领域 RPC 服务（FastAPI）。

仅供 Fastify 经 127.0.0.1／内部网络调用；不直接暴露公网。
复用 review.service 与 delivery.commands，不新增另一套状态机。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from delivery.errors import DeliveryError
from delivery.commands import dispatch as delivery_dispatch
from pipeline.scribe import _load_dotenv
from review.cli import dispatch as review_dispatch
from review.domain_auth import require_domain_token
from review.service import ReviewError
from state import db
from state.postgres import migrations as postgres_migrations

REVIEW_COMMANDS = {
    "usage-report", "inbox", "detail", "media-info", "create-revision",
    "save-override", "select-variant", "hold", "resume", "approve", "reject",
    "revise-published", "auto-approve-due",
}
DELIVERY_COMMANDS = {"publish-approved", "mark-highlights-failed", "library", "detail"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_dotenv()
    if not os.environ.get("PODCAST_DOMAIN_TOKEN"):
        raise RuntimeError("缺少 PODCAST_DOMAIN_TOKEN；内部领域服务拒绝无鉴权启动")
    # 启动时执行一次迁移；请求路径只连接，不重复跑 schema 初始化。
    conn = db.open_default()
    conn.close()
    pg_conn = postgres_migrations.connect()
    try:
        postgres_migrations.migrate(pg_conn)
    finally:
        pg_conn.close()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

from review.auth_api import router as auth_router

app.include_router(auth_router)


def _auth(request) -> None:
    require_domain_token(request)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/rpc/review/{command}")
def review_rpc(command: str, payload: dict, request: Request) -> dict:
    _auth(request)
    if command not in REVIEW_COMMANDS:
        return _failed("not_found", "未知 review command", {})
    conn = db.connect(db.default_db_path())
    try:
        data = review_dispatch(conn, command, payload, _now())
        if command in {"save-override", "select-variant"} and data.get("has_conflicts"):
            return _failed("override_conflict", "修改已保存，但存在映射冲突", data)
        return {"status": "success", "data": data}
    finally:
        conn.close()


@app.post("/rpc/delivery/{command}")
def delivery_rpc(command: str, payload: dict, request: Request) -> dict:
    _auth(request)
    if command not in DELIVERY_COMMANDS:
        return _failed("not_found", "未知 delivery command", {})
    conn = db.connect(db.default_db_path())
    try:
        data = delivery_dispatch(conn, command, payload, now=_now())
        return {"status": "success", "data": data}
    finally:
        conn.close()


def _failed(code: str, message: str, details: dict) -> dict:
    return {"status": "failed", "error": {"code": code, "message": message, "details": details}}


@app.exception_handler(ReviewError)
async def review_error(_request, exc: ReviewError):
    return JSONResponse(_failed(exc.code, exc.message, exc.details))


@app.exception_handler(DeliveryError)
async def delivery_error(_request, exc: DeliveryError):
    return JSONResponse(_failed(exc.code, exc.message, exc.details))


async def validation_error(_request, exc):
    return JSONResponse(_failed("validation_error", str(exc), {}))


for exception_type in (KeyError, TypeError, ValueError):
    app.exception_handler(exception_type)(validation_error)
