"""Start the web application only (production WSGI server: Waitress).

    python run_web.py

Serves the Bootstrap UI + REST/SSE API on ``HOST:PORT`` (default 0.0.0.0:8080).
This starts the **web process only**; run the worker separately with
``python run_worker.py`` so tasks actually execute.

For the usual single-command setup that launches both the web server and the
worker together, use ``python run.py`` instead.
"""

from __future__ import annotations

from app import __version__, create_app
from app.config import Config

app = create_app()


def main() -> None:
    Config.ensure_dirs()
    try:
        from waitress import serve
    except ImportError:  # pragma: no cover - dev fallback
        print("waitress not installed; falling back to Flask's dev server.")
        app.run(host=Config.HOST, port=Config.PORT, threaded=True)
        return

    print(f"Silver Test Platform v{__version__} -> http://{Config.HOST}:{Config.PORT}")
    # ``threads`` must be generous: each open SSE stream holds one thread.
    serve(app, host=Config.HOST, port=Config.PORT, threads=max(16, Config.HUEY_WORKERS * 2))


if __name__ == "__main__":
    main()
