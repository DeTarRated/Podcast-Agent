"""多用户登录、会话与授权检查 API（PostgreSQL + Redis 登录限流）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from pipeline.provider_gate import ProviderGate, ProviderGateError
from review.domain_auth import require_domain_token
from state.postgres import db
from state.postgres.user import (
    AuthError,
    authenticate,
    can_access,
    create_session,
    revoke_session,
    validate_session,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginInput(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=200)


class TokenInput(BaseModel):
    token: str


class GrantCheckInput(BaseModel):
    user_id: int
    resource_type: str
    resource_id: int
    permission: str = "read"


def _limited(error: str) -> dict:
    return {"status": "failed", "error": {"code": "rate_limited", "message": error, "details": {}}}


@router.post("/login")
def login(payload: LoginInput):
    gate = ProviderGate.from_env()
    try:
        decision = gate.acquire("auth", rate_per_second=0.2, burst=5, max_concurrent=100)
    except ProviderGateError as exc:
        return _limited(str(exc))
    if not decision.allowed:
        return _limited(f"登录过于频繁，请等待 {decision.retry_after_ms}ms 后重试")
    conn = db.connect()
    try:
        user = authenticate(conn, email=payload.email, password=payload.password)
        if user is None:
            return {"status": "failed", "error": {"code": "unauthorized", "message": "邮箱或密码错误", "details": {}}}
        session = create_session(conn, user_id=user["id"])
        return {"status": "success", "data": session}
    finally:
        conn.close()


@router.post("/session")
def session(payload: TokenInput):
    conn = db.connect()
    try:
        user = validate_session(conn, token=payload.token)
        if user is None:
            return {"status": "failed", "error": {"code": "unauthorized", "message": "会话无效或已过期", "details": {}}}
        return {"status": "success", "data": user}
    finally:
        conn.close()


@router.post("/logout")
def logout(payload: TokenInput):
    conn = db.connect()
    try:
        return {"status": "success", "data": {"revoked": revoke_session(conn, token=payload.token)}}
    finally:
        conn.close()


@router.post("/grant-check")
def grant_check(payload: GrantCheckInput, request: Request):
    require_domain_token(request)
    conn = db.connect()
    try:
        allowed = can_access(
            conn,
            user_id=payload.user_id,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            permission=payload.permission,
        )
        return {"status": "success", "data": {"allowed": allowed}}
    finally:
        conn.close()
