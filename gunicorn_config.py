"""Gunicorn config for production. Flask's built-in dev server (used by
`python app.py`) is single-threaded and not meant for real traffic --
gunicorn runs multiple worker processes behind it.

Usage:
    gunicorn -c gunicorn_config.py app:app

Or via the Procfile, if your host (Render/Railway/Heroku-style) reads one:
    web: gunicorn -c gunicorn_config.py app:app
"""
import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', 8000)}"

# A common starting formula; tune based on your host's CPU count and traffic.
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))

threads = int(os.environ.get("GUNICORN_THREADS", 2))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 30))

accesslog = "-"   # log to stdout, so your host's log aggregator picks it up
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
