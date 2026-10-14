"""Donor Giving page (admin.donor_giving): one row per donor, a column per
month, the year's total at the end, with filters and sorting.

Drives the real route through the Flask test client and checks what is
rendered / exported -- same standard as test_iyf_camps.py.
"""
import csv
import datetime
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


@pytest.fixture
def _logged_in(client):
    login(client)


def _donation(donor, campaign_name, amount, when, status="success"):
    """`when` is a naive *UTC* datetime, exactly as the app stores it."""
    from extensions import db
    from models import Campaign, Donation
    campaign = Campaign.query.filter_by(name=campaign_name).one()
    d = Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=amount, payment_mode="online",
        status=status, donation_date=when,
    )
    db.session.add(d)
    db.session.commit()
    return d


def _donor(name, phone, email=None):
    from extensions import db
    from models import Donor
    donor = Donor(full_name=name, phone=phone, email=email)
    db.session.add(donor)
    db.session.commit()
    return donor


@pytest.fixture
def seeded(app):
    """FY 2026-27 (1 Apr 2026 - 31 Mar 2027, IST):
      Asha   Apr 1000 + May 2000 (+ a pending and a cancelled one that must not count)
      Bhima  one 5000 donation at 20:00 UTC on 30 Apr = 01:30 IST on 1 May -> MAY, not April
      Chitra one 700 donation at 20:00 UTC on 31 Mar 2027 = 01:30 IST on 1 Apr 2027 -> FY 2027-28
    """
    asha = _donor("Asha Devi", "9000000001", "asha@example.com")
    bhima = _donor("Bhima Das", "9000000002")
    chitra = _donor("Chitra Rani", "9000000003")
    _donation(asha, "Annadan", 1000, datetime.datetime(2026, 4, 10, 6, 0))
    _donation(asha, "Temple Construction", 2000, datetime.datetime(2026, 5, 5, 6, 0))
    _donation(asha, "Annadan", 9999, datetime.datetime(2026, 5, 6, 6, 0), status="pending")
    _donation(asha, "Annadan", 8888, datetime.datetime(2026, 5, 7, 6, 0), status="cancelled")
    _donation(bhima, "Annadan", 5000, datetime.datetime(2026, 4, 30, 20, 0))
    _donation(chitra, "Annadan", 700, datetime.datetime(2027, 3, 31, 20, 0))
    return {"asha": asha, "bhima": bhima, "chitra": chitra}


def _names_in_order(html):
    import re
    return re.findall(r'donors/\d+">([^<]+)</a>', html)


@pytest.mark.usefixtures("_logged_in")
class TestTheGrid:
    def test_lists_each_donor_with_year_total_and_only_successful_money(self, client, seeded):
        resp = client.get("/admin/donor-giving?basis=fy&year=2026")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "FY 2026-27" in html
        assert "Asha Devi" in html and "Bhima Das" in html
        # Asha: 1000 + 2000 = 3000. The pending 9999 and cancelled 8888 are not money received.
        assert "9,999" not in html and "8,888" not in html
        assert "11,999" not in html

    def test_a_late_night_donation_lands_in_the_ist_month_not_the_utc_one(self, client, seeded):
        """Bhima's 20:00 UTC on 30 April is 1:30 AM IST on 1 May."""
        resp = client.get("/admin/donor-giving?basis=fy&year=2026&q=Bhima&export=1")
        csv_resp = client.get("/admin/donor-giving/export?basis=fy&year=2026&q=Bhima")
        rows = list(csv.reader(io.StringIO(csv_resp.get_data(as_text=True))))
        header, bhima = rows[0], rows[1]
        assert bhima[0] == "Bhima Das"
        assert float(bhima[header.index("Apr '26")]) == 0
        assert float(bhima[header.index("May '26")]) == 5000

    def test_the_financial_year_boundary_follows_ist(self, client, seeded):
        """Chitra's 20:00 UTC on 31 March 2027 is already 1 April 2027 in
        India: FY 2027-28, not 2026-27."""
        fy26 = client.get("/admin/donor-giving?basis=fy&year=2026").get_data(as_text=True)
        fy27 = client.get("/admin/donor-giving?basis=fy&year=2027").get_data(as_text=True)
        assert "Chitra Rani" not in fy26
        assert "Chitra Rani" in fy27

    def test_calendar_year_basis(self, client, seeded):
        cy26 = client.get("/admin/donor-giving?basis=cy&year=2026").get_data(as_text=True)
        assert "Calendar Year 2026" in cy26
        assert "Asha Devi" in cy26 and "Bhima Das" in cy26
        assert "Chitra Rani" not in cy26   # 1 Apr 2027 IST is calendar 2027

    def test_footer_totals_cover_every_donor(self, client, seeded):
        csv_resp = client.get("/admin/donor-giving/export?basis=fy&year=2026")
        rows = list(csv.reader(io.StringIO(csv_resp.get_data(as_text=True))))
        total_row = rows[-1]
        assert total_row[0] == "TOTAL"
        assert float(total_row[rows[0].index("Total 2026-27 (Rs)")]) == 8000   # 3000 + 5000


