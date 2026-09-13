"""Safety tests for the durable Zoho entry queue."""
import datetime
from unittest.mock import patch


def _entry(payment_id="pay_Queue123"):
    return {
        "ID": "zoho-1", "Name": "Jatin, Saini", "Phone": "9650150283",
        "Payment Amount": "100", "Payment Transaction ID": payment_id,
        "Added Time": "13-Sep-2026 09:00:00",
    }


def _form():
    from extensions import db
    from models import Campaign, ZohoForm
    campaign = Campaign.query.filter_by(name="BACE Contribution").first()
    form = ZohoForm(form_key="QueueForm", campaign_id=campaign.id)
    db.session.add(form)
    db.session.commit()
    return form


def test_staging_is_idempotent_and_keeps_the_payment_id(app):
    from models import ZohoEntryQueue
    import zoho_queue

    with app.app_context():
        _form()
        with patch("zoho_api.entries", return_value=([_entry()], "records")):
            first = zoho_queue.stage_entries(app.config)
        with patch("zoho_api.entries", return_value=([_entry()], "records")):
            second = zoho_queue.stage_entries(app.config)
        row = ZohoEntryQueue.query.one()
        assert first["staged"] == 1 and second["already_known"] == 1
        assert row.razorpay_payment_id == "pay_Queue123"


def test_live_exact_match_receipts_then_scrubs_personal_payload(app):
    from extensions import db
    from models import ZohoEntryQueue
    import zoho_queue

    with app.app_context():
        _form()
        app.config["ZOHO_AUTOMATION_MODE"] = "live"
        row = ZohoEntryQueue(form_key="QueueForm", entry_fingerprint="a" * 64,
            razorpay_payment_id="pay_Queue123", razorpay_order_id="order_Queue123",
            full_name="Jatin Saini", phone="9650150283", email="j@example.com", amount="100")
        db.session.add(row)
        db.session.commit()

        class Donation:
            id = 42

        with patch("public._zoho_payment_is_captured", return_value=(True, None)), \
             patch("public._create_zoho_donation", return_value=(Donation(), None)):
            result = zoho_queue.process_entries(app.config)

        row = ZohoEntryQueue.query.one()
        assert result["created"] == 1 and row.status == "receipted"
        assert row.full_name is None and row.phone is None and row.email is None
        assert row.donation_id == 42


def test_shadow_match_is_held_for_live_approval_not_expired(app):
    from extensions import db
    from models import ZohoEntryQueue
    import zoho_queue

    with app.app_context():
        _form()
        app.config["ZOHO_AUTOMATION_MODE"] = "shadow"
        row = ZohoEntryQueue(form_key="QueueForm", entry_fingerprint="c" * 64,
            razorpay_payment_id="pay_Shadow123", full_name="Jatin Saini", phone="9650150283",
            amount="100", stored_at=datetime.datetime.utcnow() - datetime.timedelta(days=6))
        db.session.add(row)
        db.session.commit()
        with patch("public._zoho_payment_is_captured", return_value=(True, None)):
            assert zoho_queue.process_entries(app.config)["shadow_matches"] == 1
        assert row.status == "validated"
        assert zoho_queue.purge_expired(app.config) == 0
        assert row.full_name == "Jatin Saini"


def test_expiry_scrubs_an_unmatched_payload_after_five_days(app):
    from extensions import db
    from models import ZohoEntryQueue
    import zoho_queue

    with app.app_context():
        row = ZohoEntryQueue(form_key="QueueForm", entry_fingerprint="b" * 64,
            full_name="Temporary Donor", phone="9650150283", amount="100",
            stored_at=datetime.datetime.utcnow() - datetime.timedelta(days=6))
        db.session.add(row)
        db.session.commit()
        assert zoho_queue.purge_expired(app.config) == 1
        assert row.status == "expired" and row.full_name is None and row.phone is None


def test_internal_sync_needs_the_shared_scheduler_secret(client, app):
    app.config["INTERNAL_TASK_TOKEN"] = "scheduler-secret"
    assert client.post("/internal/zoho-entry-sync", json={}).status_code == 401
    response = client.post(
        "/internal/zoho-entry-sync", json={},
        headers={"X-Internal-Token": "scheduler-secret"},
    )
    assert response.status_code == 200
    assert response.get_json()["mode"] == "off"
