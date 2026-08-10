import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import receipt_access_token


def _receipt_url(app, donation_id):
    """/receipt/<id> is gated by a signed token (see
    utils.receipt_access_token) so the route can't be walked to harvest
    donors' names, addresses and PANs. Tests build the same URL a
    template or a WhatsApp message would."""
    token = receipt_access_token(donation_id, app.config["SECRET_KEY"])
    return f"/receipt/{donation_id}?t={token}"


class TestReceiptStorage:
    """Receipts used to be written to local disk (instance/receipts/) and
    served from there -- broken on any host without a persistent filesystem,
    and not covered by database backups. They now live as bytes on
    Donation.receipt_pdf, generated once at issuance and never regenerated,
    so a receipt stays byte-for-byte what the donor actually got even if the
    org's template/address/logo changes later."""

    def test_online_donation_stores_receipt_pdf_bytes(self, app, client):
        from models import Campaign, Donation

        campaign = Campaign.query.filter_by(name="Annadan").first()
        order_resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id, "amount": 251, "full_name": "Storage Test Donor",
                "phone": "9199999999", "consent": "on",
            },
        )
        donation_id = order_resp.get_json()["donation_id"]
        client.post("/api/simulate-payment", json={"donation_id": donation_id})

        donation = Donation.query.get(donation_id)
        assert donation.receipt_pdf is not None
        assert donation.receipt_pdf.startswith(b"%PDF")

    def test_download_receipt_serves_from_database(self, app, client):
        from models import Campaign, Donation

        campaign = Campaign.query.filter_by(name="Annadan").first()
        order_resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id, "amount": 251, "full_name": "Download Test Donor",
                "phone": "9188888888", "consent": "on",
            },
        )
        donation_id = order_resp.get_json()["donation_id"]
        client.post("/api/simulate-payment", json={"donation_id": donation_id})

        resp = client.get(_receipt_url(app, donation_id))
        assert resp.status_code == 200
        assert resp.mimetype == "application/pdf"
        assert resp.data.startswith(b"%PDF")
        # Served bytes should be exactly what's stored, not regenerated.
        donation = Donation.query.get(donation_id)
        assert resp.data == donation.receipt_pdf


