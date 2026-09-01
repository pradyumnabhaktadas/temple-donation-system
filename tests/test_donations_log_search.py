"""Tests for the Donations Log search box (?q=...) -- matches donor name,
phone/WhatsApp number, email, receipt number, and every payment reference
staff might have in hand (Razorpay order/payment ID, cheque number, bank
transfer UTR). Shares _apply_donations_filters() with the CSV export, so
covering the /admin/donations route also covers the export honoring the
same search.
"""
from conftest import login


def _make_donation(app, **overrides):
    from extensions import db
    from models import Campaign, Donor, Donation

    with app.app_context():
        campaign = Campaign.query.filter_by(name="BACE Contribution").first()
        donor_kwargs = {
            "full_name": overrides.pop("full_name", "Search Test Donor"),
            "phone": overrides.pop("phone", "9111000001"),
            "whatsapp_number": overrides.pop("whatsapp_number", None),
            "email": overrides.pop("email", None),
        }
        donor = Donor(**donor_kwargs)
        db.session.add(donor)
        db.session.commit()

        donation_kwargs = {
            "donor_id": donor.id, "campaign_id": campaign.id, "amount": 501,
            "payment_mode": "cash", "status": "success", "recorded_by": "testadmin",
        }
        donation_kwargs.update(overrides)
        donation = Donation(**donation_kwargs)
        db.session.add(donation)
        db.session.commit()
        return donation.id


class TestDonationsLogSearch:
    def test_matches_donor_full_name(self, app, client):
        login(client)
        did = _make_donation(app, full_name="Radha Krishna Sharma", receipt_number="SRCH/1")
        _make_donation(app, full_name="Someone Else", receipt_number="SRCH/2")

        html = client.get("/admin/donations?status=all&range=all&q=Radha+Krishna").data.decode()

        assert f'id="donationDetails{did}"' in html
        assert "Someone Else" not in html

    def test_matches_donor_phone(self, app, client):
        login(client)
        did = _make_donation(app, phone="9876512345", receipt_number="SRCH/3")
        _make_donation(app, phone="9111222333", receipt_number="SRCH/4")

        html = client.get("/admin/donations?status=all&range=all&q=9876512345").data.decode()

        assert f'id="donationDetails{did}"' in html

    def test_matches_donor_whatsapp_number(self, app, client):
        login(client)
        did = _make_donation(app, phone="9000000001", whatsapp_number="9822233344", receipt_number="SRCH/5")

        html = client.get("/admin/donations?status=all&range=all&q=9822233344").data.decode()

        assert f'id="donationDetails{did}"' in html

    def test_matches_donor_email(self, app, client):
        login(client)
        did = _make_donation(app, email="findme@example.org", receipt_number="SRCH/6")

        html = client.get("/admin/donations?status=all&range=all&q=findme@example.org").data.decode()

        assert f'id="donationDetails{did}"' in html

    def test_matches_receipt_number(self, app, client):
        login(client)
        did = _make_donation(app, receipt_number="ISK500123")

        html = client.get("/admin/donations?status=all&range=all&q=ISK500123").data.decode()

        assert f'id="donationDetails{did}"' in html

    def test_matches_razorpay_payment_id(self, app, client):
        login(client)
        did = _make_donation(
            app, receipt_number="SRCH/7", payment_mode="online",
            razorpay_order_id="order_ABC123", razorpay_payment_id="pay_XYZ789",
        )

        html = client.get("/admin/donations?status=all&range=all&q=pay_XYZ789").data.decode()
        assert f'id="donationDetails{did}"' in html

        html = client.get("/admin/donations?status=all&range=all&q=order_ABC123").data.decode()
        assert f'id="donationDetails{did}"' in html

    def test_matches_cheque_number_and_bank_transaction_id(self, app, client):
        login(client)
        cheque_id = _make_donation(
            app, receipt_number="SRCH/8", payment_mode="cheque", cheque_number="CHQ998877",
        )
        bank_id = _make_donation(
            app, receipt_number="SRCH/9", payment_mode="bank_transfer", bank_transaction_id="UTR112233",
        )

        html = client.get("/admin/donations?status=all&range=all&q=CHQ998877").data.decode()
        assert f'id="donationDetails{cheque_id}"' in html

        html = client.get("/admin/donations?status=all&range=all&q=UTR112233").data.decode()
        assert f'id="donationDetails{bank_id}"' in html

    def test_is_case_insensitive_and_partial(self, app, client):
        login(client)
        did = _make_donation(app, full_name="Krishna Das Gupta", receipt_number="SRCH/10")

        html = client.get("/admin/donations?status=all&range=all&q=krishna+das").data.decode()

        assert f'id="donationDetails{did}"' in html

    def test_no_match_returns_empty_result_not_an_error(self, app, client):
        login(client)
        _make_donation(app, full_name="Someone", receipt_number="SRCH/11")

        resp = client.get("/admin/donations?status=all&range=all&q=NoSuchDonorAtAll")

        assert resp.status_code == 200
        assert b"SRCH/11" not in resp.data
        assert b"No donations found." in resp.data

    def test_search_is_honored_by_csv_export(self, app, client):
        login(client)
        did = _make_donation(app, full_name="Export Search Donor", receipt_number="SRCH/12")
        _make_donation(app, full_name="Other Donor", receipt_number="SRCH/13")

        resp = client.get("/admin/export/donations?status=all&range=all&q=Export+Search")
        body = resp.data.decode()

        assert "SRCH/12" in body
        assert "SRCH/13" not in body

    def test_blank_query_does_not_filter(self, app, client):
        login(client)
        _make_donation(app, full_name="Donor One", receipt_number="SRCH/14")
        _make_donation(app, full_name="Donor Two", receipt_number="SRCH/15")

        html = client.get("/admin/donations?status=all&range=all&q=").data.decode()

        assert "SRCH/14" in html
        assert "SRCH/15" in html
