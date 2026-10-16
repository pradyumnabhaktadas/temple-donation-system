"""Fixes from the October 2026 audit, driven through the real routes."""
import datetime
import re
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import login
from extensions import db

IST = datetime.timedelta(hours=5, minutes=30)


def _create(client, name="Dwarka", slug="dwarka", **extra):
    data = {"name": name, "slug": slug}
    data.update(extra)
    return client.post("/admin/preaching-centers", data=data, follow_redirects=True)


def _center(slug="dwarka"):
    from models import PreachingCenter
    return PreachingCenter.query.filter_by(slug=slug).one()


def _donor(name="Asha", phone="9876500001"):
    from models import Donor
    donor = Donor(full_name=name, phone=phone)
    db.session.add(donor)
    db.session.commit()
    return donor


def _give(center, donor, amount, when_utc, status="success"):
    from models import Campaign, Donation
    campaign = Campaign.query.filter_by(name="Annadan").one()
    donation = Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=amount, payment_mode="online",
        status=status, donation_date=when_utc, preaching_center_id=center.id,
        preaching_center_name=center.name,
    )
    db.session.add(donation)
    db.session.commit()
    return donation


def _ist_noon_utc(day):
    return datetime.datetime.combine(day, datetime.time(12, 0)) - IST


def _today():
    from utils import now_ist
    return now_ist().date()


@pytest.fixture
def admin(client):
    login(client)
    return client


class TestTargetAndPurposeInputs:
    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e20", "abc", "-5"])
    def test_a_bad_target_is_refused_on_create(self, admin, app, bad):
        from models import PreachingCenter
        response = _create(admin, target_amount=bad, target_period="monthly")
        assert response.status_code == 200
        assert PreachingCenter.query.count() == 0

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e20"])
    def test_a_bad_target_is_refused_on_edit_without_a_server_error(self, admin, app, bad):
        _create(admin, target_amount="500", target_period="monthly")
        center = _center()
        response = admin.post(f"/admin/preaching-centers/{center.id}/edit", data={
            "name": center.name, "target_amount": bad, "target_period": "monthly",
        }, follow_redirects=True)
        assert response.status_code == 200
        db.session.refresh(center)
        assert center.target_amount == Decimal("500")

    def test_a_target_is_stored_to_two_decimals(self, admin, app):
        _create(admin, target_amount="1234.5", target_period="monthly")
        assert _center().target_amount == Decimal("1234.50")

    def test_a_non_ascii_digit_in_the_purpose_list_does_not_crash(self, admin, app):
        _create(admin)
        center = _center()
        before = {link.live_to_give_purpose_id for link in center.purpose_links}
        response = admin.post(f"/admin/preaching-centers/{center.id}/edit", data={
            "name": center.name, "purpose_ids": ["²", str(next(iter(before)))],
        }, follow_redirects=True)
        assert response.status_code == 200


class TestCenterLinkOnlyTakesLiveToGive:
    def _l2g(self):
        from models import Campaign, LiveToGivePurpose
        campaign = Campaign(name="Live To Give", is_80g=False)
        db.session.add(campaign)
        db.session.commit()
        return campaign, LiveToGivePurpose.query.filter_by(name="Deity Seva").one()

    def _order(self, client, campaign, purpose=None, slug="dwarka"):
        payload = {
            "amount": 501, "full_name": "Link Donor", "phone": "9876501234", "consent": "on",
            "campaign_id": campaign.id, "pan": "ABCDE1234F", "preaching_center_slug": slug,
        }
        if purpose is not None:
            payload["live_to_give_purpose_id"] = purpose.id
        return client.post("/api/create-order", json=payload)

    def test_another_campaign_cannot_be_tagged_to_a_center(self, admin, app):
        from models import Campaign, Donation
        _create(admin)
        self._l2g()
        bace = Campaign.query.filter_by(name="BACE Contribution").one()
        assert self._order(admin, bace).status_code == 400
        assert Donation.query.count() == 0

    def test_a_purpose_is_required(self, admin, app):
        from models import Donation
        _create(admin)
        campaign, _purpose = self._l2g()
        assert self._order(admin, campaign).status_code == 400
        assert Donation.query.count() == 0

    def test_a_purpose_from_another_centers_list_is_refused(self, admin, app):
        from models import Donation, LiveToGivePurpose
        _create(admin)
        campaign, _purpose = self._l2g()
        stray = LiveToGivePurpose(name="Unrelated Purpose", is_80g=False)
        db.session.add(stray)
        db.session.commit()
        assert self._order(admin, campaign, stray).status_code == 400
        assert Donation.query.count() == 0

    def test_a_valid_center_order_is_recorded(self, admin, app):
        from models import Donation
        _create(admin)
        campaign, purpose = self._l2g()
        assert self._order(admin, campaign, purpose).status_code == 200
        assert Donation.query.one().preaching_center_id == _center().id


