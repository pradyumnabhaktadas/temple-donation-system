import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


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

        resp = client.get(f"/receipt/{donation_id}")
        assert resp.status_code == 200
        assert resp.mimetype == "application/pdf"
        assert resp.data.startswith(b"%PDF")
        # Served bytes should be exactly what's stored, not regenerated.
        donation = Donation.query.get(donation_id)
        assert resp.data == donation.receipt_pdf

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

        resp = client.get(f"/receipt/{donation.id}")
        assert resp.status_code == 200
        assert resp.data == b"%PDF-1.4 legacy on-disk receipt"

    def test_download_receipt_missing_everywhere_shows_friendly_message(self, app, client, tmp_path, monkeypatch):
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

        resp = client.get(f"/receipt/{donation.id}", follow_redirects=True)
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
