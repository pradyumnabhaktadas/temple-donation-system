"""Possible Duplicate Donors: one Merge button per group, and the donor
page's older "Merge a Duplicate Record" box -- both go through the same
_absorb_donor(), so both are covered here.

Drives the real routes with the Flask test client and checks what landed in
the database, same standard as test_iyf_camps.py.
"""
import datetime
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _donor(name, phone=None, email=None, **extra):
    from extensions import db
    from models import Donor
    donor = Donor(full_name=name, phone=phone, email=email, **extra)
    db.session.add(donor)
    db.session.commit()
    return donor


def _give(donor, amount, status="success"):
    from extensions import db
    from models import Campaign, Donation
    campaign = Campaign.query.filter_by(name="Annadan").one()
    d = Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=amount, payment_mode="online",
        status=status, donation_date=datetime.datetime(2026, 6, 1, 6, 0),
    )
    db.session.add(d)
    db.session.commit()
    return d


def _merge(client, *donors, keep=None):
    data = {"donor_ids": [str(d.id) for d in donors]}
    if keep is not None:
        data["keep_id"] = str(keep.id)
    return client.post("/admin/donors/duplicates/merge", data=data, follow_redirects=True)


def _compare_url(*donors):
    return "/admin/donors/duplicates/compare?" + "&".join(f"donor_ids={d.id}" for d in donors)


@pytest.fixture
def admin(client):
    login(client)
    return client


class TestThePage:
    def test_each_group_has_exactly_one_button_that_opens_the_comparison(self, admin, app):
        _give(_donor("Astha", "6204999675"), 2000)
        _give(_donor("Astha Rai", "6204999675"), 1000)
        _give(_donor("Vivek", "6206640839"), 500)
        _give(_donor("Vivek Kumar", "6206640839"), 200)

        html = admin.get("/admin/donors/duplicates").get_data(as_text=True)

        assert html.count("Compare &amp; merge</a>") == 2   # one per group, not one per row
        assert "View &amp; merge" not in html               # the old per-row link is gone

    def test_the_donor_with_most_giving_is_marked_as_the_one_kept(self, admin, app):
        small = _donor("Astha", "6204999675")
        big = _donor("Astha Rai", "6204999675")
        _give(small, 100)
        _give(big, 5000)

        html = admin.get("/admin/donors/duplicates").get_data(as_text=True)

        assert html.count("badge bg-success ms-1") == 1      # the intro text shows one example badge too
        keeper_row = re.search(r"Astha Rai</a>\s*<span[^>]*>Keeps", html)
        assert keeper_row, "the donor with the larger total should carry the Keeps badge"

    def test_the_confirmation_names_who_is_kept(self, admin, app):
        small = _donor("Small Name", "6204999675")
        big = _donor("Big Name", "6204999675")
        _give(small, 100)
        _give(big, 5000)
        html = admin.get(_compare_url(small, big)).get_data(as_text=True)
        assert 'data-confirm="Merge these 2 records into Big Name?' in html

    def test_staff_can_compare_but_not_merge(self, client, app):
        a = _donor("Astha", "6204999675")
        b = _donor("Astha Rai", "6204999675")
        _give(a, 100)
        _give(b, 200)
        login(client, username="teststaff", password="TestPass123!")
        listing = client.get("/admin/donors/duplicates").get_data(as_text=True)
        compare = client.get(_compare_url(a, b)).get_data(as_text=True)
        assert "Astha Rai" in listing and "Astha Rai" in compare
        assert "Merge into" not in compare and "Only an admin can merge" in compare

    def test_an_email_group_works_the_same_way(self, admin, app):
        _give(_donor("A One", "9000000001", "shared@example.com"), 100)
        _give(_donor("A Two", "9000000002", "shared@example.com"), 900)
        html = admin.get("/admin/donors/duplicates").get_data(as_text=True)
        assert html.count("Compare &amp; merge</a>") == 1


