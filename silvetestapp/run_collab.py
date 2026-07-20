"""Entrypoint for the real-time collaboration server (separate process).

Waitress (the WSGI server that runs the Flask app) cannot perform WebSocket
upgrades, so collaboration runs as its own ASGI process:

    python run_collab.py                 # dev: uvicorn on 0.0.0.0:1234
    COLLAB_HOST=127.0.0.1 COLLAB_PORT=8890 python run_collab.py

It reuses the SAME Flask app factory (and therefore the same database, models
and ``SECRET_KEY``) so materialization can call the existing service layer and
token verification shares the web app's secret.

Requires ``pycrdt`` and ``pycrdt-websocket`` (only this process needs them).

重要 — 必须单 worker：
    WebSocket 房间作为 Python 进程内对象维护（Y.Doc 在内存中）。
    workers > 1 时每个 OS 进程有自己独立的房间副本，不同 worker 上的
    客户端会看到分叉的文档。始终保持 workers=1。
    水平扩展需要 Redis pub/sub 中继，超出当前范围。
"""

from __future__ import annotations

import logging
import os

logging.basicConfig(level=os.environ.get("COLLAB_LOG_LEVEL", "INFO"))
_log = logging.getLogger("collab")


def main() -> None:
    import uvicorn

    from app import create_app
    from app.config import Config
    from app.extensions import db
    from app.collab.server import build_asgi_app

    flask_app = create_app(Config)

    # Additively create the collaboration table if a migration hasn't yet; this
    # never drops or alters existing tables.
    with flask_app.app_context():
        from app.models import CollabDoc  # noqa: F401
        db.create_all()

    asgi_app = build_asgi_app(flask_app)

    host = os.environ.get("COLLAB_HOST", "0.0.0.0")
    port = int(os.environ.get("COLLAB_PORT", "1234"))
    _log.info("starting collab server on ws://%s:%d/project:{id}", host, port)
    uvicorn.run(
        asgi_app,
        host=host,
        port=port,
        # FIX: 明确单 worker，多 worker 会分裂内存中的 Y.Doc 状态。
        workers=1,
        log_level=os.environ.get("COLLAB_LOG_LEVEL", "info").lower(),
        # FIX: ping/pong 保活，防止 nginx 等反代因空闲超时切断 WebSocket
        # 连接。断线后客户端重连触发全量 state-sync，表现为明显卡顿。
        ws_ping_interval=20,   # 每 20 s 发一次 WS ping
        ws_ping_timeout=10,    # 10 s 内无 pong 则关闭连接
    )


if __name__ == "__main__":
    main()
