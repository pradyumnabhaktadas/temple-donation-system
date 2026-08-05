import os
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

from config import Config
from extensions import db, login_manager, csrf, limiter, LIMITER_AVAILABLE
from models import AdminUser
from utils import format_inr


def create_app(test_config=None):
    """test_config lets the test suite (see tests/conftest.py) point the app
    at an in-memory database and disable CSRF before db.init_app() /
    db.create_all() run, instead of only being able to override config
    after the real database file has already been created."""
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    app.config["RAZORPAY_ENABLED"] = bool(
        app.config.get("RAZORPAY_KEY_ID") and app.config.get("RAZORPAY_KEY_SECRET")
    )

    if app.config.get("IS_PRODUCTION"):
        # Render (and most PaaS hosts) puts a reverse proxy in front of the
        # app -- without this, request.remote_addr is the proxy's own IP
        # for every single visitor, which would make IP-based rate limiting
        # below apply to everyone as one shared bucket instead of per
        # visitor, and could also confuse the Secure-cookie/HTTPS detection
        # used by SESSION_COOKIE_SECURE. x_for=1/x_proto=1 trusts exactly
        # one hop, matching a typical single-reverse-proxy deployment.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    if not LIMITER_AVAILABLE:
        app.logger.warning(
            "Flask-Limiter isn't installed -- the public donation API has no "
            "rate limiting. Run `pip install -r requirements.txt` to enable it."
        )

    # Security headers (HSTS, clickjacking/X-Frame-Options, MIME-sniffing/
    # X-Content-Type-Options, Referrer-Policy, Content-Security-Policy) --
    # only in production, since force_https would redirect-loop local
    # http://localhost dev. The CSP allow-list below is deliberately an
    # exact match for every external resource this app's templates actually
    # load (checked via grep, not guessed) -- Bootstrap/Chart.js from
    # jsdelivr, Google Fonts, and Razorpay's checkout script/iframe/API.
    # Guarded the same way as Sentry/Flask-Migrate above.
    if app.config.get("IS_PRODUCTION"):
        try:
            from flask_talisman import Talisman

            csp = {
                "default-src": "'self'",
                # 'unsafe-inline' is needed for the inline <script> blocks
                # (donate.html's fetch()-based checkout flow) and the many
                # inline style="..." attributes across the admin templates --
                # a full nonce-based rewrite is a bigger job than this pass.
                "script-src": [
                    "'self'", "'unsafe-inline'",
                    "https://cdn.jsdelivr.net", "https://checkout.razorpay.com",
                ],
                "style-src": [
                    "'self'", "'unsafe-inline'",
                    "https://cdn.jsdelivr.net", "https://fonts.googleapis.com",
                ],
                "font-src": ["'self'", "https://fonts.gstatic.com", "https://cdn.jsdelivr.net", "data:"],
                "img-src": ["'self'", "data:"],
                # Razorpay's checkout.js makes its own XHR/telemetry calls.
                "connect-src": ["'self'", "https://api.razorpay.com", "https://lumberjack.razorpay.com"],
                # The actual payment popup/iframe.
                "frame-src": ["https://api.razorpay.com", "https://checkout.razorpay.com"],
            }
            Talisman(
                app,
                force_https=True,
                strict_transport_security=True,
                frame_options="SAMEORIGIN",
                referrer_policy="strict-origin-when-cross-origin",
                content_security_policy=csp if app.config.get("CONTENT_SECURITY_POLICY_ENABLED") else None,
                # CSP report-only would be safer to roll out blind, but
                # Talisman applies it directly -- test the actual donation
                # form + Razorpay checkout after your first deploy with this
                # enabled (see README "Security headers"), and flip
                # CONTENT_SECURITY_POLICY_ENABLED=false if anything's blocked.
            )
        except ImportError:
            app.logger.warning(
                "Flask-Talisman isn't installed -- security headers "
                "(CSP/HSTS/etc.) are off. Run `pip install -r requirements.txt` "
                "to enable them."
            )

    # Error monitoring -- no-op unless SENTRY_DSN is set (see README "Error
    # monitoring"). Guarded so a missing sentry-sdk package degrades to
    # "monitoring off" with a log line instead of crashing the app.
    if app.config.get("SENTRY_DSN"):
        try:
            import sentry_sdk
            from sentry_sdk.integrations.flask import FlaskIntegration

            sentry_sdk.init(
                dsn=app.config["SENTRY_DSN"],
                integrations=[FlaskIntegration()],
                traces_sample_rate=0.1,
                environment="production" if app.config.get("IS_PRODUCTION") else "development",
            )
        except ImportError:
            app.logger.warning(
                "SENTRY_DSN is set but sentry-sdk isn't installed -- "
                "run `pip install -r requirements.txt` to enable error monitoring."
            )

    os.makedirs(os.path.join(app.root_path, "instance"), exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Schema migrations (Alembic via Flask-Migrate) -- see README "Database
    # migrations" for the flask db init/migrate/upgrade workflow. Guarded
    # the same way as Sentry above so this dev environment (which doesn't
    # have Flask-Migrate installed) still runs; a real deployment that's
    # run `pip install -r requirements.txt` will always have it.
    try:
        from flask_migrate import Migrate

        Migrate(app, db)
    except ImportError:
        pass

    # Indian-style digit grouping (12,34,567 instead of 1,234,567) for
    # rupee amounts everywhere in the templates. Usage: {{ amount | inr }}
    app.jinja_env.filters["inr"] = format_inr

    @login_manager.user_loader
    def load_user(user_id):
        return AdminUser.query.get(int(user_id))

    @app.context_processor
    def inject_org():
        # Makes org_name available in every template (e.g. the shared
        # header in base.html) without each view needing to pass it.
        return {"org_name": app.config["ORG_NAME"]}

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        # The donation form and admin forms both submit here. JSON API
        # callers (the fetch() calls on the donation page) need a JSON
        # error back, not Flask-WTF's default HTML error page.
        if request.path.startswith("/api/"):
            return jsonify({"error": "Security check failed. Please refresh the page and try again."}), 400
        return render_template("csrf_error.html", reason=e.description), 400

    from public import bp as public_bp
    from admin import bp as admin_bp
    from donor_portal import bp as donor_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(donor_bp)

    with app.app_context():
        db.create_all()

    return app


app = create_app()

if __name__ == "__main__":
    # macOS often has "AirPlay Receiver" listening on port 5000 by default,
    # which causes "Address already in use". Override with: PORT=5001 python app.py
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
