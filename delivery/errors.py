"""交付层·稳定错误码。"""
from __future__ import annotations

from typing import Optional


class DeliveryError(RuntimeError):
    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
