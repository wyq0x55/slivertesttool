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


class RequestParamError(ServiceError):
    """A malformed query-string parameter -- the caller's fault, not ours.

    Deliberately a :class:`ServiceError` subclass so the handler already
    registered by ``register_common`` maps it to a 400 in the standard
    envelope. The alternative -- registering a blanket ``ValueError`` handler --
    was rejected: it would dress *genuine internal bugs* up as client errors and
    hide them behind a tidy 400, which is worse than the 500 it replaces.
    """

    def __init__(self, message: str, *, param: str | None = None):
        super().__init__(message, code="VALIDATION_ERROR",
                         details={"param": param} if param else None)
        self.param = param


class VersionConflict(ServiceError):
    def __init__(self, client_version: int, server_version: int, server_data: dict):
        super().__init__("该记录已被其他用户修改", code="VERSION_CONFLICT", details={
            "client_version": client_version,
            "server_version": server_version,
            "server_data": server_data,
        })
