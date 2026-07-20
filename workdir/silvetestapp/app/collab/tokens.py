"""Short-lived signed access tokens for the collaboration WebSocket.

The Flask web app mints a token after checking the user's session and project
permission; the (separate) collab server verifies it on connect. Both sides
share ``Config.SECRET_KEY`` so no extra key management is required.

The token is an ``itsdangerous`` URL-safe, timestamped, signed blob carrying the
authenticated identity and the room the client is allowed to join. It is *not*
encrypted (the payload is readable) — it only proves the web app authorised this
user for this project, and it expires quickly.

Token lifetime note:
    The WebSocket session lives as long as the browser tab is open — potentially
    many hours. ``y-websocket`` reconnects automatically on network blips, and on
    each reconnect it passes the SAME token it originally received. If that token
    has already expired the collab server rejects the reconnect, permanently
    dropping the session to REST-only mode for that tab.

    DEFAULT_MAX_AGE is therefore set to 8 hours (28800 s). This is safe in a LAN
    context: the token is only ever sent over the WebSocket upgrade request and is
    verified against the server SECRET_KEY. Even if intercepted it only grants
    access to one project room and expires within the working day.

    If you need shorter-lived tokens, implement renewal in collab.js:
      1. Call POST /collab-token again ~30 s before expiry.
      2. Reconnect the WebsocketProvider with the fresh token.
    Until that renewal loop exists, do not shorten DEFAULT_MAX_AGE below 3600 s.
"""

from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Namespaces the signature so these tokens can never be confused with Flask's
# session cookie (which uses the same SECRET_KEY but a different salt).
_SALT = "lm-collab-ws-v1"

# FIX: was 120 s, which is far too short for a WebSocket session.
# y-websocket reconnects on network blips and reuses the same token; a 2-minute
# TTL means any reconnect after the first two minutes is permanently rejected,
# silently degrading the session to REST polling.
# 8 hours covers a full working day; see module docstring for renewal guidance.
DEFAULT_MAX_AGE = 28800  # 8 hours in seconds


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
