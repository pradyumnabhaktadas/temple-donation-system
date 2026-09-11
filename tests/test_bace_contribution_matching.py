"""Tests for turning a BACE Contribution donation into a BACE Rent
Tracker payment automatically (bace_matching.py), plus the manual
fallbacks (one-click "Record as rent payment", the bulk "Record all
matched" action, and "edit before saving") for donations that couldn't
auto-record at the time -- most commonly because their donor wasn't on
the BACE Students roster yet.

Drives the real routes through the Flask test client and checks what
landed in the database (or rendered), not internals -- same standard as
test_iyf_camps.py.
"""
import datetime
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


@pytest.fixture(autouse=True)
def _logged_in(client):
    login(client)


def _property(app, name="Nandgaon BACE"):
    from extensions import db
    from models import BaceProperty
    prop = BaceProperty(name=name)
    db.session.add(prop)
    db.session.commit()
    return prop.id


def _add_student(client, property_id, full_name="Amit Kumar", phone="9319880507",
                  monthly_amount="3000", joined_month="2026-09", **extra):
    data = {
        "full_name": full_name, "phone": phone, "bace_property_id": str(property_id),
        "monthly_amount": monthly_amount, "joined_month": joined_month,
        "room_notes": "", "notes": "",
    }
    data.update(extra)
    return client.post("/admin/bace-students", data=data, follow_redirects=True)


def _make_bace_contribution(client, prop_id, full_name="Amit Kumar", phone="9319880507", email=None, amount="1500"):
    """Goes through the real /admin/donations/manual route -- the same
    path _create_offline_donation() serves, which is what actually calls
    bace_matching.record_matched_donation() the instant the donation
    succeeds. If the donor's phone/email already matches exactly one
    BACE student at this point, the donation is auto-recorded as a rent
    payment before this call even returns."""
    from models import Campaign
    campaign = Campaign.query.filter_by(name="BACE Contribution").first()
    data = {
        "campaign_id": str(campaign.id), "amount": amount, "full_name": full_name,
        "phone": phone, "payment_mode": "cash", "bace_property_id": str(prop_id),
    }
    if email:
        data["email"] = email
    with patch("public.send_receipt_email"), patch("public.send_receipt_whatsapp"):
        return client.post("/admin/donations/manual", data=data, follow_redirects=True)


def _insert_legacy_bace_donation(prop_id, full_name="Legacy Donor", phone="9319880507",
                                  email=None, amount=1500, status="success"):
    """Inserts a Donation row directly, bypassing _create_offline_donation
    and its auto-record hook -- stands in for a donation that predates
    its donor being added to the BACE Students roster, or one that
    arrived through a path that doesn't auto-record (a historical
    import). Gives the manual "Record as rent payment" / "Record all
    matched" / "edit before saving" paths something real to exercise,
    since ordinary new donations now auto-record themselves."""
    from extensions import db
    from models import Donor, Campaign, Donation
    donor = Donor(full_name=full_name, phone=phone, email=email)
    db.session.add(donor)
    db.session.flush()
    campaign = Campaign.query.filter_by(name="BACE Contribution").first()
    donation = Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=amount,
        payment_mode="cash", status=status, bace_property_id=prop_id,
        receipt_number=f"TEST/{donor.id:06d}", donation_date=datetime.datetime.utcnow(),
    )
    db.session.add(donation)
    db.session.commit()
    return donation


