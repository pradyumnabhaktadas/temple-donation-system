"""Tests for Admin -> BACE Contribution Logs' "Rent Tracker Match" column:
best-effort matching of a BACE Contribution donation's donor to a
BaceStudent by phone or email, with a "Record as rent payment" quick-link
that pre-fills (but never auto-submits) the Payments Log form. Drives the
real routes through the Flask test client and checks what rendered, not
internals -- same standard as test_iyf_camps.py.
"""
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


class TestMatchingByPhone:
    def test_a_donation_matching_a_students_phone_is_flagged(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Amit Kumar", phone="9319880507")
        _make_bace_contribution(client, prop_id, full_name="Amit Kumar", phone="9319880507")

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "Record as rent payment" in body

    def test_a_donation_with_no_matching_student_shows_no_match(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Someone Else", phone="9000000001")
        _make_bace_contribution(client, prop_id, full_name="Unrelated Donor", phone="9111111111")

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "no match" in body
        assert "prefill_student_id" not in body


class TestMatchingByEmail:
    def test_a_donation_matching_a_students_email_is_flagged(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Email Student", phone="9222222222", email="student@example.com")
        # Different phone on the donation -- only the email should match.
        _make_bace_contribution(
            client, prop_id, full_name="Email Student", phone="9333333333", email="student@example.com",
        )

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "Record as rent payment" in body


class TestAmbiguousMatchIsRefused:
    def test_two_students_sharing_a_phone_number_leaves_the_donation_unmatched(self, client, app):
        """A wrong guess here would misattribute someone else's rent, so an
        ambiguous phone/email match must never auto-pick one."""
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Student A", phone="9444444444")
        _add_student(client, prop_id, full_name="Student B", phone="9444444444")
        _make_bace_contribution(client, prop_id, full_name="Shared Phone Donor", phone="9444444444")

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert "no match" in body
        assert "prefill_student_id" not in body


class TestRecordAsRentPaymentLink:
    def test_the_link_prefills_student_amount_and_reference(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Link Student", phone="9319880507")
        _make_bace_contribution(client, prop_id, full_name="Link Student", phone="9319880507", amount="2500")

        from models import BaceStudent, Donation
        student = BaceStudent.query.filter_by(full_name="Link Student").one()
        donation = Donation.query.order_by(Donation.id.desc()).first()

        resp = client.get("/admin/bace-contributions")
        body = resp.get_data(as_text=True)
        assert f"prefill_student_id={student.id}" in body
        assert "prefill_amount=2500" in body
        assert donation.receipt_number.replace("/", "%2F") in body or donation.receipt_number in body

        # Following the link actually pre-fills the Payments Log form.
        follow = client.get(f"/admin/bace-payments?prefill_student_id={student.id}&prefill_amount=2500.0")
        follow_body = follow.get_data(as_text=True)
        assert "Link Student" in follow_body
        assert 'value="2500.0"' in follow_body

    def test_only_success_donations_get_a_link(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Pending Match", phone="9555555555")

        from extensions import db
        from models import Donor, Campaign, Donation
        donor = Donor(full_name="Pending Match", phone="9555555555")
        db.session.add(donor)
        db.session.commit()
        campaign = Campaign.query.filter_by(name="BACE Contribution").first()
        donation = Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=500,
            payment_mode="online", status="pending", bace_property_id=prop_id,
        )
        db.session.add(donation)
        db.session.commit()

        resp = client.get("/admin/bace-contributions?status=pending")
        body = resp.get_data(as_text=True)
        assert "prefill_student_id" not in body
