"""REG-059 (QA report, 2026-08-20): CSV/formula injection in admin exports.

Every export writes donor-controlled fields (name, address, remarks, ...)
straight into a CSV that an admin later opens in Excel for a government
filing (Form 10BD) or a reconciliation. A donor whose name begins with =,
+, -, or @ would have that read as a formula by Excel/Sheets, not as text
-- utils.csv_safe_row() neutralizes it by prefixing a leading single quote,
the standard defense (OWASP CSV Injection).

These tests drive the real routes end to end, not the helper in isolation,
so a future export that forgets to wrap its row is caught here rather than
only being provable by reading the code.
"""
import csv
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

PAYLOAD = "=CMD(calc)"


def _cells(csv_text):
    """Every individual cell across the whole CSV, parsed properly --
    a naive substring check on the raw text would wrongly flag the fixed
    version too, since "=CMD(calc)" is itself a substring of the correctly
    neutralized "'=CMD(calc)"."""
    return [cell for row in csv.reader(io.StringIO(csv_text)) for cell in row]


@pytest.fixture
def campaign_id(app):
    from extensions import db
    from models import Campaign
    with app.app_context():
        campaign = Campaign.query.filter_by(name="Annadan").first()
        if campaign is None:
            campaign = Campaign(name="Annadan", is_80g=True)
            db.session.add(campaign)
            db.session.commit()
        return campaign.id


def _log_donation(client, campaign_id, **overrides):
    data = {
        "campaign_id": campaign_id,
        "full_name": PAYLOAD,
        "phone": "9876543210",
        "amount": "1100",
        "payment_mode": "cash",
        "remarks": PAYLOAD,
        "donation_date": "2026-08-01",
    }
    data.update(overrides)
    return client.post("/admin/donations/manual", data=data, follow_redirects=True)


class TestUtilHelper:
    def test_formula_prefixes_are_neutralized(self):
        from utils import csv_safe
        for trigger in ("=", "+", "-", "@", "\t", "\r"):
            assert csv_safe(f"{trigger}evil").startswith("'")

    def test_ordinary_text_is_untouched(self):
        from utils import csv_safe
        assert csv_safe("Ravi Sharma") == "Ravi Sharma"
        assert csv_safe("") == ""
        assert csv_safe(None) is None

    def test_numbers_and_none_pass_through(self):
        from utils import csv_safe_row
        assert csv_safe_row([101.0, None, "ok"]) == [101.0, None, "ok"]

    def test_a_negative_amount_would_still_be_neutralized(self):
        """csv_safe() can't tell "this is a number that happens to be
        negative" from "this is a formula" once it's already a string --
        callers must only wrap donor-controlled text fields, never a raw
        float. Documented here so that boundary isn't crossed by accident
        in a future export."""
        from utils import csv_safe
        assert csv_safe("-500") == "'-500"


class TestExportsNeutralizeInjection:
    """Each of these donor-facing exports must not let a name/remarks
    value starting with = reach the CSV unprefixed."""

    def test_donations_log_export(self, app, client, campaign_id):
        login(client)
        _log_donation(client, campaign_id)
        cells = _cells(client.get("/admin/export/donations?range=all").data.decode())
        assert PAYLOAD not in cells
        assert f"'{PAYLOAD}" in cells

    def test_monthly_report_export(self, app, client, campaign_id):
        login(client)
        _log_donation(client, campaign_id)
        cells = _cells(client.get("/admin/export/monthly").data.decode())
        assert PAYLOAD not in cells
        assert f"'{PAYLOAD}" in cells

    def test_collections_export(self, app, client, campaign_id):
        login(client)
        _log_donation(client, campaign_id)
        cells = _cells(client.get("/admin/export/collections").data.decode())
        assert PAYLOAD not in cells
        assert f"'{PAYLOAD}" in cells

    def test_10bd_export(self, app, client, campaign_id):
        login(client)
        _log_donation(client, campaign_id, payment_mode="cash")
        cells = _cells(client.get("/admin/export/10bd").data.decode())
        # This donor may or may not appear (10BD only lists 80G-eligible
        # donations) -- either way, if they do appear, the payload must be
        # neutralized; if they don't, this is just confirming the route
        # still returns a normal CSV.
        assert PAYLOAD not in cells
