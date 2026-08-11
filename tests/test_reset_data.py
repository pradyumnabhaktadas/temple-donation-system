"""reset_data.py -- the go-live wipe.

Run once, on the real database, immediately before going live, by someone
who then imports the temple's actual history on top. It is irreversible,
and anything it misses becomes permanent test data in a live system.

The Camp table was missed: it was added to the app but never to the delete
list, so a reset left test camps in place and didn't even mention them in
the summary printed before asking for confirmation. The registry check
below exists so the next model added can't repeat that.
"""
import datetime
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _populate(db):
    """One row in every table a live system has."""
    from models import (AdminActivityLog, AdminUser, BaceProperty, Camp, Campaign,
                        Donation, Donor, DonorLoginOTP, Festival, LiveToGivePurpose,
                        Preacher, ReceiptCounter, SevaType)
    campaign = Campaign.query.filter_by(name="Annadan").first() or Campaign(
        name="Annadan", is_80g=True)
    db.session.add(campaign)
    db.session.add_all([
        BaceProperty(name="Test BACE"), Festival(name="Test Festival"),
        SevaType(name="Test Seva"), LiveToGivePurpose(name="Test Purpose"),
        Preacher(name="Test Preacher"), Camp(name="Test Camp"),
    ])
    db.session.flush()
    donor = Donor(full_name="Test Donor", phone="9876543210")
    db.session.add(donor)
    db.session.flush()
    db.session.add(Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=100, payment_mode="cash",
        status="success", receipt_number="032511/ISK500001", camp_name="Test Camp"))
    # The real counter row, already advanced -- this is what a system
    # full of test donations looks like just before go-live.
    db.session.add(ReceiptCounter(
        financial_year=ReceiptCounter._FY_KEY, series=ReceiptCounter._SERIES_KEY,
        last_number=500123))
    db.session.add(AdminActivityLog(admin_username="admin", action="test"))
    db.session.add(DonorLoginOTP(phone="9876543210", otp_hash="x",
                                 expires_at=datetime.datetime.utcnow()))
    db.session.commit()


def _run_reset(app, argv=("reset_data.py", "--yes"), typed="DELETE ALL DATA"):
    import reset_data
    with patch.object(sys, "argv", list(argv)), \
         patch("reset_data.create_app", return_value=app), \
         patch("builtins.input", return_value=typed):
        try:
            reset_data.main()
            return 0
        except SystemExit as exc:
            return exc.code


class TestNothingIsMissedFromTheWipe:
    def test_every_model_is_deleted_or_deliberately_excluded(self, app):
        """The delete list is maintained by hand; this is what checks it.

        Compares it against SQLAlchemy's own model registry, so a model
        added to the app and forgotten here fails the build instead of
        quietly surviving a go-live wipe."""
        import reset_data
        from extensions import db

        registered = {
            m.class_ for m in db.Model.registry.mappers
        }
        covered = set(reset_data.MODELS_IN_DELETE_ORDER)
        missing = {m.__name__ for m in registered - covered}
        assert not missing, (
            f"model(s) not wiped by reset_data.py: {sorted(missing)} -- add them to "
            "MODELS_IN_DELETE_ORDER, or this data survives a go-live reset"
        )

    def test_reset_empties_every_table(self, app):
        from extensions import db
        import reset_data
        with app.app_context():
            _populate(db)
            assert _run_reset(app) in (0, None)
            remaining = {
                m.__name__: m.query.count()
                for m in reset_data.MODELS_IN_DELETE_ORDER if m.query.count()
            }
            assert not remaining, f"survived the reset: {remaining}"

    def test_camps_are_wiped(self, app):
        """Regression: the Camp table used to survive."""
        from extensions import db
        from models import Camp
        with app.app_context():
            _populate(db)
            _run_reset(app)
            assert Camp.query.count() == 0

    def test_summary_lists_every_table_before_confirming(self, app, capsys):
        """The operator decides based on this summary, so a table missing
        from it is worse than a table missing from the delete -- it means
        they consented to something other than what happened."""
        from extensions import db
        import reset_data
        with app.app_context():
            _populate(db)
            _run_reset(app)
            printed = capsys.readouterr().out
            for model in reset_data.MODELS_IN_DELETE_ORDER:
                assert model.__tablename__ in printed, \
                    f"{model.__tablename__} missing from the pre-confirmation summary"


class TestRefusals:
    def test_refuses_without_the_yes_flag(self, app):
        from extensions import db
        from models import Donation
        with app.app_context():
            _populate(db)
            assert _run_reset(app, argv=("reset_data.py",)) == 1
            assert Donation.query.count() == 1, "data deleted without --yes"

    def test_refuses_on_a_wrong_confirmation_phrase(self, app):
        from extensions import db
        from models import Donation
        with app.app_context():
            _populate(db)
            assert _run_reset(app, typed="yes") == 1
            assert Donation.query.count() == 1, "data deleted without confirmation"

    def test_empty_database_is_a_no_op(self, app):
        with app.app_context():
            assert _run_reset(app) in (0, None)


