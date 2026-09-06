"""Tests for naming a confirmed payment from the submissions sheet.

The direction matters and is the whole point. Razorpay has already
established that a payment happened, for an amount, from a phone. The
sheet is asked one question -- what is this payer called -- and never
"does this deserve a receipt", which it cannot answer: it has no payment
id, and its own Payment Status column has been wrong in both directions.

Rows here are modelled on the real export from this account, including
its quirks: "First, Last" names, phone numbers written without a +, cash
registrations with no payment at all, and the same person submitting
several times in one evening.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import zoho_sheet


SHEET = """Added Time,IP Address,Name,Phone,Gender,Age,Mode of Payment,Payment Amount,Payment Status
07-Jul-2026 19:27:58,106.221.230.73,"Harshit, Prajapati",919220830216,Male,13,,,Processing not needed
07-Jul-2026 19:28:50,106.221.230.73,"Harshit, Prajapati",919220830216,Male,14,Online (UPI),100,Failed
07-Jul-2026 19:30:51,106.221.230.73,"Harshit, Prajapati",919220830216,Male,14,Online (UPI),100,Failed
07-Jul-2026 19:43:21,106.219.236.141,"Sachin, .",918743071401,Male,21,Online (UPI),100,Completed
08-Jul-2026 20:09:36,152.58.116.29,"Divyansh, Narang",919818359809,Male,20,Offline (Cash),,Processing not needed
04-Sep-2026 21:55:22,27.59.76.227,"shivam, raj",919319880507,Male,27,Online (UPI),100,Completed
04-Sep-2026 16:06:00,27.59.76.227,"Kshitij, Kansal",919023555958,Male,30,Online (UPI),1100,Completed
"""


def _rows():
    import csv
    import io
    return list(csv.DictReader(io.StringIO(SHEET)))


@pytest.fixture
def index():
    return zoho_sheet.index_by_phone(_rows())


class TestNaming:
    def test_it_names_a_payer_from_their_phone(self, index):
        name, reason = zoho_sheet.resolve_name(index, "+919319880507", 100)
        assert name == "Shivam Raj" and reason is None

    def test_razorpays_plus_prefix_matches_zohos_bare_digits(self, index):
        """Zoho writes 919220830216, Razorpay returns +919220830216."""
        assert zoho_sheet.resolve_name(index, "+919220830216", 100)[0] == "Harshit Prajapati"

    def test_repeat_submissions_from_one_donor_are_not_an_obstacle(self, index):
        """Harshit appears three times in four minutes. Under the old
        payment-matching design that was an unresolvable ambiguity; here
        every candidate agrees on the name, so there is nothing to
        decide."""
        name, reason = zoho_sheet.resolve_name(index, "919220830216")
        assert name == "Harshit Prajapati" and reason is None

    def test_the_amount_separates_two_donations_from_one_person(self, index):
        """Someone who registers for a Rs. 100 seminar and separately
        gives Rs. 1,100 must not have the two conflated."""
        assert zoho_sheet.resolve_name(index, "919023555958", 1100)[0] == "Kshitij Kansal"

    def test_an_unknown_phone_is_reported_not_guessed(self, index):
        name, reason = zoho_sheet.resolve_name(index, "919999999999", 100)
        assert name is None and "no submission" in reason

    def test_a_payment_with_no_phone_is_reported(self, index):
        name, reason = zoho_sheet.resolve_name(index, "", 100)
        assert name is None and "phone" in reason

    def test_one_phone_under_two_names_is_refused(self):
        """A shared family phone. A receipt carrying the wrong donor's
        name is worse than one that waits for a person."""
        rows = _rows() + [{
            "Name": "Someone, Else", "Phone": "919319880507",
            "Payment Amount": "100", "Payment Status": "Completed",
        }]
        index = zoho_sheet.index_by_phone(rows)
        name, reason = zoho_sheet.resolve_name(index, "919319880507", 100)
        assert name is None and "different names" in reason

    def test_zohos_composite_name_reads_first_then_last(self):
        assert zoho_sheet.donor_name({"Name": "shivam, raj"}) == "Shivam Raj"
        assert zoho_sheet.donor_name({"Name": "Jatin, saini"}) == "Jatin Saini"

    def test_a_cash_registration_still_has_a_name(self, index):
        """No payment, but if one ever turns up the name is there."""
        assert zoho_sheet.resolve_name(index, "919818359809")[0] == "Divyansh Narang"


class TestFetching:
    def test_an_unreachable_sheet_is_an_error_not_an_empty_result(self):
        """"Couldn't read the sheet" and "no submissions" must never look
        alike -- treating a failed fetch as "nothing to do" is the exact
        silent-nothing failure this whole body of work exists to remove."""
        import requests
        with patch("zoho_sheet.requests.get", side_effect=requests.RequestException("dns")):
            rows, error = zoho_sheet.fetch_rows({"ZOHO_SHEET_CSV_URL": "https://x/csv"})
        assert rows == [] and error

    def test_an_http_error_is_reported(self):
        resp = MagicMock(status_code=404, text="nope")
        with patch("zoho_sheet.requests.get", return_value=resp):
            rows, error = zoho_sheet.fetch_rows({"ZOHO_SHEET_CSV_URL": "https://x/csv"})
        assert rows == [] and "404" in error

    def test_a_login_page_instead_of_csv_says_so_plainly(self):
        """The commonest misconfiguration: the sheet isn't actually
        published, or the link is the editor URL. Google answers with
        HTML, which csv would happily parse into nonsense."""
        resp = MagicMock(status_code=200, text="<!DOCTYPE html><html>Sign in</html>")
        with patch("zoho_sheet.requests.get", return_value=resp):
            rows, error = zoho_sheet.fetch_rows({"ZOHO_SHEET_CSV_URL": "https://x"})
        assert rows == [] and "publish to web" in error

    def test_not_being_configured_is_not_an_error(self):
        assert zoho_sheet.fetch_rows({"ZOHO_SHEET_CSV_URL": ""}) == ([], None)

    def test_a_real_csv_parses(self):
        resp = MagicMock(status_code=200, text=SHEET)
        with patch("zoho_sheet.requests.get", return_value=resp):
            rows, error = zoho_sheet.fetch_rows({"ZOHO_SHEET_CSV_URL": "https://x/csv"})
        assert error is None and len(rows) == 7


class TestExtras:
    def test_optional_details_are_picked_up_when_present(self):
        row = {"Name": "A, B", "Email": "a@example.com", "PAN": "ABCDE1234F"}
        assert zoho_sheet.extras(row) == {"email": "a@example.com", "pan": "ABCDE1234F"}

    def test_a_missing_email_or_pan_never_blocks_anything(self):
        assert zoho_sheet.extras({"Name": "A, B"}) == {}

    def test_a_malformed_email_is_left_out_rather_than_stored(self):
        assert "email" not in zoho_sheet.extras({"Email": "not-an-email"})
