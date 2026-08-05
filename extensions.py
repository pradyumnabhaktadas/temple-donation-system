from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "admin.login"
csrf = CSRFProtect()

# Rate limiting on the public donation API (create-order/verify-payment/
# simulate-payment) -- guarded the same way as Flask-Migrate/sentry-sdk
# elsewhere in this app, so a fresh checkout that hasn't run `pip install`
# yet still runs, just without throttling, instead of failing to start.
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address, default_limits=[])
    LIMITER_AVAILABLE = True
except ImportError:
    LIMITER_AVAILABLE = False

    class _NoOpLimiter:
        """Stand-in for flask_limiter.Limiter when the package isn't
        installed. Every route decorated with @limiter.limit(...) keeps
        working -- the decorator just becomes a no-op -- rather than the
        app failing to import."""

        def init_app(self, app):
            pass

        def limit(self, *args, **kwargs):
            def decorator(f):
                return f
            return decorator

    limiter = _NoOpLimiter()