class TestReceiptAccessControl:
    """Receipt PDFs carry the donor's full name, address, PAN, email and
    phone, and donation ids are sequential -- so before this the route
    could be walked (/receipt/1, /receipt/2, ...) to harvest every donor's
    personal details from an unauthenticated endpoint.

    It can't just be put behind a login: WhatsApp delivery hands Airtel a
    public URL to fetch the PDF from, and donors need their receipt
    immediately after paying, before any account exists. Hence a signed
    token in the URL (utils.receipt_access_token)."""

    def _paid_donation_id(self, client):
        from models import Campaign

        campaign = Campaign.query.filter_by(name="Annadan").first()
        order_resp = client.post(
            "/api/create-order",
            json={
                "campaign_id": campaign.id, "amount": 300, "full_name": "Private Donor",
                "phone": "9155555555", "consent": "on",
            },
        )
        donation_id = order_resp.get_json()["donation_id"]
        client.post("/api/simulate-payment", json={"donation_id": donation_id})
        return donation_id

    def test_valid_token_is_allowed(self, app, client):
        donation_id = self._paid_donation_id(client)
        assert client.get(_receipt_url(app, donation_id)).status_code == 200

    def test_no_token_is_refused(self, app, client):
        donation_id = self._paid_donation_id(client)
        assert client.get(f"/receipt/{donation_id}").status_code == 404

    def test_another_donations_token_is_refused(self, app, client):
        """The token is bound to a specific donation, so holding a
        legitimate link to your own receipt doesn't unlock anyone else's."""
        donation_id = self._paid_donation_id(client)
        wrong = receipt_access_token(donation_id + 1000, app.config["SECRET_KEY"])
        assert client.get(f"/receipt/{donation_id}?t={wrong}").status_code == 404

    def test_refusal_is_indistinguishable_from_a_missing_donation(self, app, client):
        """Same 404 either way -- telling the two apart would confirm which
        ids exist, which is most of what an enumeration attempt wants."""
        donation_id = self._paid_donation_id(client)
        real_no_token = client.get(f"/receipt/{donation_id}")
        nonexistent = client.get("/receipt/99999999")
        assert real_no_token.status_code == nonexistent.status_code == 404

    def test_download_receipt_falls_back_to_legacy_disk_path(self, app, client, tmp_path, monkeypatch):
        """Simulates a donation issued before the DB-storage migration --
        receipt_pdf is NULL, but a file still exists at the old on-disk
        location. Should serve that instead of 404ing or erroring."""
        from extensions import db
        from models import Campaign, Donation
        from public import find_or_create_donor
        import pdf_utils

        campaign = Campaign.query.filter_by(name="Annadan").first()
        donor = find_or_create_donor({"full_name": "Legacy Donor", "phone": "9177777777"})
        donation = Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=100, payment_mode="online",
            status="success", recorded_by="online", receipt_number="032511/ISK999999",
            financial_year="2026-27", receipt_pdf=None,
        )
        db.session.add(donation)
        db.session.commit()

        # Point RECEIPTS_DIR at a temp dir and drop a fake legacy PDF there.
        monkeypatch.setattr(pdf_utils, "RECEIPTS_DIR", str(tmp_path))
        legacy_path = pdf_utils.receipt_pdf_path(donation.receipt_number)
        with open(legacy_path, "wb") as f:
            f.write(b"%PDF-1.4 legacy on-disk receipt")

        resp = client.get(_receipt_url(app, donation.id))
        assert resp.status_code == 200
        assert resp.data == b"%PDF-1.4 legacy on-disk receipt"

    def _donation_with_no_stored_pdf(self, tmp_path, monkeypatch):
        """A donation that genuinely succeeded and holds a receipt number,
        but has no stored PDF and no legacy file on disk. Reachable in
        production because _finalize_success() treats PDF generation as
        best-effort -- a PDF failure must never cost a donor the receipt
        number that was already committed."""
        from extensions import db
        from models import Campaign, Donation
        from public import find_or_create_donor
        import pdf_utils

        campaign = Campaign.query.filter_by(name="Annadan").first()
        donor = find_or_create_donor({"full_name": "Nowhere Donor", "phone": "9166666666"})
        donation = Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=100, payment_mode="online",
            status="success", recorded_by="online", receipt_number="032511/ISK888888",
            financial_year="2026-27", receipt_pdf=None,
        )
        db.session.add(donation)
        db.session.commit()
        monkeypatch.setattr(pdf_utils, "RECEIPTS_DIR", str(tmp_path))
        return donation

    def test_download_receipt_regenerates_a_missing_pdf(self, app, client, tmp_path, monkeypatch):
        """This used to be a dead end -- the donor was told to contact the
        office about a receipt they were entitled to and that we held all
        the data for. Nothing about the receipt depends on when it's
        rendered (the receipt number was fixed at finalization, everything
        else comes from stored donation/donor/campaign rows), so it's now
        built on demand and kept for next time."""
        from extensions import db
        from models import Donation

        donation = self._donation_with_no_stored_pdf(tmp_path, monkeypatch)
        donation_id = donation.id

        resp = client.get(_receipt_url(app, donation_id))
        assert resp.status_code == 200
        assert resp.mimetype == "application/pdf"
        assert resp.data.startswith(b"%PDF")

        # And it was persisted, so the next download doesn't rebuild it.
        db.session.expire_all()
        assert Donation.query.get(donation_id).receipt_pdf == resp.data

    def test_download_receipt_friendly_message_if_regeneration_also_fails(
        self, app, client, tmp_path, monkeypatch
    ):
        """Last resort: no stored PDF, no legacy file, and rebuilding it
        raises too. The donor gets the friendly message rather than a 500."""
        import public

        donation = self._donation_with_no_stored_pdf(tmp_path, monkeypatch)

        def _boom(*args, **kwargs):
            raise RuntimeError("PDF engine unavailable")

        monkeypatch.setattr(public, "generate_receipt_pdf", _boom)

        resp = client.get(_receipt_url(app, donation.id), follow_redirects=True)
        assert resp.status_code == 200
        assert b"regenerated" in resp.data

    def test_manual_donation_stores_receipt_pdf_bytes(self, app, client):
        from models import Campaign, Donor, Donation

        campaign = Campaign.query.filter_by(name="Annadan").first()
        client.post("/admin/login", data={"username": "testadmin", "password": "TestPass123!"}, follow_redirects=True)

        client.post(
            "/admin/donations/manual",
            data={
                "campaign_id": campaign.id, "amount": "500", "payment_mode": "cash",
                "full_name": "Manual Storage Donor", "phone": "9155555555", "donation_date": "2026-07-29",
            },
            follow_redirects=True,
        )

        donor = Donor.query.filter_by(phone="9155555555").first()
        donation = Donation.query.filter_by(donor_id=donor.id).first()
        assert donation.receipt_pdf is not None
        assert donation.receipt_pdf.startswith(b"%PDF")