class TestTheComparison:
    def test_the_list_links_to_a_comparison_of_the_whole_group(self, admin, app):
        a, b, c = _donor("A", "6204999675"), _donor("B", "6204999675"), _donor("C", "6204999675")
        html = admin.get("/admin/donors/duplicates").get_data(as_text=True)
        href = re.search(r'href="([^"]*duplicates/compare[^"]*)"', html).group(1).replace("&amp;", "&")
        assert all(f"donor_ids={d.id}" in href for d in (a, b, c))
        assert admin.get(href).status_code == 200

    def test_each_record_offers_a_keep_choice_defaulting_to_the_biggest_giver(self, admin, app):
        a, b = _donor("astha", "6204999675"), _donor("Astha Rai", "6204999675")
        _give(a, 10)
        _give(b, 99)
        html = admin.get(_compare_url(a, b)).get_data(as_text=True)
        assert html.count('name="keep_id"') == 2
        assert re.search(rf'value="{b.id}"[^>]*checked', html)
        assert not re.search(rf'value="{a.id}"[^>]*checked', html)

    def test_shows_every_record_side_by_side_with_totals_and_recent_gifts(self, admin, app):
        a = _donor("Astha", "6204999675", city="Delhi", pan="ABCDE1234F")
        b = _donor("Astha Rai", "6204999675", city="Noida", email="rai@example.com")
        _give(a, 1000)
        _give(b, 2500)
        _give(b, 500)

        html = admin.get(_compare_url(a, b)).get_data(as_text=True)

        assert "Compare 2 Donor Records" in html
        for text in ("Astha", "Astha Rai", "Delhi", "Noida", "ABCDE1234F", "rai@example.com"):
            assert text in html
        assert "Rs. 3,000" in html and "Rs. 1,000" in html       # each record's own total
        assert "Annadan" in html                                # recent donations listed
        assert html.count('class="badge bg-success ms-1">Recommended') == 1
        assert "Merge into Astha Rai" in html                   # the one merge button, naming the kept record

    def test_differences_and_fill_ins_are_highlighted(self, admin, app):
        keeper = _donor("Keeper", "6204999675", city="Delhi")
        other = _donor("Other", "6204999675", city="Noida", email="o@example.com")
        _give(keeper, 5000)
        _give(other, 10)
        html = admin.get(_compare_url(keeper, other)).get_data(as_text=True)
        city_row = re.search(r"<tr[^>]*>\s*<th class=\"small\">City</th>", html).group(0)
        assert "#fff1cc" in city_row                             # two different cities -> amber
        assert "background:#e3f0ff" in html                      # kept record has no email -> blue

    def test_does_not_change_anything(self, admin, app):
        from models import Donor
        a, b = _donor("A", "6204999675"), _donor("B", "6204999675")
        admin.get(_compare_url(a, b))
        assert Donor.query.count() == 2

    def test_unrelated_donors_cannot_be_compared(self, admin, app):
        x = _donor("X", "9111111111", "x@example.com")
        y = _donor("Y", "9222222222", "y@example.com")
        resp = admin.get(_compare_url(x, y), follow_redirects=True)
        assert "can&#39;t be compared as a group" in resp.get_data(as_text=True)

    def test_a_stale_or_single_group_goes_back_to_the_list(self, admin, app):
        a = _donor("A", "6204999675")
        for url in (_compare_url(a), _compare_url(a) + "&donor_ids=999999"):
            resp = admin.get(url, follow_redirects=True)
            assert "already changed" in resp.get_data(as_text=True)

    def test_garbage_ids_are_a_400(self, admin, app):
        assert admin.get("/admin/donors/duplicates/compare?donor_ids=abc").status_code == 400

    def test_requires_login(self, client, app):
        a, b = _donor("A", "6204999675"), _donor("B", "6204999675")
        resp = client.get(_compare_url(a, b), follow_redirects=False)
        assert resp.status_code == 302 and "/admin/login" in resp.headers["Location"]


