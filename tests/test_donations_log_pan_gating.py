"""Follow-up to REG-034 (user-reported, 2026-08-21): the "Full details"
modal on the admin Donations Log (templates/admin/donations.html)
unconditionally showed PAN/Address/City-State-Pincode for every donation,
regardless of whether that donation was ever 80G-eligible or high-value
enough to legally require collecting them. Once a donor's PAN is on file
(collected for one donation that needed it), every other donation of
theirs -- even a small BACE Contribution top-up -- exposed it to any
staff member browsing the log.

This mirrors the exact same gate already applied to the receipt PDF
itself (tests/test_receipt_pan_gating.py): show PAN/Address only when
Donation.effective_is_80g is true or the amount crosses
utils.HIGH_VALUE_PAN_THRESHOLD.
"""
from conftest import login


def _make_donation(app, campaign_name, amount, donor_pan="ABCDE1234F", donor_address="12 Test Street"):
    from extensions import db
    from models import Campaign, Donor, Donation
    with app.app_context():
        campaign = Campaign.query.filter_by(name=campaign_name).first()
        donor = Donor(full_name="Gating Test Donor", phone="9177008899",
                       pan=donor_pan, address=donor_address)
        db.session.add(donor)
        db.session.commit()
        donation = Donation(
            donor_id=donor.id, campaign_id=campaign.id, amount=amount, payment_mode="cash",
            status="success", recorded_by="testadmin", receipt_number=f"TEST/{amount}",
        )
        db.session.add(donation)
        db.session.commit()
        return donation.id


class TestDonationsLogPanGating:
    def test_non_80g_low_value_donation_hides_pan_and_address_rows(self, app, client):
        login(client)
        did = _make_donation(app, "BACE Contribution", amount=501)
        html = client.get("/admin/donations?status=all&range=all").data.decode()

        modal_html = html[html.index(f'id="donationDetails{did}"'):][:4000]

        assert "ABCDE1234F" not in modal_html
        assert "<th class=\"text-muted\">PAN</th>" not in modal_html
        assert "<th class=\"text-muted\">Address</th>" not in modal_html

    def test_non_80g_high_value_donation_shows_pan_and_address_rows(self, app, client):
        login(client)
        did = _make_donation(app, "BACE Contribution", amount=50001)
        html = client.get("/admin/donations?status=all&range=all").data.decode()

        modal_html = html[html.index(f'id="donationDetails{did}"'):][:4000]
        assert "ABCDE1234F" in modal_html
        assert "<th class=\"text-muted\">PAN</th>" in modal_html
        assert "<th class=\"text-muted\">Address</th>" in modal_html

    def test_80g_campaign_donation_always_shows_pan_and_address_rows(self, app, client):
        """Annadan is 80G-eligible regardless of amount -- unchanged from
        before this fix."""
        login(client)
        did = _make_donation(app, "Annadan", amount=101)
        html = client.get("/admin/donations?status=all&range=all").data.decode()

        modal_html = html[html.index(f'id="donationDetails{did}"'):][:4000]
        assert "ABCDE1234F" in modal_html
        assert "<th class=\"text-muted\">PAN</th>" in modal_html