@pytest.mark.usefixtures("_logged_in")
class TestSorting:
    def test_largest_total_first_is_the_default(self, client, seeded):
        html = client.get("/admin/donor-giving?basis=fy&year=2026").get_data(as_text=True)
        assert _names_in_order(html) == ["Bhima Das", "Asha Devi"]

    def test_smallest_total_first(self, client, seeded):
        html = client.get("/admin/donor-giving?basis=fy&year=2026&sort=total_asc").get_data(as_text=True)
        assert _names_in_order(html) == ["Asha Devi", "Bhima Das"]

    def test_name_sort_both_ways(self, client, seeded):
        az = client.get("/admin/donor-giving?basis=fy&year=2026&sort=name_asc").get_data(as_text=True)
        za = client.get("/admin/donor-giving?basis=fy&year=2026&sort=name_desc").get_data(as_text=True)
        assert _names_in_order(az) == ["Asha Devi", "Bhima Das"]
        assert _names_in_order(za) == ["Bhima Das", "Asha Devi"]

    def test_largest_single_donation(self, client, seeded):
        """Asha's biggest single gift is 2000; Bhima's is 5000."""
        html = client.get("/admin/donor-giving?basis=fy&year=2026&sort=largest_asc").get_data(as_text=True)
        assert _names_in_order(html) == ["Asha Devi", "Bhima Das"]

    def test_sorting_by_a_month_column(self, client, seeded):
        """April: Asha 1000, Bhima 0. May: Asha 2000, Bhima 5000."""
        apr = client.get("/admin/donor-giving?basis=fy&year=2026&sort=m4_desc").get_data(as_text=True)
        may = client.get("/admin/donor-giving?basis=fy&year=2026&sort=m5_desc").get_data(as_text=True)
        assert _names_in_order(apr) == ["Asha Devi", "Bhima Das"]
        assert _names_in_order(may) == ["Bhima Das", "Asha Devi"]

    def test_a_garbage_sort_value_falls_back_instead_of_erroring(self, client, seeded):
        resp = client.get("/admin/donor-giving?basis=fy&year=2026&sort=;drop table donors_asc")
        assert resp.status_code == 200
        assert _names_in_order(resp.get_data(as_text=True)) == ["Bhima Das", "Asha Devi"]


