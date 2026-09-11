"""Gunicorn config for production. Flask's built-in dev server (used by
`python app.py`) is single-threaded and not meant for real traffic --
gunicorn runs multiple worker processes behind it.

Usage:
    gunicorn -c gunicorn_config.py app:app

Or via the Procfile, if your host (Render/Railway/Heroku-style) reads one:
    web: gunicorn -c gunicorn_config.py app:app
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', 8000)}"

# Deliberately NOT using multiprocessing.cpu_count() here. On Render (and
# most container hosts) that reports the underlying host machine's total
# core count, not the sliver actually allocated to this container -- e.g.
# the Starter instance type is 0.5 CPU / 512MB, but cpu_count() on shared
# infra can still report 8, 16, or more. That formula then spawns far more
# worker *processes* than a 512MB container can hold: each one loads the
# full app (SQLAlchemy, ReportLab, PIL, Sentry, ...), which alone can be
# 80-150MB+ RSS. A few workers too many gets silently OOM-killed by the
# host with no application-level error or log line at all -- which looks
# exactly like "the server dropped the connection" from the browser's side,
# and was the root cause of donations getting stuck on Render's Starter
# (512MB) tier.
#
# gthread lets one worker process handle several requests concurrently via
# threads instead of needing a whole extra process per concurrent request.
# One worker x two threads is sufficient for this small temple site and
# leaves extra memory margin on Render's 512 MB instance.  Heavy operations
# such as receipt generation should not multiply their peak memory use.
worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", 1))
threads = int(os.environ.get("GUNICORN_THREADS", 2))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 90))

# Recycle workers periodically -- cheap insurance against slow memory growth
# over a long-running worker's life (PDF/image generation, SMTP sockets).
max_requests = 200
max_requests_jitter = 50

accesslog = "-"   # log to stdout, so your host's log aggregator picks it up
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
