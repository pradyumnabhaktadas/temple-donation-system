"""Integration tests for the Dhoti Kurta Contribution feature.

Requested as a small, discreet footer-only contribution link: Name/Mobile/
Amount only, no receipt ever issued, tracked separately from the regular
Donations Log in its own admin section. These drive the real routes
through Flask's test client end to end -- the footer link's placement, the
minimal public form, the online (create_order + simulate-payment) and
offline (manual entry + bulk import) donation paths, the dedicated admin
list/search/filter/export, the success-page rendering, and the
suppress_receipt checkbox on Campaign edit -- rather than checking the
code reads correctly. See Campaign.suppress_receipt (models.py),
public._finalize_success, and admin._create_offline_donation for the
shared mechanism every one of these paths goes through.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _mk_campaign(db):
    from models import Campaign
    campaign = Campaign.query.filter_by(name="Dhoti Kurta Contribution").first()
    if campaign:
        return campaign
    campaign = Campaign(name="Dhoti Kurta Contribution", is_80g=False, is_active=True, suppress_receipt=True)
    db.session.add(campaign)
    db.session.commit()
    return campaign


class TestFooterLink:
    def test_link_appears_in_footer_and_points_to_the_form(self, app, client):
        html = client.get("/").data.decode()
        assert "footer-admin-link" in html
        assert '/dhoti-kurta-contribution">Dhoti Kurta Contribution</a>' in html

    def test_link_is_not_in_main_nav_or_prominent_areas(self, app, client):
        """Section 1's whole point: reachable only via the small footer
        link, never the main nav, homepage hero, or a regular donation
        section."""
        html = client.get("/").data.decode()
        nav_start = html.index('id="siteNavLinks"')
        nav_end = html.index("</nav>", nav_start)
        assert "Dhoti Kurta" not in html[nav_start:nav_end]

        donation_card_start = html.index('id="donation-card"') if 'id="donation-card"' in html else None
        if donation_card_start is not None:
            donation_card_end = html.index("</section>", donation_card_start)
            assert "Dhoti Kurta" not in html[donation_card_start:donation_card_end]

    def test_link_appears_on_other_public_pages_too(self, app, client):
        """The footer is shared across every public page via base.html --
        spot-check one more page to make sure it's not only on the
        homepage."""
        html = client.get("/about").data.decode()
        assert "footer-admin-link" in html


class TestContributionForm:
    def test_form_has_exactly_the_three_fields_plus_submit(self, app, client):
        from extensions import db
        _mk_campaign(db)
        html = client.get("/dhoti-kurta-contribution").data.decode()
        form_start = html.index('id="dhoti-kurta-form"')
        form_end = html.index("</form>", form_start)
        form_html = html[form_start:form_end]

        assert 'name="full_name"' in form_html
        assert 'name="phone"' in form_html
        assert 'name="amount"' in form_html
        assert 'type="submit"' in form_html

        # No PAN, address, email, remarks, or visible consent checkbox --
        # the user explicitly chose "skip it entirely" for consent.
        assert 'name="pan"' not in form_html
        assert 'name="address"' not in form_html
        assert 'name="email"' not in form_html
        assert 'name="remarks"' not in form_html
        assert 'type="checkbox"' not in form_html
        # consent is still sent (create_order requires it) but as a hidden
        # field, never shown to the contributor.
        assert 'name="consent" value="on"' in form_html
        assert 'type="hidden"' in form_html

    def test_form_redirects_to_main_donate_page_if_campaign_missing(self, app, client):
        """A fresh install that hasn't run seed.py/the migration yet --
        must not 500."""
        resp = client.get("/dhoti-kurta-contribution", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Dhoti Kurta Contribution campaign isn" in resp.data or b"contact the office" in resp.data


class TestOnlineDonationFlow:
    def test_successful_contribution_gets_no_receipt(self, app, client):
        from extensions import db
        from models import Donation
        campaign = _mk_campaign(db)

        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Dhoti Donor", "phone": "9876500201",
            "consent": "on", "campaign_id": campaign.id,
        })
        assert resp.status_code == 200
        donation_id = resp.get_json()["donation_id"]

        sim = client.post("/api/simulate-payment", json={"donation_id": donation_id})
        assert sim.status_code == 200
        assert sim.get_json()["receipt_number"] is None

        donation = Donation.query.get(donation_id)
        assert donation.status == "success"
        assert donation.receipt_number is None
        assert donation.receipt_pdf is None
        assert donation.campaign_id == campaign.id
        # Donation Purpose = "General Donation": no purpose sub-picker
        # applies to this campaign, so specific_purpose is blank -- the
        # codebase's own definition of a plain General Donation.
        assert donation.specific_purpose == ""

    def test_amount_above_high_value_threshold_is_rejected(self, app, client):
        """The form has no PAN/address fields, so the existing
        legally-mandated high-value check (amount > Rs 49,000 needs a PAN
        or address on file) blocks it server-side -- the form's own amount
        cap is just the client-side half of this."""
        from extensions import db
        from models import Donation
        campaign = _mk_campaign(db)

        resp = client.post("/api/create-order", json={
            "amount": 50000, "full_name": "Big Dhoti Donor", "phone": "9876500202",
            "consent": "on", "campaign_id": campaign.id,
        })
        assert resp.status_code == 400
        assert Donation.query.count() == 0

    def test_amount_at_cap_is_accepted(self, app, client):
        from extensions import db
        campaign = _mk_campaign(db)
        resp = client.post("/api/create-order", json={
            "amount": 49000, "full_name": "Cap Donor", "phone": "9876500203",
            "consent": "on", "campaign_id": campaign.id,
        })
        assert resp.status_code == 200

    def test_missing_consent_is_rejected(self, app, client):
        """The hidden field is what makes the minimal form work at all --
        confirm the shared validation really does require it (i.e. this
        isn't accidentally bypassed for this campaign)."""
        from extensions import db
        from models import Donation
        campaign = _mk_campaign(db)
        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "No Consent Donor", "phone": "9876500204",
            "campaign_id": campaign.id,
        })
        assert resp.status_code == 400
        assert Donation.query.count() == 0