class TestMerging:
    def test_everything_moves_into_the_donor_with_most_giving(self, admin, app):
        from models import Donor, Donation
        small = _donor("Astha", "6204999675")
        big = _donor("Astha Rai", "6204999675")
        _give(small, 100)
        _give(small, 150)
        _give(big, 5000)
        small_id, big_id = small.id, big.id

        resp = _merge(admin, small, big)

        assert resp.status_code == 200
        assert Donor.query.get(small_id) is None
        survivor = Donor.query.get(big_id)
        assert survivor.full_name == "Astha Rai"
        assert Donation.query.filter_by(donor_id=big_id).count() == 3
        assert float(survivor.total_donated) == 5250
        assert "Merged 1 record(s) into Astha Rai" in resp.get_data(as_text=True)

    def test_which_button_you_press_cannot_change_which_record_survives(self, admin, app):
        """The page only sends *which donors*; ordering in the request is irrelevant."""
        from models import Donor
        small = _donor("Small", "6204999675")
        big = _donor("Big", "6204999675")
        _give(small, 1)
        _give(big, 9999)

        _merge(admin, small, big)
        assert Donor.query.filter_by(full_name="Big").count() == 1
        assert Donor.query.filter_by(full_name="Small").count() == 0

    def test_the_admin_can_choose_which_record_to_keep(self, admin, app):
        from models import Donor, Donation
        fuller = _donor("Astha Rai", "6204999675")
        lower = _donor("astha", "6204999675")
        _give(fuller, 100)
        _give(lower, 9000)          # the default survivor would be "astha"
        resp = _merge(admin, fuller, lower, keep=fuller)
        assert Donor.query.get(lower.id) is None
        assert Donor.query.get(fuller.id).full_name == "Astha Rai"
        assert Donation.query.filter_by(donor_id=fuller.id).count() == 2
        assert "into Astha Rai" in resp.get_data(as_text=True)

    def test_keep_id_outside_the_group_is_ignored(self, admin, app):
        from models import Donor
        a, b = _donor("A", "6204999675"), _donor("B", "6204999675")
        outsider = _donor("Outsider", "9111111111")
        _give(a, 1)
        _give(b, 50)
        _merge(admin, a, b, keep=outsider)
        assert Donor.query.get(outsider.id) is not None
        assert Donor.query.get(b.id) is not None and Donor.query.get(a.id) is None

    def test_a_three_way_group_merges_into_one(self, admin, app):
        from models import Donor, Donation
        a, b, c = _donor("A", "6204999675"), _donor("B", "6204999675"), _donor("C", "6204999675")
        _give(a, 10)
        _give(b, 300)
        _give(c, 20)

        _merge(admin, a, b, c)

        assert Donor.query.count() == 1
        assert Donor.query.one().full_name == "B"
        assert Donation.query.filter_by(donor_id=Donor.query.one().id).count() == 3

    def test_blank_details_on_the_kept_donor_are_filled_in_not_overwritten(self, admin, app):
        from models import Donor
        keeper = _donor("Keeper", "6204999675", pan="ABCDE1234F", city="Delhi")
        other = _donor("Other", "6204999675", email="other@example.com", pan="ZZZZZ9999Z",
                       city="Mumbai", address="12 Temple Road", dob=datetime.date(1990, 5, 17))
        _give(keeper, 5000)
        _give(other, 10)

        _merge(admin, keeper, other)

        kept = Donor.query.one()
        assert kept.pan == "ABCDE1234F" and kept.city == "Delhi"        # untouched
        assert kept.email == "other@example.com"                         # was blank
        assert kept.address == "12 Temple Road"
        assert kept.dob == datetime.date(1990, 5, 17)

    def test_notes_on_both_records_are_kept(self, admin, app):
        from models import Donor
        keeper = _donor("Keeper", "6204999675", additional_info="Prefers WhatsApp.")
        other = _donor("Other", "6204999675", additional_info="Sponsors Annadan every Ekadashi.")
        _give(keeper, 5000)
        _give(other, 10)

        _merge(admin, keeper, other)

        notes = Donor.query.one().additional_info
        assert "Prefers WhatsApp." in notes and "Sponsors Annadan every Ekadashi." in notes

    def test_the_merge_is_written_to_the_activity_log(self, admin, app):
        from models import AdminActivityLog
        a, b = _donor("A", "6204999675"), _donor("B", "6204999675")
        _give(a, 5)
        _give(b, 50)
        _merge(admin, a, b)
        entry = AdminActivityLog.query.filter_by(action="donor_merge").one()
        assert "Possible Duplicates" in entry.details and "'A'" in entry.details


