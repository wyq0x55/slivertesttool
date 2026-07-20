"""Entrypoint for the real-time collaboration server (separate process).

Waitress (the WSGI server that runs the Flask app) cannot perform WebSocket
upgrades, so collaboration runs as its own ASGI process:

    python run_collab.py                 # dev: uvicorn on 0.0.0.0:1234
    COLLAB_HOST=127.0.0.1 COLLAB_PORT=8890 python run_collab.py

It reuses the SAME Flask app factory (and therefore the same database, models
and ``SECRET_KEY``) so materialization can call the existing service layer and
token verification shares the web app's secret.

Requires ``pycrdt`` and ``pycrdt-websocket`` (only this process needs them).

IMPORTANT — single worker only:
    WebSocket rooms are maintained as in-process Python objects (the Y.Doc
    lives in memory). Running uvicorn with ``workers > 1`` would give each
    OS process its own independent copy of every room, so clients on different
    workers would see diverging documents. Always use ``workers=1``.
    For horizontal scaling, a Redis pub/sub relay is needed — out of scope here.
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
        # FIX: explicitly one worker — multiple workers split in-memory Y.Doc
        # state across OS processes, causing document divergence.
        workers=1,
        log_level=os.environ.get("COLLAB_LOG_LEVEL", "info").lower(),
        # Keep WebSocket connections alive through nginx/reverse-proxy idle
        # timeouts.  Without ping/pong, a proxy with a 60 s read timeout will
        # silently drop idle connections, and clients reconnect with a full
        # state-sync burst that looks like a lag spike.
        ws_ping_interval=20,   # send a WS ping every 20 s
        ws_ping_timeout=10,    # close if no pong within 10 s of the ping
    )


if __name__ == "__main__":
    main()