class TestOfflineSingleEntry:
    def test_manual_entry_gets_no_receipt(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        campaign = _mk_campaign(db)

        resp = client.post("/admin/donations/manual", data={
            "campaign_id": campaign.id, "amount": "501", "full_name": "Offline Dhoti Donor",
            "phone": "9876500301", "payment_mode": "cash",
        }, follow_redirects=True)

        donation = Donation.query.one()
        assert donation.receipt_number is None
        assert donation.status == "success"
        assert b"No receipt is issued for this contribution type." in resp.data


class TestOfflineBulkImport:
    def test_bulk_import_row_gets_no_receipt(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        _mk_campaign(db)

        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Bulk Dhoti Donor,Dhoti Kurta Contribution,501,cash,2026-04-01\n"
        )
        client.post("/admin/donations/bulk-import", data={
            "csv_file": (io.BytesIO(csv_text.encode()), "import.csv"),
            "action": "import",
        }, content_type="multipart/form-data", follow_redirects=True)

        donation = Donation.query.one()
        assert donation.receipt_number is None
        assert donation.status == "success"

    def test_bulk_import_preview_does_not_create_anything(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        _mk_campaign(db)

        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Preview Dhoti Donor,Dhoti Kurta Contribution,501,cash,2026-04-01\n"
        )
        client.post("/admin/donations/bulk-import", data={
            "csv_file": (io.BytesIO(csv_text.encode()), "import.csv"),
            "action": "preview",
        }, content_type="multipart/form-data", follow_redirects=True)
        assert Donation.query.count() == 0


class TestAdminSection:
    def _seed(self, db, dk_campaign):
        from models import Campaign, Donor, Donation
        regular = Campaign.query.filter_by(name="Annadan").first()
        donor_a = Donor(full_name="Dhoti Alpha", phone="9876500401")
        donor_b = Donor(full_name="Dhoti Beta", phone="9876500402")
        db.session.add_all([donor_a, donor_b])
        db.session.flush()
        db.session.add_all([
            Donation(donor_id=donor_a.id, campaign_id=dk_campaign.id, amount=201,
                      payment_mode="cash", status="success"),
            Donation(donor_id=donor_b.id, campaign_id=dk_campaign.id, amount=302,
                      payment_mode="cash", status="success"),
            # A regular donation against a different campaign -- must never
            # show up in this dedicated view.
            Donation(donor_id=donor_a.id, campaign_id=regular.id, amount=999,
                      payment_mode="cash", status="success"),
        ])
        db.session.commit()
        return donor_a, donor_b

    def test_list_excludes_regular_donations(self, app, client):
        from extensions import db
        login(client)
        campaign = _mk_campaign(db)
        self._seed(db, campaign)

        html = client.get("/admin/dhoti-kurta-contributions?range=all").data.decode()
        assert "Dhoti Alpha" in html
        assert "Dhoti Beta" in html
        assert "Rs. 999" not in html

    def test_search_narrows_by_name(self, app, client):
        from extensions import db
        login(client)
        campaign = _mk_campaign(db)
        self._seed(db, campaign)

        html = client.get("/admin/dhoti-kurta-contributions?range=all&q=Alpha").data.decode()
        assert "Dhoti Alpha" in html
        assert "Dhoti Beta" not in html

    def test_search_narrows_by_phone(self, app, client):
        from extensions import db
        login(client)
        campaign = _mk_campaign(db)
        self._seed(db, campaign)

        html = client.get("/admin/dhoti-kurta-contributions?range=all&q=9876500402").data.decode()
        assert "Dhoti Beta" in html
        assert "Dhoti Alpha" not in html

    def test_status_filter(self, app, client):
        from extensions import db
        from models import Donor, Donation
        login(client)
        campaign = _mk_campaign(db)
        donor = Donor(full_name="Pending Dhoti Donor", phone="9876500403")
        db.session.add(donor)
        db.session.flush()
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign.id, amount=150,
                                 payment_mode="online", status="pending"))
        db.session.commit()

        html = client.get("/admin/dhoti-kurta-contributions?range=all&status=success").data.decode()
        assert "Pending Dhoti Donor" not in html

        html = client.get("/admin/dhoti-kurta-contributions?range=all&status=pending").data.decode()
        assert "Pending Dhoti Donor" in html

    def test_page_renders_when_campaign_not_configured(self, app, client):
        login(client)
        resp = client.get("/admin/dhoti-kurta-contributions")
        assert resp.status_code == 200
        assert b"isn&#39;t set up yet" in resp.data or b"isn't set up yet" in resp.data

    def test_csv_export_has_the_six_required_columns_and_only_dk_rows(self, app, client):
        from extensions import db
        login(client)
        campaign = _mk_campaign(db)
        self._seed(db, campaign)

        resp = client.get("/admin/dhoti-kurta-contributions/export?range=all")
        body = resp.data.decode()
        header = body.splitlines()[0]
        for col in ["Name", "Mobile Number", "Amount", "Date & Time", "Transaction Status",
                    "Transaction ID / Payment Reference"]:
            assert col in header
        assert "Dhoti Alpha" in body
        assert "Dhoti Beta" in body
        assert "999" not in body

    def test_csv_export_reference_column_shows_online_payment_id(self, app, client):
        from extensions import db
        from models import Donation
        campaign = _mk_campaign(db)
        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Ref Dhoti Donor", "phone": "9876500404",
            "consent": "on", "campaign_id": campaign.id,
        })
        donation_id = resp.get_json()["donation_id"]
        client.post("/api/simulate-payment", json={"donation_id": donation_id})

        login(client)
        body = client.get("/admin/dhoti-kurta-contributions/export?range=all").data.decode()
        assert "SIMULATED" in body


