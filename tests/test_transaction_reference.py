"""The payment reference typed into an offline entry has to come back out.

Staff record a payment that already happened somewhere else -- a Razorpay
or Zoho transaction id, a bank UTR, a camp collection's reference -- and
that string is the only thread back to the payment in the other system.
Every place it can be read (Donations Log, detail panel, CSV exports, the
receipt PDF) goes through one property, Donation.reference_display, so a
gap there loses it everywhere at once. Which is what happened: the
property allowed the reference only for payment_mode "bank_transfer", so
an "online" donation logged through the Offline Donation form stored the
id and displayed it nowhere.

The narrow fix -- adding "online" to the allowed list -- would have left
the same bug in the IYF camp form, which offers the same reference field
for Cheque while collecting no cheque number. So these tests cover every
form that can write the field, not just the one that was reported.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login

TXN = "pay_TO5ASGCNZOi4fP"


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
    """POST the real Offline Donation form."""
    data = {
        "campaign_id": campaign_id,
        "full_name": "Ravi Sharma",
        "phone": "9876543210",
        "amount": "1100",
        "payment_mode": "online",
        "bank_transaction_id": TXN,
        "donation_date": "2026-08-01",
    }
    data.update(overrides)
    return client.post("/admin/donations/manual", data=data, follow_redirects=True)


def _receipt_field_values(monkeypatch):
    """Record every value drawn into a labelled box on the receipt.

    The rendered PDF can't be read back with a substring search --
    reportlab embeds subsetted TrueType fonts, so the visible text isn't
    present as literal bytes in the file (searching for it returns a
    confident, wrong "not found"; that cost an hour). The repo has no PDF
    text extractor and no network to install one, so instead we capture
    what the drawing code is actually handed, keyed by which box it goes
    in. The output of one such render was separately confirmed with
    pdftotext outside the suite -- the transaction id appears under
    "Payment Details (Cheque / Transaction Details)".
    """
    import pdf_utils

    drawn = {}
    real_box_label = pdf_utils._box_label_above
    real_value = pdf_utils._value_in_box
    labels = {}

    def spy_label(c, box, text, *args, **kwargs):
        labels[tuple(box)] = text
        return real_box_label(c, box, text, *args, **kwargs)

    def spy_value(c, box, value, *args, **kwargs):
        drawn[labels.get(tuple(box), tuple(box))] = value
        return real_value(c, box, value, *args, **kwargs)

    monkeypatch.setattr(pdf_utils, "_box_label_above", spy_label)
    monkeypatch.setattr(pdf_utils, "_value_in_box", spy_value)
    return drawn


PAYMENT_DETAILS_LABEL = "Payment Details (Cheque / Transaction Details)"
MODE_LABEL = "Mode of Payment (Cheque / Online / UPI / Cash)"


class TestOnlineModeReference:
    """The reported bug: Payment Mode = Online in the Offline Donation log."""

    def test_reference_display_returns_the_transaction_id(self, app, client, campaign_id):
        from models import Donation
        login(client)
        _log_donation(client, campaign_id)
        with app.app_context():
            donation = Donation.query.one()
            assert donation.bank_transaction_id == TXN, "not even stored"
            assert donation.reference_display == TXN

    def test_it_appears_in_the_donations_log(self, app, client, campaign_id):
        login(client)
        _log_donation(client, campaign_id)
        assert TXN in client.get("/admin/donations?range=all").data.decode()

    def test_it_appears_in_the_donations_csv_export(self, app, client, campaign_id):
        login(client)
        _log_donation(client, campaign_id)
        assert TXN in client.get("/admin/export/donations?range=all").data.decode()

    def test_it_is_printed_on_the_receipt(self, app, client, campaign_id, monkeypatch):
        """The half of the request that isn't visible on screen -- the donor's
        copy has to carry the reference too, or they can't match the receipt
        to the payment on their own statement."""
        drawn = _receipt_field_values(monkeypatch)
        login(client)
        _log_donation(client, campaign_id)
        assert drawn.get(PAYMENT_DETAILS_LABEL) == TXN
        assert drawn.get(MODE_LABEL) == "Online"

    def test_the_stored_pdf_is_real(self, app, client, campaign_id):
        """Guards the test above: it asserts on what was handed to the
        renderer, so it would still pass if the render then blew up and
        stored nothing."""
        from models import Donation
        login(client)
        _log_donation(client, campaign_id)
        with app.app_context():
            pdf = Donation.query.one().receipt_pdf
            assert pdf and pdf.startswith(b"%PDF")

    def test_online_without_a_reference_is_still_accepted(self, app, client, campaign_id):
        """The field is optional for Online -- staff recording a payment
        after the fact don't always have the id, and refusing the entry
        over it would lose the donation, not just the reference."""
        from models import Donation
        login(client)
        _log_donation(client, campaign_id, bank_transaction_id="")
        with app.app_context():
            donation = Donation.query.one()
            assert donation.amount == 1100
            assert donation.reference_display is None


class TestTheOtherPaymentModesStillBehave:
    """The fix widened reference_display; these pin what it must not change."""

    def test_bank_transfer_utr(self, app, client, campaign_id):
        from models import Donation
        login(client)
        _log_donation(client, campaign_id, payment_mode="bank_transfer",
                      bank_transaction_id="UTR2026042212345")
        with app.app_context():
            assert Donation.query.one().reference_display == "UTR2026042212345"

    def test_cheque_still_reads_as_a_cheque(self, app, client, campaign_id):
        """A cheque number and a bank name make a more useful reference
        than a bare id, so the cheque branch keeps priority."""
        from models import Donation
        login(client)
        _log_donation(client, campaign_id, payment_mode="cheque",
                      cheque_number="123456", cheque_bank_name="HDFC Bank",
                      bank_transaction_id="")
        with app.app_context():
            assert Donation.query.one().reference_display == "Cheque #123456 (HDFC Bank)"

    def test_cash_has_no_reference(self, app, client, campaign_id, monkeypatch):
        from models import Donation
        drawn = _receipt_field_values(monkeypatch)
        login(client)
        _log_donation(client, campaign_id, payment_mode="cash", bank_transaction_id="")
        with app.app_context():
            assert Donation.query.one().reference_display is None
        assert drawn.get(PAYMENT_DETAILS_LABEL) == "-"

    def test_a_real_gateway_payment_id_wins(self, app, campaign_id):
        """An online donation paid through the site has the gateway's own
        id; nothing typed by hand should displace it."""
        from extensions import db
        from models import Donation, Donor
        with app.app_context():
            donor = Donor(full_name="Ravi Sharma", phone="9876543210")
            db.session.add(donor)
            db.session.flush()
            donation = Donation(
                donor_id=donor.id, campaign_id=campaign_id, amount=1100,
                payment_mode="online", status="success",
                razorpay_payment_id="pay_REALGATEWAYID",
                bank_transaction_id="typed-in-by-hand")
            db.session.add(donation)
            db.session.commit()
            assert donation.reference_display == "pay_REALGATEWAYID"


class TestEveryFormThatWritesTheField:
    """reference_display is shared, so every writer of bank_transaction_id
    has to come back out of it -- including the ones nobody reported."""

    def test_bulk_csv_import_online_row(self, app, client, campaign_id):
        from models import Donation
        login(client)
        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date,bank_transaction_id\n"
            f"Ravi Sharma,Annadan,1100,online,2026-08-01,{TXN}\n"
        )
        resp = client.post(
            "/admin/donations/bulk-import",
            data={"csv_file": (io.BytesIO(csv_text.encode()), "donations.csv")},
            content_type="multipart/form-data", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert Donation.query.one().reference_display == TXN

    def test_iyf_camp_entry_paid_online(self, app, client):
        from models import Donation
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Utkarsha 2026"},
                    follow_redirects=True)
        client.post("/admin/iyf-camps/single", data={
            "full_name": "Anita Verma", "phone": "9812345678", "amount": "2100",
            "camp_name": "Utkarsha 2026", "batch_name": "Batch B",
            "payment_mode": "online", "bank_transaction_id": TXN,
            "donation_date": "2026-08-01",
        }, follow_redirects=True)
        with app.app_context():
            assert Donation.query.one().reference_display == TXN

    def test_iyf_camp_entry_paid_by_cheque(self, app, client):
        """The camp form shows its reference field for Cheque but collects
        no cheque number, so the reference arrives as bank_transaction_id
        on a payment_mode of "cheque" -- a combination an allowlist of
        modes would have dropped."""
        from models import Donation
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Utkarsha 2026"},
                    follow_redirects=True)
        client.post("/admin/iyf-camps/single", data={
            "full_name": "Anita Verma", "phone": "9812345678", "amount": "2100",
            "camp_name": "Utkarsha 2026", "batch_name": "Batch B",
            "payment_mode": "cheque", "bank_transaction_id": "CHQ-889900",
            "donation_date": "2026-08-01",
        }, follow_redirects=True)
        with app.app_context():
            assert Donation.query.one().reference_display == "CHQ-889900"

    def test_it_reaches_the_camp_detail_export(self, app, client):
        """The camp detail CSV exists to be reconciled against Zoho, which
        is exactly the column that was blank."""
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Utkarsha 2026"},
                    follow_redirects=True)
        client.post("/admin/iyf-camps/single", data={
            "full_name": "Anita Verma", "phone": "9812345678", "amount": "2100",
            "camp_name": "Utkarsha 2026", "batch_name": "Batch B",
            "payment_mode": "online", "bank_transaction_id": TXN,
            "donation_date": "2026-08-01",
        }, follow_redirects=True)
        resp = client.get("/admin/iyf-camps/export/detail.csv?range=all")
        assert resp.status_code == 200
        assert TXN in resp.data.decode()
