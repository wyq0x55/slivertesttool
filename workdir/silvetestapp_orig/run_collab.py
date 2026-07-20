"""Entrypoint for the real-time collaboration server (separate process).

Waitress (the WSGI server that runs the Flask app) cannot perform WebSocket
upgrades, so collaboration runs as its own ASGI process:

    python run_collab.py                 # dev: uvicorn on 0.0.0.0:1234
    COLLAB_HOST=127.0.0.1 COLLAB_PORT=8890 python run_collab.py

It reuses the SAME Flask app factory (and therefore the same database, models
and ``SECRET_KEY``) so materialization can call the existing service layer and
token verification shares the web app's secret.

Requires ``pycrdt`` and ``pycrdt-websocket`` (only this process needs them).
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
    uvicorn.run(asgi_app, host=host, port=port,
                log_level=os.environ.get("COLLAB_LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