class TestLinksUsePublicBaseUrl:
    @pytest.fixture
    def fake_qrcode(self, monkeypatch):
        seen = []

        class _Image:
            def save(self, stream, format=None):
                stream.write(b"\x89PNG fake")

        module = types.ModuleType("qrcode")
        module.make = lambda text: (seen.append(text) or _Image())
        monkeypatch.setitem(sys.modules, "qrcode", module)
        return seen

    def test_the_qr_encodes_the_configured_domain_not_the_request_host(self, admin, app, fake_qrcode):
        app.config["PUBLIC_BASE_URL"] = "https://givetokrishna.com"
        _create(admin)
        center = _center()
        response = admin.get(f"/admin/preaching-centers/{center.id}/qr.png", base_url="http://temple-x.onrender.com")
        assert response.status_code == 200 and response.mimetype == "image/png"
        assert fake_qrcode == ["https://givetokrishna.com/pc/dwarka"]

    def test_the_detail_page_shows_the_public_link(self, admin, app):
        app.config["PUBLIC_BASE_URL"] = "https://givetokrishna.com"
        _create(admin)
        html = admin.get(f"/admin/preaching-centers/{_center().id}", base_url="http://temple-x.onrender.com").get_data(as_text=True)
        assert 'href="https://givetokrishna.com/pc/dwarka"' in html
        assert "temple-x.onrender.com/pc/dwarka" not in html

    def test_the_share_card_is_a_pdf_with_the_link(self, admin, app):
        app.config["PUBLIC_BASE_URL"] = "https://givetokrishna.com"
        _create(admin, name="Dwarka Preaching Center", city="New Delhi")
        response = admin.get(f"/admin/preaching-centers/{_center().id}/share-card.pdf")
        assert response.status_code == 200
        assert response.mimetype == "application/pdf"
        assert response.data.startswith(b"%PDF")


class TestNamesAndNotices:
    def test_names_are_unique_ignoring_case(self, admin, app):
        from models import PreachingCenter
        _create(admin, name="Dwarka", slug="dwarka")
        _create(admin, name="  dwarka ", slug="dwarka-2")
        assert PreachingCenter.query.count() == 1

    def test_rename_updates_the_name_copied_onto_donations(self, admin, app):
        _create(admin)
        center = _center()
        donation = _give(center, _donor(), 100, datetime.datetime.utcnow())
        admin.post(f"/admin/preaching-centers/{center.id}/edit", data={"name": "Dwarka East"}, follow_redirects=True)
        db.session.refresh(donation)
        assert donation.preaching_center_name == "Dwarka East"

    def test_creating_a_center_says_when_it_adds_global_purposes(self, admin, app):
        response = _create(admin)
        html = response.get_data(as_text=True)
        assert "Deity Seva" in html and "Live To Give purpose list" in html

    def test_no_notice_when_the_purposes_already_exist(self, admin, app):
        _create(admin, name="First", slug="first")
        html = _create(admin, name="Second", slug="second").get_data(as_text=True)
        assert "Live To Give purpose list" not in html


