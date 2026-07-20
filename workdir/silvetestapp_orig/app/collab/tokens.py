"""Short-lived signed access tokens for the collaboration WebSocket.

The Flask web app mints a token after checking the user's session and project
permission; the (separate) collab server verifies it on connect. Both sides
share ``Config.SECRET_KEY`` so no extra key management is required.

The token is an ``itsdangerous`` URL-safe, timestamped, signed blob carrying the
authenticated identity and the room the client is allowed to join. It is *not*
encrypted (the payload is readable) — it only proves the web app authorised this
user for this project, and it expires quickly.
"""

from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Namespaces the signature so these tokens can never be confused with Flask's
# session cookie (which uses the same SECRET_KEY but a different salt).
_SALT = "lm-collab-ws-v1"

# Default lifetime: long enough to open the socket right after fetching the
# token, short enough that a leaked token is near-useless.
DEFAULT_MAX_AGE = 120  # seconds


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=_SALT)


def mint(secret_key: str, *, user_id: int, username: str, project_id: int,
         role: str) -> str:
    """Sign a token granting ``user_id`` access to ``project:{project_id}``."""
    payload: dict[str, Any] = {
        "uid": int(user_id),
        "un": username,
        "pid": int(project_id),
        "role": role,
        "room": f"project:{int(project_id)}",
    }
    return _serializer(secret_key).dumps(payload)


def verify(secret_key: str, token: str, *,
           max_age: int = DEFAULT_MAX_AGE) -> dict[str, Any] | None:
    """Return the token payload if valid & unexpired, else ``None``."""
    if not token:
        return None
    try:
        return _serializer(secret_key).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
