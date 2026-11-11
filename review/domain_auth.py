"""Fastify 与内部领域服务共享的 RPC 令牌校验。"""
from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request


def require_domain_token(request: Request) -> None:
    expected = os.environ.get("PODCAST_DOMAIN_TOKEN", "")
    token = request.headers.get("X-Domain-Token")
    if not expected or token is None or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "内部领域令牌无效"},
        )