class TestAutoRecordOnDonation:
    """The main path now: a donation to a donor already on the roster
    records its own rent payment the instant it succeeds -- nobody has
    to click anything."""

    def test_a_donation_matching_a_students_phone_auto_records(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Amit Kumar", phone="9319880507", monthly_amount="1500")
        _make_bace_contribution(client, prop_id, full_name="Amit Kumar", phone="9319880507", amount="1500")

        from models import BaceStudent, BaceRentPayment
        student = BaceStudent.query.filter_by(full_name="Amit Kumar").one()
        payment = BaceRentPayment.query.filter_by(student_id=student.id).one()
        assert float(payment.amount_paid) == 1500
        assert payment.recorded_by == "auto-match"
        assert payment.source_donation_id is not None

    def test_a_donation_matching_a_students_email_auto_records(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Email Student", phone="9222222222", email="student@example.com")
        # Different phone on the donation -- only the email should match.
        _make_bace_contribution(
            client, prop_id, full_name="Email Student", phone="9333333333", email="student@example.com",
        )

        from models import BaceStudent, BaceRentPayment
        student = BaceStudent.query.filter_by(full_name="Email Student").one()
        assert BaceRentPayment.query.filter_by(student_id=student.id).count() == 1

    def test_the_tracker_shows_it_immediately(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Tracker Student", phone="9319880507",
                      monthly_amount="1500", joined_month="2026-01")
        _make_bace_contribution(client, prop_id, full_name="Tracker Student", phone="9319880507", amount="1500")

        this_month = datetime.date.today().strftime("%Y-%m")
        resp = client.get(f"/admin/bace-tracker?from={this_month}&to={this_month}")
        body = resp.get_data(as_text=True)
        assert "Tracker Student" in body
        assert "Paid" in body

    def test_the_contribution_log_shows_recorded_not_a_button(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Shows Recorded", phone="9319880507")
        _make_bace_contribution(client, prop_id, full_name="Shows Recorded", phone="9319880507", amount="1234")

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "Recorded: Rs. 1234" in body
        assert "/record-rent-payment" not in body

    def test_an_ambiguous_match_does_not_auto_record(self, client, app):
        """A wrong guess here would misattribute someone else's rent, so a
        donor whose phone matches two students is left unrecorded."""
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Dup A", phone="9444444444")
        _add_student(client, prop_id, full_name="Dup B", phone="9444444444")
        _make_bace_contribution(client, prop_id, full_name="Shared Phone Donor", phone="9444444444")

        from models import BaceRentPayment
        assert BaceRentPayment.query.count() == 0

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "no match" in body

    def test_no_matching_student_leaves_it_unrecorded(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Someone Else", phone="9000000001")
        _make_bace_contribution(client, prop_id, full_name="Unrelated Donor", phone="9111111111")

        from models import BaceRentPayment
        assert BaceRentPayment.query.count() == 0

    def test_offline_donations_also_auto_record(self, client, app):
        """_create_offline_donation() is the one shared path behind both
        the single-entry form and bulk CSV import -- this exercises it
        exactly like a staff member logging a cash contribution."""
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Cash Payer", phone="9319880507", monthly_amount="2000")

        from models import Campaign
        campaign = Campaign.query.filter_by(name="BACE Contribution").first()
        with patch("public.send_receipt_email"), patch("public.send_receipt_whatsapp"):
            client.post("/admin/donations/manual", data={
                "campaign_id": str(campaign.id), "amount": "2000", "full_name": "Cash Payer",
                "phone": "9319880507", "payment_mode": "cash", "bace_property_id": str(prop_id),
            }, follow_redirects=True)

        from models import BaceStudent, BaceRentPayment
        student = BaceStudent.query.filter_by(full_name="Cash Payer").one()
        assert BaceRentPayment.query.filter_by(student_id=student.id).count() == 1


class TestCatchUpWhenStudentAdded:
    """A donation that arrived before its donor was on the roster can't
    auto-record at the time -- adding the student afterward catches it
    up immediately, with no separate click."""

    def test_adding_the_student_records_their_past_donation(self, client, app):
        prop_id = _property(app)
        donation = _insert_legacy_bace_donation(prop_id, full_name="Becomes Student", phone="9101010101", amount=1800)

        _add_student(client, prop_id, full_name="Becomes Student", phone="9101010101", monthly_amount="1800")

        from models import BaceRentPayment
        payment = BaceRentPayment.query.filter_by(source_donation_id=donation.id).one()
        assert float(payment.amount_paid) == 1800

        resp = client.get("/admin/bace-contributions")
        assert "Recorded: Rs. 1800" in resp.get_data(as_text=True)

    def test_the_add_student_flash_mentions_the_catch_up(self, client, app):
        prop_id = _property(app)
        _insert_legacy_bace_donation(prop_id, full_name="Flash Student", phone="9101010102")

        resp = _add_student(client, prop_id, full_name="Flash Student", phone="9101010102")
        body = resp.get_data(as_text=True)
        assert "Recorded 1 past BACE Contribution donation" in body

    def test_does_not_touch_an_unrelated_students_past_donation(self, client, app):
        prop_id = _property(app)
        _insert_legacy_bace_donation(prop_id, full_name="Unrelated", phone="9101010103")

        _add_student(client, prop_id, full_name="Someone New", phone="9101010104")

        from models import BaceRentPayment
        assert BaceRentPayment.query.count() == 0


class TestAddToRosterLink:
    def test_an_unmatched_success_donation_gets_an_add_to_roster_link(self, client, app):
        prop_id = _property(app, name="Nandgaon BACE")
        _make_bace_contribution(
            client, prop_id, full_name="New Resident", phone="9666666666", email="new.resident@example.com",
        )

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "Add to roster" in body
        assert "prefill_full_name=New+Resident" in body
        assert "prefill_phone=9666666666" in body
        assert "prefill_email=new.resident@example.com" in body
        assert f"prefill_bace_property_id={prop_id}" in body
        assert "Record as rent payment manually" in body

    def test_unmatched_donation_can_be_manually_recorded_against_selected_student(self, client, app):
        prop_id = _property(app, name="Manual BACE")
        donation = _make_bace_contribution(
            client, prop_id, full_name="Unmatched Payer", phone="9666666666", amount="6000",
        )
        _add_student(client, prop_id, full_name="Selected Resident", phone="9777777777")

        from models import Donation, BaceStudent, BaceRentPayment
        source_donation = Donation.query.order_by(Donation.id.desc()).first()
        selected = BaceStudent.query.filter_by(full_name="Selected Resident").one()

        resp = client.get("/admin/bace-contributions")
        assert f"prefill_source_donation_id={source_donation.id}" in resp.get_data(as_text=True)

        picker = client.get(f"/admin/bace-payments?prefill_source_donation_id={source_donation.id}")
        picker_body = picker.get_data(as_text=True)
        assert "Or choose from the BACE student list" in picker_body
        assert "Selected Resident" in picker_body

        client.post("/admin/bace-payments", data={
            "student_id": str(selected.id), "for_month": "2026-08", "amount_paid": "6000",
            "source_donation_id": str(source_donation.id),
        }, follow_redirects=True)

        payment = BaceRentPayment.query.filter_by(source_donation_id=source_donation.id).one()
        assert payment.student_id == selected.id

    def test_a_matched_donation_does_not_get_an_add_to_roster_link(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Already On Roster", phone="9777777777")
        _make_bace_contribution(client, prop_id, full_name="Already On Roster", phone="9777777777")

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "prefill_full_name=" not in body

    def test_only_success_donations_get_the_link(self, client, app):
        prop_id = _property(app)
        _insert_legacy_bace_donation(prop_id, full_name="Pending New Resident", phone="9888888888", status="pending")

        resp = client.get("/admin/bace-contributions?status=pending")
        body = resp.get_data(as_text=True)
        assert "prefill_full_name=" not in body

    def test_following_the_link_prefills_the_add_student_form(self, client, app):
        prop_id = _property(app, name="Yogapitha BACE")
        _make_bace_contribution(
            client, prop_id, full_name="Prefilled Resident", phone="9999999999",
            email="prefilled.resident@example.com",
        )

        resp = client.get(
            "/admin/bace-students"
            "?prefill_full_name=Prefilled+Resident&prefill_phone=9999999999"
            "&prefill_email=prefilled.resident%40example.com"
            f"&prefill_bace_property_id={prop_id}"
        )
        body = resp.get_data(as_text=True)
        assert 'value="Prefilled Resident"' in body
        assert 'value="9999999999"' in body
        assert 'value="prefilled.resident@example.com"' in body
        assert f'value="{prop_id}" selected' in body

    def test_adding_from_the_prefilled_form_auto_records_the_donation(self, client, app):
        prop_id = _property(app)
        _make_bace_contribution(client, prop_id, full_name="Becomes Student", phone="9101010101", amount="1500")

        client.post("/admin/bace-students", data={
            "full_name": "Becomes Student", "phone": "9101010101", "bace_property_id": str(prop_id),
            "monthly_amount": "1500", "joined_month": "2026-09", "room_notes": "", "notes": "",
        }, follow_redirects=True)

        from models import BaceStudent
        assert BaceStudent.query.filter_by(full_name="Becomes Student").count() == 1

        # Adding them just caught up their existing donation -- the log
        # shows it recorded, not an outstanding action.
        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "Recorded: Rs. 1500" in body


class TestManualRecordAsRentPayment:
    """The one-click button and its docstring both say most donations
    never need this -- these tests exercise it against a legacy donation
    that predates its donor being added, the case it exists for."""

    def test_the_button_appears_for_a_matched_unrecorded_donation(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Button Student", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Button Student", phone="9319880507")

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert f'action="/admin/bace-contributions/{donation.id}/record-rent-payment"' in body

    def test_clicking_it_records_using_the_donations_own_month(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Auto Student", phone="9319880507", monthly_amount="2500")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Auto Student", phone="9319880507", amount=2500)

        resp = client.post(
            f"/admin/bace-contributions/{donation.id}/record-rent-payment", data={}, follow_redirects=True,
        )
        assert resp.status_code == 200

        from models import BaceRentPayment
        payment = BaceRentPayment.query.filter_by(source_donation_id=donation.id).one()
        assert float(payment.amount_paid) == 2500
        assert payment.for_month == donation.donation_date.date().replace(day=1)
        assert payment.reference and donation.receipt_number in payment.reference

    def test_record_button_returns_to_the_same_filtered_log_page(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Keep My Place", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Keep My Place", phone="9319880507")

        resp = client.post(
            f"/admin/bace-contributions/{donation.id}/record-rent-payment",
            data={"return_to": f"/admin/bace-contributions?bace_property_id={prop_id}&page=2"},
            follow_redirects=False,
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith(
            f"/admin/bace-contributions?bace_property_id={prop_id}&page=2"
        )

    def test_record_button_backfills_missing_property_from_matched_student(self, client, app):
        prop_id = _property(app)
        donation = _insert_legacy_bace_donation(
            prop_id, full_name="Historical Property", phone="9319880507", amount=8000,
        )
        # Older imports can belong to the BACE campaign without carrying a
        # bace_property_id, even though the donor is on the student roster.
        from extensions import db
        donation.bace_property_id = None
        db.session.commit()
        _add_student(client, prop_id, full_name="Historical Property", phone="9319880507")

        client.post(
            f"/admin/bace-contributions/{donation.id}/record-rent-payment", data={},
            follow_redirects=True,
        )

        from models import BaceRentPayment, Donation
        payment = BaceRentPayment.query.filter_by(source_donation_id=donation.id).one()
        assert payment.student.bace_property_id == prop_id
        assert Donation.query.get(donation.id).bace_property_id == prop_id

    def test_recording_twice_is_refused(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Once Only", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Once Only", phone="9319880507")

        client.post(f"/admin/bace-contributions/{donation.id}/record-rent-payment", data={}, follow_redirects=True)
        client.post(f"/admin/bace-contributions/{donation.id}/record-rent-payment", data={}, follow_redirects=True)

        from models import BaceRentPayment
        assert BaceRentPayment.query.filter_by(source_donation_id=donation.id).count() == 1

    def test_an_ambiguous_match_is_refused_even_via_direct_post(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Dup A", phone="9444444444")
        _add_student(client, prop_id, full_name="Dup B", phone="9444444444")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Shared Phone Donor", phone="9444444444")

        resp = client.post(
            f"/admin/bace-contributions/{donation.id}/record-rent-payment", data={}, follow_redirects=True,
        )
        assert resp.status_code == 200
        from models import BaceRentPayment
        assert BaceRentPayment.query.filter_by(source_donation_id=donation.id).count() == 0

    def test_only_success_donations_can_be_recorded(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Pending Match", phone="9555555555")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Pending Match", phone="9555555555", status="pending")

        resp = client.get("/admin/bace-contributions?status=pending")
        assert f'action="/admin/bace-contributions/{donation.id}/record-rent-payment"' not in resp.get_data(as_text=True)

        client.post(f"/admin/bace-contributions/{donation.id}/record-rent-payment", data={}, follow_redirects=True)
        from models import BaceRentPayment
        assert BaceRentPayment.query.filter_by(source_donation_id=donation.id).count() == 0

    def test_staff_role_does_not_see_the_button(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Staff View", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Staff View", phone="9319880507")

        login(client, username="teststaff", password="TestPass123!")
        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "Staff View" in body
        assert f'action="/admin/bace-contributions/{donation.id}/record-rent-payment"' not in body


class TestRecordAllMatchedBulkAction:
    def test_records_every_matched_unrecorded_donation(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Bulk One", phone="9111111101", monthly_amount="1000")
        _add_student(client, prop_id, full_name="Bulk Two", phone="9111111102", monthly_amount="2000")
        d1 = _insert_legacy_bace_donation(prop_id, full_name="Bulk One", phone="9111111101", amount=1000)
        d2 = _insert_legacy_bace_donation(prop_id, full_name="Bulk Two", phone="9111111102", amount=2000)

        resp = client.post("/admin/bace-contributions/record-all-matched", data={}, follow_redirects=True)
        assert resp.status_code == 200
        assert "Recorded 2 rent payment(s)" in resp.get_data(as_text=True)

        from models import BaceRentPayment
        assert BaceRentPayment.query.filter_by(source_donation_id=d1.id).count() == 1
        assert BaceRentPayment.query.filter_by(source_donation_id=d2.id).count() == 1

    def test_skips_ambiguous_and_unmatched_donations(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Dup A", phone="9444444444")
        _add_student(client, prop_id, full_name="Dup B", phone="9444444444")
        _insert_legacy_bace_donation(prop_id, full_name="Ambiguous", phone="9444444444")
        _insert_legacy_bace_donation(prop_id, full_name="No Match At All", phone="9333333333")

        client.post("/admin/bace-contributions/record-all-matched", data={}, follow_redirects=True)

        from models import BaceRentPayment
        assert BaceRentPayment.query.count() == 0

    def test_a_second_run_is_a_no_op(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Idempotent", phone="9319880507")
        _insert_legacy_bace_donation(prop_id, full_name="Idempotent", phone="9319880507")

        client.post("/admin/bace-contributions/record-all-matched", data={}, follow_redirects=True)
        resp = client.post("/admin/bace-contributions/record-all-matched", data={}, follow_redirects=True)

        assert "Nothing to record" in resp.get_data(as_text=True)
        from models import BaceRentPayment
        assert BaceRentPayment.query.count() == 1

    def test_staff_role_cannot_trigger_it(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Guarded", phone="9319880507")
        _insert_legacy_bace_donation(prop_id, full_name="Guarded", phone="9319880507")

        login(client, username="teststaff", password="TestPass123!")
        client.post("/admin/bace-contributions/record-all-matched", data={}, follow_redirects=True)

        from models import BaceRentPayment
        assert BaceRentPayment.query.count() == 0


class TestEditBeforeSavingFallback:
    def test_the_fallback_link_prefills_the_payments_log_form(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Fallback Student", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Fallback Student", phone="9319880507", amount=2500)

        from models import BaceStudent
        student = BaceStudent.query.filter_by(full_name="Fallback Student").one()

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "wrong month? edit before saving" in body
        assert f"prefill_student_id={student.id}" in body
        assert f"prefill_source_donation_id={donation.id}" in body
        assert "return_to=/admin/bace-contributions" in body

        follow = client.get(
            f"/admin/bace-payments?prefill_student_id={student.id}"
            f"&prefill_amount=2500.0&prefill_source_donation_id={donation.id}"
        )
        follow_body = follow.get_data(as_text=True)
        assert "Fallback Student" in follow_body
        assert f'name="source_donation_id" value="{donation.id}"' in follow_body

    def test_saving_from_the_fallback_returns_to_contribution_logs(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Return Here", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Return Here", phone="9319880507")

        from models import BaceStudent
        student = BaceStudent.query.filter_by(full_name="Return Here").one()

        resp = client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
            "source_donation_id": str(donation.id),
            "return_to": "/admin/bace-contributions?status=success&page=2",
        }, follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith(
            "/admin/bace-contributions?status=success&page=2"
        )

    def test_fallback_rejects_external_return_url(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Safe Return", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Safe Return", phone="9319880507")

        from models import BaceStudent
        student = BaceStudent.query.filter_by(full_name="Safe Return").one()

        resp = client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
            "source_donation_id": str(donation.id), "return_to": "https://example.com",
        }, follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/admin/bace-payments")

    def test_saving_via_the_fallback_form_also_tags_source_donation_id(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Via Fallback", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Via Fallback", phone="9319880507")

        from models import BaceStudent, BaceRentPayment
        student = BaceStudent.query.filter_by(full_name="Via Fallback").one()

        client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
            "source_donation_id": str(donation.id),
        }, follow_redirects=True)

        payment = BaceRentPayment.query.filter_by(source_donation_id=donation.id).one()
        assert payment.student_id == student.id

        resp = client.get("/admin/bace-contributions")
        assert "Recorded" in resp.get_data(as_text=True)

    def test_the_fallback_form_also_refuses_a_duplicate(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Dup Fallback", phone="9319880507")
        donation = _insert_legacy_bace_donation(prop_id, full_name="Dup Fallback", phone="9319880507")

        from models import BaceStudent, BaceRentPayment
        student = BaceStudent.query.filter_by(full_name="Dup Fallback").one()

        client.post(f"/admin/bace-contributions/{donation.id}/record-rent-payment", data={}, follow_redirects=True)
        client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
            "source_donation_id": str(donation.id),
        }, follow_redirects=True)

        assert BaceRentPayment.query.filter_by(source_donation_id=donation.id).count() == 1
