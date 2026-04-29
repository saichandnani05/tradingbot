"""WSGI entry point for Vercel / Gunicorn / any WSGI server.

Adds the trading_bot package directory to sys.path so all imports resolve,
then imports the Flask app.  Works both locally and on serverless platforms.
"""
import os
import sys

# ── Resolve paths ─────────────────────────────────────────────────────────────
_root    = os.path.dirname(os.path.abspath(__file__))
_bot_dir = os.path.join(_root, "trading_bot")

# Put trading_bot/ on the path so its local imports work
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)

# ── Import the Flask application ──────────────────────────────────────────────
from app import app   # noqa: E402  (trading_bot/app.py)

# Vercel / gunicorn look for either `app` or `application`
application = app

# ── Local dev convenience ─────────────────────────────────────────────────────
if __name__ == "__main__":
    from app import start_background, CONFIG
    start_background()
    app.run(host=CONFIG.flask_host, port=CONFIG.flask_port,
            debug=False, use_reloader=False)