class TestCenterStatistics:
    def test_figures_use_only_successful_donations_and_ist_boundaries(self, admin, app):
        from preaching_center_stats import center_stats
        _create(admin)
        _create(admin, name="Other", slug="other")
        center, other = _center(), _center("other")
        asha, ravi = _donor("Asha", "9876500001"), _donor("Ravi", "9876500002")
        today = _today()
        _give(center, asha, 100, _ist_noon_utc(today))
        _give(center, asha, 200, _ist_noon_utc(today - datetime.timedelta(days=120)))
        _give(center, ravi, 50, _ist_noon_utc(today - datetime.timedelta(days=121)))
        _give(center, ravi, 999, _ist_noon_utc(today), status="pending")
        _give(center, ravi, 999, _ist_noon_utc(today), status="cancelled")
        _give(other, ravi, 7, _ist_noon_utc(today))

        stats = center_stats(today)[center.id]
        assert stats["today"] == Decimal("100")
        assert stats["lifetime"] == Decimal("350")
        assert stats["count"] == 3 and stats["donors"] == 2
        assert Decimal("100") <= stats["month"] <= stats["fy"] <= stats["lifetime"]
        assert stats["last_donation"] == _ist_noon_utc(today)
        assert center_stats(today)[other.id]["lifetime"] == Decimal("7")

    def test_a_late_night_ist_donation_counts_on_the_ist_day(self, admin, app):
        from preaching_center_stats import center_stats
        _create(admin)
        center = _center()
        # 20:00 UTC on 30 June is 01:30 IST on 1 July.
        _give(center, _donor(), 300, datetime.datetime(2026, 6, 30, 20, 0))
        assert center_stats(datetime.date(2026, 7, 1))[center.id]["today"] == Decimal("300")
        assert center_stats(datetime.date(2026, 6, 30))[center.id]["today"] == Decimal("0")

    def test_target_progress_for_a_monthly_target(self, admin, app):
        from preaching_center_stats import center_stats, target_progress
        _create(admin, target_amount="1000", target_period="monthly")
        center = _center()
        _give(center, _donor(), 100, _ist_noon_utc(_today()))
        progress = target_progress(center, center_stats(_today())[center.id])
        assert progress["achieved"] == Decimal("100")
        assert progress["pending"] == Decimal("900")
        assert progress["pct"] == Decimal("10")

    def test_pending_target_is_never_negative(self, admin, app):
        from preaching_center_stats import center_stats, target_progress
        _create(admin, target_amount="50", target_period="monthly")
        center = _center()
        _give(center, _donor(), 100, _ist_noon_utc(_today()))
        assert target_progress(center, center_stats(_today())[center.id])["pending"] == Decimal("0")

    def test_no_target_means_no_progress(self, admin, app):
        from preaching_center_stats import EMPTY_STATS, target_progress
        _create(admin)
        assert target_progress(_center(), EMPTY_STATS) is None