class TestDonateSuccessPage:
    def test_renders_without_a_broken_receipt_link(self, app, client):
        from extensions import db
        campaign = _mk_campaign(db)
        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Success Page Donor", "phone": "9876500501",
            "consent": "on", "campaign_id": campaign.id,
        })
        donation_id = resp.get_json()["donation_id"]
        client.post("/api/simulate-payment", json={"donation_id": donation_id})

        login(client)
        page = client.get(f"/donate/success/{donation_id}")
        assert page.status_code == 200
        html = page.data.decode()
        assert "No receipt is issued for this contribution." in html
        assert "Download receipt" not in html
        assert "Receipt No:" not in html


class TestCampaignEditSuppressReceipt:
    def test_checkbox_persists_on(self, app, client):
        from extensions import db
        from models import Campaign
        login(client)
        campaign = Campaign(name="New Test Campaign", is_80g=True, suppress_receipt=False)
        db.session.add(campaign)
        db.session.commit()

        client.post(f"/admin/campaigns/{campaign.id}/edit", data={
            "name": "New Test Campaign", "is_80g": "on", "suppress_receipt": "on",
        }, follow_redirects=True)
        assert Campaign.query.get(campaign.id).suppress_receipt is True

    def test_checkbox_persists_off_when_unchecked(self, app, client):
        from extensions import db
        from models import Campaign
        login(client)
        campaign = Campaign(name="Another Test Campaign", is_80g=True, suppress_receipt=True)
        db.session.add(campaign)
        db.session.commit()

        client.post(f"/admin/campaigns/{campaign.id}/edit", data={
            "name": "Another Test Campaign", "is_80g": "on",
        }, follow_redirects=True)
        assert Campaign.query.get(campaign.id).suppress_receipt is False
