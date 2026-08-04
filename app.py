import os
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFError

load_dotenv()

from config import Config
from extensions import db, login_manager, csrf
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
