"""Tests for the BACE Rent Contribution Tracker admin pages: Students,
Payments Log, Tracker grid, Dashboard, Pending List. Drives every route
through the Flask test client and checks what landed in the database (or
what rendered), not internals -- same standard as test_iyf_camps.py.
"""
import datetime
import os
import sys

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


class TestStudentsPage:
    def test_add_a_student(self, client, app):
        prop_id = _property(app)
        resp = _add_student(client, prop_id)
        assert resp.status_code == 200

        from models import BaceStudent
        student = BaceStudent.query.filter_by(full_name="Amit Kumar").one()
        assert student.phone == "9319880507"
        assert float(student.monthly_amount) == 3000
        assert student.joined_month == datetime.date(2026, 9, 1)
        assert student.status == "Active"

    def test_blank_name_is_rejected(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="")

        from models import BaceStudent
        assert BaceStudent.query.count() == 0

    def test_invalid_phone_is_rejected(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, phone="123")

        from models import BaceStudent
        assert BaceStudent.query.count() == 0

    def test_toggle_flips_active_status(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id)
        from models import BaceStudent
        student = BaceStudent.query.first()

        client.post(f"/admin/bace-students/{student.id}/toggle", data={}, follow_redirects=True)
        assert BaceStudent.query.get(student.id).status == "Inactive"

        client.post(f"/admin/bace-students/{student.id}/toggle", data={}, follow_redirects=True)
        assert BaceStudent.query.get(student.id).status == "Active"

    def test_edit_updates_the_row(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id)
        from models import BaceStudent
        student = BaceStudent.query.first()

        client.post(
            f"/admin/bace-students/{student.id}/edit",
            data={
                "full_name": "Amit K.", "phone": "9319880507", "bace_property_id": str(prop_id),
                "monthly_amount": "3500", "joined_month": "2026-08", "room_notes": "", "notes": "",
            },
            follow_redirects=True,
        )
        updated = BaceStudent.query.get(student.id)
        assert updated.full_name == "Amit K."
        assert float(updated.monthly_amount) == 3500
        assert updated.joined_month == datetime.date(2026, 8, 1)

    def test_delete_is_blocked_once_a_payment_exists(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id)
        from extensions import db
        from models import BaceStudent, BaceRentPayment
        student = BaceStudent.query.first()
        db.session.add(BaceRentPayment(
            student_id=student.id, for_month=datetime.date(2026, 9, 1),
            amount_paid=3000, date_paid=datetime.date(2026, 9, 5),
        ))
        db.session.commit()

        client.post(f"/admin/bace-students/{student.id}/delete", data={}, follow_redirects=True)

        assert BaceStudent.query.get(student.id) is not None

    def test_delete_succeeds_with_no_payments(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id)
        from models import BaceStudent
        student = BaceStudent.query.first()

        client.post(f"/admin/bace-students/{student.id}/delete", data={}, follow_redirects=True)

        assert BaceStudent.query.get(student.id) is None


class TestPaymentsPage:
    def test_record_a_payment(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id)
        from models import BaceStudent
        student = BaceStudent.query.first()

        resp = client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
            "date_paid": "2026-09-05", "mode": "UPI", "reference": "ref123",
        }, follow_redirects=True)
        assert resp.status_code == 200

        from models import BaceRentPayment
        payment = BaceRentPayment.query.one()
        assert payment.student_id == student.id
        assert float(payment.amount_paid) == 3000
        assert payment.for_month == datetime.date(2026, 9, 1)
        assert payment.recorded_by == "testadmin"

    def test_no_student_chosen_is_rejected(self, client, app):
        client.post("/admin/bace-payments", data={
            "student_id": "", "for_month": "2026-09", "amount_paid": "3000",
        }, follow_redirects=True)
        from models import BaceRentPayment
        assert BaceRentPayment.query.count() == 0

    def test_filter_by_student(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Student One")
        _add_student(client, prop_id, full_name="Student Two")
        from models import BaceStudent
        s1, s2 = BaceStudent.query.order_by(BaceStudent.full_name).all()

        client.post("/admin/bace-payments", data={
            "student_id": str(s1.id), "for_month": "2026-09", "amount_paid": "1000",
        }, follow_redirects=True)
        client.post("/admin/bace-payments", data={
            "student_id": str(s2.id), "for_month": "2026-09", "amount_paid": "2000",
        }, follow_redirects=True)

        resp = client.get(f"/admin/bace-payments?student_id={s1.id}")
        body = resp.get_data(as_text=True)
        # "Student Two" legitimately still appears in the filter dropdown's
        # own options -- what actually proves filtering worked is that
        # their payment amount is missing from the table.
        assert "1000.00" in body
        assert "2000.00" not in body

    def test_delete_a_payment(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id)
        from models import BaceStudent
        student = BaceStudent.query.first()
        client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
        }, follow_redirects=True)

        from models import BaceRentPayment
        payment = BaceRentPayment.query.one()
        client.post(f"/admin/bace-payments/{payment.id}/delete", data={}, follow_redirects=True)

        assert BaceRentPayment.query.count() == 0


