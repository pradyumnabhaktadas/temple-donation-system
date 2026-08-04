import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app import create_app
from extensions import db
from models import Campaign, AdminUser


@pytest.fixture
def app():
    """A fresh app + in-memory database for every test, so tests can't
    leak state into each other or touch your real instance/temple.db."""
    test_app = create_app(
        test_config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test-secret",
        }
    )

    with test_app.app_context():
        db.drop_all()
        db.create_all()

        db.session.add_all([
            Campaign(name="Annadan", is_80g=True),
            Campaign(name="Temple Construction", is_80g=True, target_amount=1000000),
            Campaign(name="BACE Rent", is_80g=False),
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
