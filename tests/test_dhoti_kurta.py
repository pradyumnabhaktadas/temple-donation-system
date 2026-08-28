"""Integration tests for the Dhoti Kurta Contribution feature.

Requested as a small, discreet footer-only contribution link: Name/Mobile/
Amount only, tracked separately from the regular Donations Log in its own
admin section. A real receipt number and PDF are generated for every
contribution exactly like any other donation (for internal accounting) --
what's suppressed is only the *proactive* email/WhatsApp send. The donor
can still see/download their own receipt the normal way (the success
page, the donor portal). See Campaign.suppress_receipt's docstring in
models.py, public._finalize_success, and admin._create_offline_donation
for the shared mechanism every donation path goes through.

These drive the real routes through Flask's test client end to end -- the
footer link's placement, the minimal public form, the online
(create_order + simulate-payment) and offline (manual entry + bulk
import) donation paths, the dedicated admin list/search/filter/export,
the success-page rendering, and the suppress_receipt checkbox on Campaign
edit -- rather than checking the code reads correctly.
"""
import io
import os
import sys
from unittest.mock import patch

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
        # Lives in the footer's "Links" column now (footer-links-list),
        # not the old bottom-strip "footer-admin-link" note.
        assert "footer-links-list" in html
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
        assert "footer-links-list" in html


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
    def test_successful_contribution_gets_a_receipt_with_no_notification_sent(self, app, client):
        """The core of the current design: a real receipt number and PDF
        are issued exactly like any other donation (for internal
        accounting), but the email/WhatsApp send that every other
        donation triggers must never fire for this campaign."""
        from extensions import db
        from models import Donation
        campaign = _mk_campaign(db)

        with patch("public.send_receipt_email") as mock_email, \
             patch("public.send_receipt_whatsapp") as mock_whatsapp:
            resp = client.post("/api/create-order", json={
                "amount": 501, "full_name": "Dhoti Donor", "phone": "9876500201",
                "consent": "on", "campaign_id": campaign.id,
            })
            assert resp.status_code == 200
            donation_id = resp.get_json()["donation_id"]

            sim = client.post("/api/simulate-payment", json={"donation_id": donation_id})
            assert sim.status_code == 200
            assert sim.get_json()["receipt_number"] is not None

            mock_email.assert_not_called()
            mock_whatsapp.assert_not_called()

        donation = Donation.query.get(donation_id)
        assert donation.status == "success"
        assert donation.receipt_number is not None
        assert donation.receipt_pdf is not None
        assert donation.campaign_id == campaign.id
        # Donation Purpose = "General Donation": no purpose sub-picker
        # applies to this campaign, so specific_purpose is blank -- the
        # codebase's own definition of a plain General Donation.
        assert donation.specific_purpose == ""

    def test_a_normal_campaigns_donation_does_attempt_notifications(self, app, client):
        """Contrast case: proves the mocks above would actually have
        caught a regression -- a normal (non-suppress_receipt) campaign's
        successful donation does try to send, even though it's a no-op in
        this test environment (no SMTP/WhatsApp configured). Uses BACE
        Contribution (is_80g=False in conftest) rather than Annadan, to
        avoid the separate PAN-required-for-80G validation getting in the
        way of what this test is actually checking."""
        from models import Campaign
        campaign = Campaign.query.filter_by(name="BACE Contribution").first()

        with patch("public.send_receipt_email") as mock_email, \
             patch("public.send_receipt_whatsapp") as mock_whatsapp:
            resp = client.post("/api/create-order", json={
                "amount": 501, "full_name": "Normal Donor", "phone": "9876500299",
                "consent": "on", "campaign_id": campaign.id,
            })
            donation_id = resp.get_json()["donation_id"]
            client.post("/api/simulate-payment", json={"donation_id": donation_id})

            mock_email.assert_called_once()
            mock_whatsapp.assert_called_once()

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
    def test_manual_entry_gets_a_receipt_with_no_notification_sent(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        campaign = _mk_campaign(db)

        # _create_offline_donation (admin.py) reuses public.py's
        # _send_receipt_notifications_background, which calls
        # send_receipt_email/send_receipt_whatsapp by their names as
        # resolved in public.py's own module namespace regardless of who
        # imported the background-sender function -- so that's what has
        # to be patched here, not admin.send_receipt_email (which doesn't
        # exist; admin.py never imports those two directly).
        with patch("public.send_receipt_email") as mock_email, \
             patch("public.send_receipt_whatsapp") as mock_whatsapp:
            resp = client.post("/admin/donations/manual", data={
                "campaign_id": campaign.id, "amount": "501", "full_name": "Offline Dhoti Donor",
                "phone": "9876500301", "payment_mode": "cash",
            }, follow_redirects=True)
            mock_email.assert_not_called()
            mock_whatsapp.assert_not_called()

        donation = Donation.query.one()
        assert donation.receipt_number is not None
        assert donation.receipt_pdf is not None
        assert donation.status == "success"
        assert b"generated for internal" in resp.data
        assert b"not sent to the contributor" in resp.data


class TestOfflineBulkImport:
    def test_bulk_import_row_gets_a_receipt_with_no_notification_sent(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        _mk_campaign(db)

        csv_text = (
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Bulk Dhoti Donor,Dhoti Kurta Contribution,501,cash,2026-04-01\n"
        )
        with patch("public.send_receipt_email") as mock_email, \
             patch("public.send_receipt_whatsapp") as mock_whatsapp:
            client.post("/admin/donations/bulk-import", data={
                "csv_file": (io.BytesIO(csv_text.encode()), "import.csv"),
                "action": "import",
            }, content_type="multipart/form-data", follow_redirects=True)
            mock_email.assert_not_called()
            mock_whatsapp.assert_not_called()

        donation = Donation.query.one()
        assert donation.receipt_number is not None
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

    def test_csv_export_has_the_required_columns_and_only_dk_rows(self, app, client):
        from extensions import db
        login(client)
        campaign = _mk_campaign(db)
        self._seed(db, campaign)

        resp = client.get("/admin/dhoti-kurta-contributions/export?range=all")
        body = resp.data.decode()
        header = body.splitlines()[0]
        # The 6 columns spec Section 4 asked for, plus Receipt No. -- added
        # once receipts started being generated for internal accounting,
        # which is the whole point of issuing one at all here.
        for col in ["Name", "Mobile Number", "Amount", "Date & Time", "Transaction Status",
                    "Transaction ID / Payment Reference", "Receipt No."]:
            assert col in header
        assert "Dhoti Alpha" in body
        assert "Dhoti Beta" in body
        assert "999" not in body

    def test_csv_export_includes_the_receipt_number(self, app, client):
        """Uses the real online create_order + simulate-payment flow
        (unlike _seed(), which inserts Donation rows directly and so
        never exercises receipt issuance) -- this is the path that
        actually generates a receipt_number, which is exactly what this
        test needs to be checking is present in the export."""
        from extensions import db
        from models import Donation
        campaign = _mk_campaign(db)
        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Receipt Column Donor", "phone": "9876500405",
            "consent": "on", "campaign_id": campaign.id,
        })
        donation_id = resp.get_json()["donation_id"]
        client.post("/api/simulate-payment", json={"donation_id": donation_id})
        receipt_number = Donation.query.get(donation_id).receipt_number
        assert receipt_number is not None

        login(client)
        body = client.get("/admin/dhoti-kurta-contributions/export?range=all").data.decode()
        assert receipt_number in body

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
    def test_owner_view_shows_the_receipt_and_a_working_download_link(self, app, client):
        """The donor themselves, on their own success page right after
        paying, can see and download their receipt exactly like any other
        donation -- suppress_receipt only gates the proactive email/
        WhatsApp send, not this."""
        from extensions import db
        from models import Donation
        campaign = _mk_campaign(db)
        resp = client.post("/api/create-order", json={
            "amount": 501, "full_name": "Success Page Donor", "phone": "9876500501",
            "consent": "on", "campaign_id": campaign.id,
        })
        donation_id = resp.get_json()["donation_id"]
        client.post("/api/simulate-payment", json={"donation_id": donation_id})
        receipt_number = Donation.query.get(donation_id).receipt_number
        assert receipt_number is not None

        login(client)
        page = client.get(f"/donate/success/{donation_id}")
        assert page.status_code == 200
        html = page.data.decode()
        assert f"Receipt No: <strong>{receipt_number}</strong>" in html
        assert "Download receipt" in html

        download = client.get(f"/receipt/{donation_id}")
        assert download.status_code == 200
        assert download.mimetype == "application/pdf"


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


