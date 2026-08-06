"""Short-lived signed access tokens for the collaboration WebSocket.

The Flask web app mints a token after checking the user's session and project
permission; the (separate) collab server verifies it on connect. Both sides
share ``Config.SECRET_KEY`` so no extra key management is required.

The token is an ``itsdangerous`` URL-safe, timestamped, signed blob carrying the
authenticated identity and the room the client is allowed to join. It is *not*
encrypted (the payload is readable) — it only proves the web app authorised this
user for this project, and it expires quickly.

Token 有效期说明：
    WebSocket 会话与浏览器标签页共存亡，可能持续数小时。
    y-websocket 在网络抖动后自动重连，且每次重连沿用最初拿到的 token。
    如果 token 此时已过期，collab 服务器会拒绝重连，该标签页永久降级
    为 REST 轮询模式（无感知，只是不再实时协同）。

    DEFAULT_MAX_AGE 设为 2 小时（7200 s）。在局域网场景下这是安全的：
    token 仅在 WebSocket 握手请求中传输，由 SECRET_KEY 签名验证，即使
    被截获也只能访问单个项目房间，且在 2 小时内过期。

    静默续签已在前端实现（collab.js `_scheduleTokenRenewal`/`_renewToken`）：
    在过期前 ~45 s 自动重新调用 POST /collab-token 拿新 token，并写回
    `provider.params.token`，使后续每次（重）连都携带有效 token。续签会在
    到期前接管，因此把 TTL 从 8 小时收敛到 2 小时不会影响长会话，却显著
    缩短了 token 一旦泄露的可用时长。
"""

from __future__ import annotations

from typing import Any

from itsdangerous import BadData, URLSafeTimedSerializer

# Namespaces the signature so these tokens can never be confused with Flask's
# session cookie (which uses the same SECRET_KEY but a different salt).
_SALT = "lm-collab-ws-v1"

# TTL 收敛：前端已实现静默续签（collab.js `_scheduleTokenRenewal`），会在
# 到期前 ~45 s 自动换新 token，因此不再需要 8 小时的超长有效期来容忍断线重连。
# 收敛到 2 小时：既能覆盖续签定时器偶发失败/系统休眠唤醒的窗口，又把「token
# 被截获后可用时长」显著缩短。隔夜标签页由续签接管，不再依赖 token 本身的长 TTL。
DEFAULT_MAX_AGE = 7200  # 2 小时，单位秒


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
    except BadData:
        # BadData is the base of BadSignature/SignatureExpired AND BadPayload;
        # catching it also rejects truncated/garbage tokens that would otherwise
        # surface as an unhandled 500 on the collab handshake.
        return None