class TestGuards:
    def test_unrelated_donors_cannot_be_merged_by_a_crafted_request(self, admin, app):
        from models import Donor
        x = _donor("X", "9111111111", "x@example.com")
        y = _donor("Y", "9222222222", "y@example.com")
        _give(x, 100)
        _give(y, 200)

        resp = _merge(admin, x, y)

        assert Donor.query.count() == 2
        assert "share a phone number or email, so they were not merged" in resp.get_data(as_text=True)

    def test_a_stale_group_is_refused_not_half_merged(self, admin, app):
        """Someone else already merged/removed one of the rows."""
        from models import Donor
        a, b = _donor("A", "6204999675"), _donor("B", "6204999675")
        _give(a, 5)
        resp = admin.post("/admin/donors/duplicates/merge",
                          data={"donor_ids": [str(a.id), "999999"]}, follow_redirects=True)
        assert Donor.query.count() == 2
        assert "already changed" in resp.get_data(as_text=True)

    def test_a_single_donor_is_not_a_group(self, admin, app):
        from models import Donor
        a = _donor("A", "6204999675")
        admin.post("/admin/donors/duplicates/merge", data={"donor_ids": [str(a.id)]})
        assert Donor.query.count() == 1

    def test_garbage_ids_are_a_400_not_a_crash(self, admin, app):
        resp = admin.post("/admin/donors/duplicates/merge", data={"donor_ids": ["abc", "1"]})
        assert resp.status_code == 400

    def test_staff_cannot_merge(self, client, app):
        from models import Donor
        a, b = _donor("A", "6204999675"), _donor("B", "6204999675")
        _give(a, 5)
        _give(b, 50)
        login(client, username="teststaff", password="TestPass123!")
        resp = _merge(client, a, b)
        assert Donor.query.count() == 2

    def test_logged_out_cannot_merge(self, client, app):
        from models import Donor
        a, b = _donor("A", "6204999675"), _donor("B", "6204999675")
        resp = client.post("/admin/donors/duplicates/merge",
                           data={"donor_ids": [str(a.id), str(b.id)]}, follow_redirects=False)
        assert resp.status_code == 302 and "/admin/login" in resp.headers["Location"]
        assert Donor.query.count() == 2


class TestTheOlderMergeBoxStillWorks:
    def test_merge_in_by_phone_uses_the_same_logic(self, admin, app):
        from models import Donor, Donation
        keeper = _donor("Keeper", "9000000001")
        dup = _donor("Dup", "9000000002", email="dup@example.com", city="Pune")
        _give(dup, 400)
        keeper_id = keeper.id

        resp = admin.post(f"/admin/donors/{keeper_id}/merge",
                          data={"duplicate_lookup": "9000000002"}, follow_redirects=True)

        assert resp.status_code == 200
        assert Donor.query.count() == 1
        kept = Donor.query.get(keeper_id)
        assert kept.email == "dup@example.com" and kept.city == "Pune"
        assert Donation.query.filter_by(donor_id=keeper_id).count() == 1
