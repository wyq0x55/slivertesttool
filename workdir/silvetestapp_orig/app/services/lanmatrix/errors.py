"""Service-layer exceptions for the LAN Test Matrix.

Shared by the per-domain service modules so they don't import each other just
for the error types. The API blueprint maps these to the unified JSON envelope.
"""
from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Business-rule violation (mapped to a 4xx by the API)."""

    def __init__(self, message: str, *, code: str = "ERROR", details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


class VersionConflict(ServiceError):
    def __init__(self, client_version: int, server_version: int, server_data: dict):
        super().__init__("该记录已被其他用户修改", code="VERSION_CONFLICT", details={
            "client_version": client_version,
            "server_version": server_version,
            "server_data": server_data,
        })