class TestCampaignsListBadge:
    def test_receipt_not_sent_badge_shown_only_for_suppressed_campaigns(self, app, client):
        """Admin-clarity gap found on recheck: nothing on the Campaigns
        list previously distinguished a suppress_receipt campaign from a
        normal one, even though the dedicated Edit page already has the
        checkbox -- an admin scanning the list had no way to tell why a
        campaign's receipts never reach the donor."""
        from extensions import db
        login(client)
        _mk_campaign(db)

        html = client.get("/admin/campaigns").data.decode()

        # The admin nav also has a "Dhoti Kurta Contributions" (plural)
        # link, so search only within the campaign cards themselves,
        # after the page's own heading.
        list_start = html.index("Campaigns / Collection Categories")

        dk_start = html.index("Dhoti Kurta Contribution", list_start)
        assert "Receipt Not Sent" in html[dk_start:dk_start + 600]

        annadan_start = html.index("Annadan", list_start)
        assert "Receipt Not Sent" not in html[annadan_start:annadan_start + 600]


class TestGenerateMissingReceipt:
    """Backfill for the two real Dhoti Kurta contributions already
    recorded in production under the old "no receipt at all" behavior --
    admin.generate_missing_receipt() lets an admin issue a receipt for a
    successful donation that predates this change, without triggering the
    email/WhatsApp send a fresh contribution would (still) never get
    either."""

    def _mk_receiptless_donation(self, db, campaign, full_name="Backfill Donor", phone="9876500601"):
        from models import Donor, Donation
        donor = Donor(full_name=full_name, phone=phone)
        db.session.add(donor)
        db.session.flush()
        donation = Donation(donor_id=donor.id, campaign_id=campaign.id, amount=1500,
                             payment_mode="online", status="success", razorpay_payment_id="pay_TUpxAGHbCtHxop")
        db.session.add(donation)
        db.session.commit()
        return donation

    def test_generates_a_receipt_with_no_notification_sent(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        campaign = _mk_campaign(db)
        donation = self._mk_receiptless_donation(db, campaign)
        assert donation.receipt_number is None

        with patch("public.send_receipt_email") as mock_email, \
             patch("public.send_receipt_whatsapp") as mock_whatsapp:
            resp = client.post(
                f"/admin/donations/{donation.id}/generate-receipt", follow_redirects=True
            )
            mock_email.assert_not_called()
            mock_whatsapp.assert_not_called()

        refreshed = Donation.query.get(donation.id)
        assert refreshed.receipt_number is not None
        assert refreshed.receipt_pdf is not None
        assert refreshed.receipt_number.encode() in resp.data

    def test_is_idempotent_on_a_second_call(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        campaign = _mk_campaign(db)
        donation = self._mk_receiptless_donation(db, campaign)

        client.post(f"/admin/donations/{donation.id}/generate-receipt")
        first_number = Donation.query.get(donation.id).receipt_number

        resp = client.post(f"/admin/donations/{donation.id}/generate-receipt", follow_redirects=True)
        second_number = Donation.query.get(donation.id).receipt_number

        assert first_number == second_number
        assert f"already has receipt {first_number}".encode() in resp.data

    def test_requires_admin_role(self, app, client):
        from extensions import db
        from models import Donation
        login(client, username="teststaff")
        campaign = _mk_campaign(db)
        donation = self._mk_receiptless_donation(db, campaign)

        client.post(f"/admin/donations/{donation.id}/generate-receipt")
        assert Donation.query.get(donation.id).receipt_number is None

    def test_rejects_a_non_success_donation(self, app, client):
        from extensions import db
        from models import Donor, Donation
        login(client)
        campaign = _mk_campaign(db)
        donor = Donor(full_name="Pending Backfill Donor", phone="9876500602")
        db.session.add(donor)
        db.session.flush()
        donation = Donation(donor_id=donor.id, campaign_id=campaign.id, amount=500,
                             payment_mode="online", status="pending")
        db.session.add(donation)
        db.session.commit()

        client.post(f"/admin/donations/{donation.id}/generate-receipt")
        assert Donation.query.get(donation.id).receipt_number is None

    def test_button_appears_on_dhoti_kurta_list_for_receiptless_rows(self, app, client):
        from extensions import db
        login(client)
        campaign = _mk_campaign(db)
        donation = self._mk_receiptless_donation(db, campaign)

        html = client.get("/admin/dhoti-kurta-contributions?range=all").data.decode()
        assert f'/admin/donations/{donation.id}/generate-receipt' in html
        assert "Generate receipt" in html

    def test_button_appears_on_donor_detail_for_receiptless_rows(self, app, client):
        from extensions import db
        login(client)
        campaign = _mk_campaign(db)
        donation = self._mk_receiptless_donation(db, campaign)

        html = client.get(f"/admin/donors/{donation.donor_id}").data.decode()
        assert f'/admin/donations/{donation.id}/generate-receipt' in html