class TestCenterReportPages:
    def test_dashboard_shows_every_figure(self, admin, app):
        _create(admin, target_amount="1000", target_period="monthly")
        center = _center()
        _give(center, _donor(), 250, _ist_noon_utc(_today()))
        html = admin.get("/admin/preaching-centers/dashboard").get_data(as_text=True)
        assert "Dwarka" in html and "Rs. 250" in html
        assert "pending" in html and "Active" in html

    def test_detail_filters_and_pagination(self, admin, app):
        _create(admin)
        center = _center()
        asha, ravi = _donor("Asha Rao", "9876500001"), _donor("Ravi Kumar", "9876500002")
        for i in range(120):
            _give(center, asha, 10, _ist_noon_utc(_today() - datetime.timedelta(days=i % 30)))
        _give(center, ravi, 77, _ist_noon_utc(_today()))
        base = f"/admin/preaching-centers/{center.id}"

        first = admin.get(base).get_data(as_text=True)
        assert "121 donations match" in first
        assert "Page 1 of 3" in first
        assert "Page 2 of 3" in admin.get(base + "?page=2").get_data(as_text=True)

        by_name = admin.get(base + "?q=ravi").get_data(as_text=True)
        assert "1 donation match" in by_name
        assert "<td>Ravi Kumar</td>" in by_name and "<td>Asha Rao</td>" not in by_name   # (top-donors panel is unfiltered)
        by_phone = admin.get(base + "?q=9876500002").get_data(as_text=True)
        assert "1 donation match" in by_phone

        day = _today().isoformat()
        assert "1 donation match" in admin.get(f"{base}?from={day}&to={day}&q=ravi").get_data(as_text=True)
        assert "No donations match" in admin.get(base + "?status=failed").get_data(as_text=True)

    def test_detail_status_filter_reaches_pending_donations(self, admin, app):
        _create(admin)
        center = _center()
        _give(center, _donor(), 40, _ist_noon_utc(_today()), status="pending")
        base = f"/admin/preaching-centers/{center.id}"
        assert "No donations match" in admin.get(base).get_data(as_text=True)
        assert "1 donation match" in admin.get(base + "?status=pending").get_data(as_text=True)
        assert "1 donation match" in admin.get(base + "?status=").get_data(as_text=True)

    def test_detail_dates_are_shown_in_ist(self, admin, app):
        _create(admin)
        center = _center()
        _give(center, _donor(), 10, datetime.datetime(2026, 3, 31, 20, 0))   # 1 April, 01:30 IST
        html = admin.get(f"/admin/preaching-centers/{center.id}").get_data(as_text=True)
        assert "01-Apr-2026" in html and "31-Mar-2026" not in html

    def test_detail_lists_top_donors_and_months(self, admin, app):
        _create(admin)
        center = _center()
        _give(center, _donor("Big Giver", "9876500001"), 900, datetime.datetime(2026, 5, 10, 6, 0))
        _give(center, _donor("Small Giver", "9876500002"), 100, datetime.datetime(2026, 6, 10, 6, 0))
        html = admin.get(f"/admin/preaching-centers/{center.id}").get_data(as_text=True)
        assert html.index("Big Giver") < html.index("Small Giver")
        assert "2026-05" in html and "2026-06" in html

    def test_csv_export_follows_the_same_filters(self, admin, app):
        _create(admin)
        center = _center()
        _give(center, _donor("Asha Rao", "9876500001"), 10, _ist_noon_utc(_today()))
        _give(center, _donor("Ravi Kumar", "9876500002"), 20, _ist_noon_utc(_today()))
        csv_text = admin.get(f"/admin/preaching-centers/{center.id}/export.csv?q=ravi").get_data(as_text=True)
        assert "Ravi Kumar" in csv_text and "Asha Rao" not in csv_text

    def test_a_center_viewer_cannot_open_another_centers_share_card(self, app, client):
        from models import AdminUser
        login(client)
        _create(client, name="Mine", slug="mine")
        _create(client, name="Theirs", slug="theirs")
        mine, theirs = _center("mine"), _center("theirs")
        viewer = AdminUser(username="viewer1", role="preaching_center_viewer", preaching_center_id=mine.id,
                           must_change_password=False)
        viewer.set_password("TestPass123!")
        db.session.add(viewer)
        db.session.commit()
        client.get("/admin/logout")
        login(client, username="viewer1")
        assert client.get(f"/admin/preaching-centers/{mine.id}/share-card.pdf").status_code == 200
        assert client.get(f"/admin/preaching-centers/{theirs.id}/share-card.pdf").status_code == 403

    def test_main_dashboard_has_a_center_section(self, admin, app):
        before = admin.get("/admin/dashboard").get_data(as_text=True)
        assert "Noida Center" not in before
        _create(admin, name="Noida Center", slug="noida")
        _give(_center("noida"), _donor(), 640, _ist_noon_utc(_today()))
        html = admin.get("/admin/dashboard").get_data(as_text=True)
        assert "Noida Center" in html and "640" in html