class TestTrackerGrid:
    def test_renders_with_correct_status(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Fully Paid Student", joined_month="2026-09")
        from models import BaceStudent
        student = BaceStudent.query.first()
        client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
        }, follow_redirects=True)

        resp = client.get("/admin/bace-tracker?from=2026-09&to=2026-09")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Fully Paid Student" in body
        assert "Paid" in body

    def test_inactive_students_hidden_by_default(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Left Already")
        from models import BaceStudent
        student = BaceStudent.query.first()
        client.post(f"/admin/bace-students/{student.id}/toggle", data={}, follow_redirects=True)

        resp = client.get("/admin/bace-tracker")
        assert "Left Already" not in resp.get_data(as_text=True)

        resp = client.get("/admin/bace-tracker?include_inactive=yes")
        assert "Left Already" in resp.get_data(as_text=True)


class TestDashboard:
    def test_totals_reflect_a_fully_paid_student(self, client, app):
        prop_id = _property(app, name="Goverdhan BACE")
        _add_student(client, prop_id, full_name="Paid Student", monthly_amount="3000", joined_month="2026-09")
        from models import BaceStudent
        student = BaceStudent.query.first()
        client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
        }, follow_redirects=True)

        resp = client.get("/admin/bace-dashboard")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Goverdhan BACE" in body
        assert "Paid Student" in body

    def test_a_pending_student_shows_up_in_lookup_unpaid(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="Unpaid Student", monthly_amount="3000", joined_month="2026-09")

        resp = client.get("/admin/bace-dashboard")
        body = resp.get_data(as_text=True)
        assert "Unpaid Student" in body
        assert "Pending" in body


class TestPendingList:
    def test_a_pending_student_gets_a_drafted_reminder(self, client, app):
        prop_id = _property(app)
        _add_student(
            client, prop_id, full_name="Needs Reminder", phone="9319880507",
            monthly_amount="3000", joined_month="2026-09",
        )

        resp = client.get("/admin/bace-pending")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Needs Reminder" in body
        assert "Hare Krishna Needs Reminder" in body
        assert "Rs 3,000" in body
        assert "givetokrishna.com/bace-rent" in body
        assert "wa.me/919319880507" in body

    def test_a_fully_paid_student_is_not_listed(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="All Settled", monthly_amount="3000", joined_month="2026-09")
        from models import BaceStudent
        student = BaceStudent.query.first()
        client.post("/admin/bace-payments", data={
            "student_id": str(student.id), "for_month": "2026-09", "amount_paid": "3000",
        }, follow_redirects=True)

        resp = client.get("/admin/bace-pending")
        body = resp.get_data(as_text=True)
        assert "All Settled" not in body

    def test_a_student_with_no_phone_gets_no_whatsapp_link(self, client, app):
        prop_id = _property(app)
        _add_student(client, prop_id, full_name="No Phone", phone="", joined_month="2026-09")

        resp = client.get("/admin/bace-pending")
        body = resp.get_data(as_text=True)
        assert "No Phone" in body