class TestSchemaSurvives:
    def test_tables_and_migration_state_are_untouched(self, app):
        """It deletes rows, not schema -- so migrations must not need
        re-running afterwards."""
        from extensions import db
        with app.app_context():
            _populate(db)
            before = set(db.inspect(db.engine).get_table_names())
            _run_reset(app)
            after = set(db.inspect(db.engine).get_table_names())
            assert before == after, f"reset changed the schema: {before ^ after}"


class TestGoLiveSequence:
    """reset -> seed -> the app still works. The actual go-live steps."""

    def _seed(self, app):
        """seed.py runs at import time against its own app, so this does
        what it does, against the test app."""
        from extensions import db
        from models import AdminUser, Campaign
        db.session.add_all([
            Campaign(name="Annadan", is_80g=True),
            Campaign(name="BACE Contribution", is_80g=False),
        ])
        admin = AdminUser(username="admin", role="admin", must_change_password=True)
        admin.set_password("ChangeMe123!")
        db.session.add(admin)
        db.session.commit()

    def test_admin_can_log_in_again_after_reset_and_seed(self, app, client):
        """The reset deletes the admin login too -- without seeding, nobody
        can get back in."""
        from extensions import db
        with app.app_context():
            _populate(db)
            _run_reset(app)
            self._seed(app)
        resp = client.post("/admin/login",
                           data={"username": "admin", "password": "ChangeMe123!"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert b"Invalid" not in resp.data

    def test_public_donation_page_works_after_reset_and_seed(self, app, client):
        from extensions import db
        with app.app_context():
            _populate(db)
            _run_reset(app)
            self._seed(app)
        assert client.get("/").status_code == 200

    def test_receipt_numbering_restarts_cleanly(self, app, client):
        """The counters are wiped, so the first live receipt starts the
        series again rather than continuing from test data."""
        from extensions import db
        from models import Campaign, Donation
        with app.app_context():
            _populate(db)
            _run_reset(app)
            self._seed(app)
            campaign_id = Campaign.query.filter_by(name="Annadan").first().id

        # The seeded admin is forced to change its password before it can
        # do anything -- see test_seeded_admin_must_change_password_first.
        # That's a real go-live step, not an incidental detail.
        login(client, username="admin", password="ChangeMe123!")
        client.post("/admin/change-password", data={
            "current_password": "ChangeMe123!",
            "new_password": "TempleLive2026!",
            "confirm_password": "TempleLive2026!",
        }, follow_redirects=True)

        client.post("/admin/donations/manual", data={
            "campaign_id": campaign_id, "full_name": "First Live Donor",
            "phone": "9811111111", "amount": "1100", "payment_mode": "cash",
            "donation_date": "2026-08-01",
        }, follow_redirects=True)

        with app.app_context():
            from models import ReceiptCounter
            donations = Donation.query.all()
            assert len(donations) == 1, "the reset left donations behind"
            assert donations[0].receipt_number, "no receipt issued after reset"

            # The pre-reset counter was at 500123. If the wipe had missed
            # the counters, this first live donation would be 500124 and
            # test data's numbering would carry into the real records.
            # A clean reset puts it back at the start of the series.
            counter = ReceiptCounter.query.one()
            assert counter.last_number < 500123, (
                f"numbering continued from test data (counter at "
                f"{counter.last_number}, receipt {donations[0].receipt_number})"
            )
            assert "500000" in donations[0].receipt_number, (
                f"first live receipt wasn't the start of the series: "
                f"{donations[0].receipt_number}"
            )

    def test_no_donor_or_donation_history_remains(self, app, client):
        from extensions import db
        from models import Donation, Donor
        with app.app_context():
            _populate(db)
            _run_reset(app)
            self._seed(app)
            assert Donor.query.count() == 0
            assert Donation.query.count() == 0

    def test_seeded_admin_must_change_password_first(self, app, client):
        """The seed's default password is published in the README, so the
        account is locked to changing it until it's replaced. Anyone
        following the go-live steps needs to know this comes before they
        can record anything."""
        from extensions import db
        with app.app_context():
            _populate(db)
            _run_reset(app)
            self._seed(app)

        resp = client.post("/admin/login",
                           data={"username": "admin", "password": "ChangeMe123!"},
                           follow_redirects=False)
        assert "/admin/change-password" in resp.headers.get("Location", "")

        # And it stays locked until the password is actually changed.
        resp = client.get("/admin/dashboard", follow_redirects=False)
        assert "/admin/change-password" in resp.headers.get("Location", "")

        client.post("/admin/change-password", data={
            "current_password": "ChangeMe123!",
            "new_password": "TempleLive2026!",
            "confirm_password": "TempleLive2026!",
        }, follow_redirects=True)
        assert client.get("/admin/dashboard").status_code == 200