class TestDailyReportCenterSection:
    def test_report_lists_centers_and_the_email_renders_them(self, admin, app):
        from daily_report_utils import _render_email_html, compute_report
        _create(admin, name="Dwarka <Main>", slug="dwarka")
        center = _center()
        report_date = _today() - datetime.timedelta(days=1)
        _give(center, _donor(), 500, _ist_noon_utc(report_date))
        data = compute_report(report_date)
        assert data["centers"][0]["today"][0] == Decimal("500")
        html = _render_email_html(data, "Temple")
        assert "Preaching Centers" in html
        assert "Dwarka &lt;Main&gt;" in html

    def test_report_has_no_section_without_center_donations(self, app):
        from daily_report_utils import _render_email_html, compute_report
        data = compute_report(_today() - datetime.timedelta(days=1))
        assert data["centers"] == []
        assert "Preaching Centers" not in _render_email_html(data, "Temple")


class TestMemoryFixes:
    def test_receipt_pdf_is_not_loaded_with_the_row(self, app):
        from sqlalchemy import inspect
        from models import Campaign, Donation
        donor = _donor()
        campaign = Campaign.query.filter_by(name="Annadan").one()
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign.id, amount=10, payment_mode="cash",
                                status="success", receipt_pdf=b"%PDF-1.4 big"))
        db.session.commit()
        db.session.expire_all()
        loaded = Donation.query.first()
        assert "receipt_pdf" in inspect(loaded).unloaded
        assert loaded.receipt_pdf == b"%PDF-1.4 big"          # still readable on demand

    def test_campaign_and_donor_totals_count_only_successful_donations(self, app):
        from models import Campaign, Donation
        donor = _donor()
        campaign = Campaign.query.filter_by(name="Annadan").one()
        for amount, status in ((100, "success"), (50, "success"), (999, "pending"), (999, "cancelled")):
            db.session.add(Donation(donor_id=donor.id, campaign_id=campaign.id, amount=amount,
                                    payment_mode="cash", status=status))
        db.session.commit()
        assert campaign.total_collected == 150
        assert donor.total_donated == 150
        empty = Campaign.query.filter_by(name="BACE Contribution").one()
        assert empty.total_collected == 0

    def test_campaigns_page_still_renders_with_a_target(self, admin, app):
        from models import Campaign, Donation
        donor = _donor()
        campaign = Campaign.query.filter_by(name="Temple Construction").one()
        db.session.add(Donation(donor_id=donor.id, campaign_id=campaign.id, amount=2500, payment_mode="cash",
                                status="success"))
        db.session.commit()
        html = admin.get("/admin/campaigns").get_data(as_text=True)
        assert "2,500" in html


class TestPublicHeaderIsFailSafe:
    def test_a_database_error_loading_centers_does_not_break_pages(self, app, client, monkeypatch):
        from models import PreachingCenter

        class _Boom:
            def filter_by(self, **kw):
                raise RuntimeError("database hiccup")

        monkeypatch.setattr(PreachingCenter, "query", _Boom(), raising=False)
        assert client.get("/").status_code == 200

    def test_admin_pages_do_not_query_centers_for_the_header(self, app, client, monkeypatch):
        from models import PreachingCenter
        login(client)
        calls = []

        class _Spy:
            def filter_by(self, **kw):
                calls.append(kw)
                raise AssertionError("header query ran on an admin page")

        monkeypatch.setattr(PreachingCenter, "query", _Spy(), raising=False)
        assert client.get("/admin/donors").status_code == 200
        assert calls == []


class TestDependenciesArePinned:
    def test_every_library_that_has_broken_production_is_pinned(self):
        text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text().lower()
        for name in ("sqlalchemy", "alembic", "werkzeug", "jinja2", "markupsafe", "wtforms", "pillow"):
            assert re.search(rf"^{name}==", text, re.M), f"{name} must be pinned with =="