@pytest.mark.usefixtures("_logged_in")
class TestFilters:
    def test_search_by_name_phone_or_email(self, client, seeded):
        by_name = client.get("/admin/donor-giving?basis=fy&year=2026&q=asha").get_data(as_text=True)
        by_phone = client.get("/admin/donor-giving?basis=fy&year=2026&q=9000000002").get_data(as_text=True)
        by_email = client.get("/admin/donor-giving?basis=fy&year=2026&q=asha@example").get_data(as_text=True)
        assert _names_in_order(by_name) == ["Asha Devi"]
        assert _names_in_order(by_phone) == ["Bhima Das"]
        assert _names_in_order(by_email) == ["Asha Devi"]

    def test_campaign_filter_counts_only_that_campaigns_money(self, client, seeded):
        from models import Campaign
        tc = Campaign.query.filter_by(name="Temple Construction").one()
        html = client.get(f"/admin/donor-giving?basis=fy&year=2026&campaign_id={tc.id}").get_data(as_text=True)
        assert _names_in_order(html) == ["Asha Devi"]
        csv_resp = client.get(f"/admin/donor-giving/export?basis=fy&year=2026&campaign_id={tc.id}")
        rows = list(csv.reader(io.StringIO(csv_resp.get_data(as_text=True))))
        assert float(rows[1][rows[0].index("Total 2026-27 (Rs)")]) == 2000   # not 3000

    def test_minimum_and_maximum_year_total(self, client, seeded):
        big = client.get("/admin/donor-giving?basis=fy&year=2026&min_total=4000").get_data(as_text=True)
        small = client.get("/admin/donor-giving?basis=fy&year=2026&max_total=4000").get_data(as_text=True)
        assert _names_in_order(big) == ["Bhima Das"]
        assert _names_in_order(small) == ["Asha Devi"]

    def test_comma_formatted_and_junk_amounts_are_tolerated(self, client, seeded):
        ok = client.get("/admin/donor-giving?basis=fy&year=2026&min_total=4,000").get_data(as_text=True)
        junk = client.get("/admin/donor-giving?basis=fy&year=2026&min_total=abc&max_total=-5")
        assert _names_in_order(ok) == ["Bhima Das"]
        assert junk.status_code == 200
        assert len(_names_in_order(junk.get_data(as_text=True))) == 2   # junk is ignored, not applied

    def test_an_empty_year_says_so(self, client, seeded):
        resp = client.get("/admin/donor-giving?basis=fy&year=2019")
        assert resp.status_code == 200
        assert "No donations match these filters" in resp.get_data(as_text=True)


@pytest.mark.usefixtures("_logged_in")
class TestPagination:
    def test_more_than_one_page_of_donors(self, client, app):
        for i in range(55):
            _donation(_donor(f"Donor {i:03d}", f"91000{i:05d}"), "Annadan", 100 + i,
                      datetime.datetime(2026, 6, 1, 6, 0))
        page1 = client.get("/admin/donor-giving?basis=fy&year=2026&sort=name_asc").get_data(as_text=True)
        page2 = client.get("/admin/donor-giving?basis=fy&year=2026&sort=name_asc&page=2").get_data(as_text=True)
        assert len(_names_in_order(page1)) == 50
        assert len(_names_in_order(page2)) == 5
        assert "Donor 000" in page1 and "Donor 054" in page2
        # The footer total covers all 55, not just the visible page.
        assert "55 donors" in page1

    def test_export_contains_every_donor_not_just_the_page(self, client, app):
        for i in range(55):
            _donation(_donor(f"Donor {i:03d}", f"91000{i:05d}"), "Annadan", 100,
                      datetime.datetime(2026, 6, 1, 6, 0))
        rows = list(csv.reader(io.StringIO(
            client.get("/admin/donor-giving/export?basis=fy&year=2026").get_data(as_text=True))))
        assert len(rows) == 1 + 55 + 1   # header + donors + TOTAL


class TestExportAndAccess:
    @pytest.mark.usefixtures("_logged_in")
    def test_export_neutralises_spreadsheet_formulas_in_names(self, client, app):
        _donation(_donor("=HYPERLINK(\"http://evil\")", "9000000099"), "Annadan", 100,
                  datetime.datetime(2026, 6, 1, 6, 0))
        body = client.get("/admin/donor-giving/export?basis=fy&year=2026").get_data(as_text=True)
        rows = list(csv.reader(io.StringIO(body)))
        assert not rows[1][0].startswith("=")

    @pytest.mark.usefixtures("_logged_in")
    def test_export_is_a_csv_attachment(self, client, seeded):
        resp = client.get("/admin/donor-giving/export?basis=fy&year=2026")
        assert resp.mimetype == "text/csv"
        assert "Donor_Giving_FY_2026-27.csv" in resp.headers["Content-Disposition"]

    def test_requires_login(self, client):
        for url in ("/admin/donor-giving", "/admin/donor-giving/export"):
            resp = client.get(url, follow_redirects=False)
            assert resp.status_code == 302 and "/admin/login" in resp.headers["Location"]

    @pytest.mark.usefixtures("_logged_in")
    def test_the_nav_links_to_it(self, client, seeded):
        html = client.get("/admin/donors").get_data(as_text=True)
        assert "/admin/donor-giving" in html
