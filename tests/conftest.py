import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app import create_app
from extensions import db
from models import Campaign, AdminUser


@pytest.fixture
def app(tmp_path):
    """A fresh app + in-memory database for every test, so tests can't
    leak state into each other or touch your real instance/temple.db."""
    test_app = create_app(
        test_config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test-secret",

            # Pin every external-integration setting to "not configured".
            #
            # app.py calls load_dotenv() at import time and Config reads
            # os.environ, so without this the suite inherits whatever is in
            # the developer's own .env -- and then passes or fails
            # depending on which integrations that developer happens to
            # have set up locally. That's exactly what bit us: adding real
            # Airtel credentials to .env made the WhatsApp "demo mode when
            # not configured" test start failing, because the send really
            # was configured, while the Razorpay amount test depended on
            # Razorpay keys being present.
            #
            # Tests that need an integration switched on now say so
            # explicitly (see test_razorpay_order_amount and
            # test_whatsapp_receipt's _configure), which is both clearer
            # and reproducible on any machine.
            "RAZORPAY_KEY_ID": "",
            "RAZORPAY_KEY_SECRET": "",
            "RAZORPAY_WEBHOOK_SECRET": "",
            "WHATSAPP_AIRTEL_USERNAME": "",
            "WHATSAPP_AIRTEL_PASSWORD": "",
            "WHATSAPP_FROM_NUMBER": "",
            "WHATSAPP_TEMPLATE_ID": "",
            "WHATSAPP_REPORT_TEMPLATE_ID": "",
            "PUBLIC_BASE_URL": "",
            "SMTP_HOST": "",
            "INTERNAL_TASK_TOKEN": "",
            "ZOHO_FORMS_WEBHOOK_TOKEN": "",

            # Backups go to a temp directory, not instance/backups. Tests
            # that exercise the restore route trigger a real safety backup,
            # and without this they leave actual ZIP files behind in the
            # developer's working copy every run.
            "BACKUP_DIR": str(tmp_path / "backups"),
        }
    )

    with test_app.app_context():
        db.drop_all()
        db.create_all()

        db.session.add_all([
            Campaign(name="Annadan", is_80g=True),
            Campaign(name="Temple Construction", is_80g=True, target_amount=1000000),
            Campaign(name="BACE Contribution", is_80g=False),
        ])

        admin = AdminUser(username="testadmin", role="admin")
        admin.set_password("TestPass123!")
        db.session.add(admin)

        staff = AdminUser(username="teststaff", role="staff")
        staff.set_password("TestPass123!")
        db.session.add(staff)

        db.session.commit()

        yield test_app

        db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="testadmin", password="TestPass123!"):
    return client.post("/admin/login", data={"username": username, "password": password}, follow_redirects=True)
