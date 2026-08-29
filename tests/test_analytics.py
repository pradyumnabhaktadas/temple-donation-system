"""Integration tests for the Analytics page's period-over-period growth
badges and the new Campaign-wise Collections / Payment Mode breakdown
sections (added when the page was reworked for more insight + a visual
polish pass -- see admin.analytics()'s docstring for the overall design).

Donations here are seeded directly via the ORM rather than through the
public create_order + simulate-payment flow used elsewhere in the suite,
because these tests need exact control over `donation_date` to land rows
in specific "this period" vs "previous period" buckets -- the online flow
always stamps the donation with "now".

No dedicated test file for Analytics existed before this -- there was
previously only the blanket "every admin page renders" smoke test, which
never checked any of the numbers on the page were actually right.
"""
import datetime

from conftest import login


def _mk_donor(db, name, phone):
    from models import Donor
    donor = Donor(full_name=name, phone=phone)
    db.session.add(donor)
    db.session.flush()
    return donor


def _mk_donation(db, donor, campaign, amount, payment_mode, donation_date):
    from models import Donation
    donation = Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=amount,
        payment_mode=payment_mode, status="success", donation_date=donation_date,
    )
    db.session.add(donation)
    return donation


class TestCampaignAndPaymentModeBreakdown:
    def test_amounts_and_percentages_tie_out_to_the_period_total(self, app, client):
        """Rs. 1,000 (Annadan / cash) + Rs. 500 (BACE Contribution /
        online) = Rs. 1,500 for the period -- campaign_grand_total and
        payment_mode_grand_total are each computed from the same period
        rows as the Total Donations KPI, so both breakdowns' percentages
        should tie out to that Rs. 1,500 exactly."""
        from extensions import db
        from models import Campaign
        today = datetime.date.today()
        annadan = Campaign.query.filter_by(name="Annadan").first()
        bace = Campaign.query.filter_by(name="BACE Contribution").first()

        donor_a = _mk_donor(db, "Analytics Donor A", "9876500501")
        donor_b = _mk_donor(db, "Analytics Donor B", "9876500502")
        _mk_donation(db, donor_a, annadan, 1000, "cash",
                     datetime.datetime.combine(today, datetime.time(10, 0)))
        _mk_donation(db, donor_b, bace, 500, "online",
                     datetime.datetime.combine(today, datetime.time(11, 0)))
        db.session.commit()

        login(client)
        date_str = today.strftime("%Y-%m-%d")
        html = client.get(
            f"/admin/analytics?date_preset=custom&date_from={date_str}&date_to={date_str}"
        ).data.decode()

        assert "Rs. 1,000" in html
        assert "Rs. 500" in html
        assert "66.7%" in html  # Annadan's / cash's share of Rs. 1,500
        assert "33.3%" in html  # BACE Contribution's / online's share
        assert "Cash" in html
        assert "Online" in html
        assert 'id="campaignChart"' in html

    def test_empty_period_shows_the_no_data_message_not_a_crash(self, app, client):
        login(client)
        html = client.get("/admin/analytics?date_preset=custom&date_from=2020-01-01&date_to=2020-01-01").data.decode()
        assert "No successful donations in this population for the selected period." in html


class TestPeriodOverPeriodGrowthBadges:
    def test_revenue_and_active_donor_growth_vs_previous_period(self, app, client):
        """Previous period (yesterday, per this custom single-day range)
        has Rs. 400 from one active donor; this period has Rs. 1,500 from
        two. revenue_growth_pct = (1500-400)/400*100 = +275.0%.
        active_donor_growth_pct = (2-1)/1*100 = +100.0%."""
        from extensions import db
        from models import Campaign
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        annadan = Campaign.query.filter_by(name="Annadan").first()
        bace = Campaign.query.filter_by(name="BACE Contribution").first()

        donor_a = _mk_donor(db, "Growth Donor A", "9876500503")
        donor_b = _mk_donor(db, "Growth Donor B", "9876500504")
        _mk_donation(db, donor_a, annadan, 400, "cash",
                     datetime.datetime.combine(yesterday, datetime.time(9, 0)))
        _mk_donation(db, donor_a, annadan, 1000, "cash",
                     datetime.datetime.combine(today, datetime.time(10, 0)))
        _mk_donation(db, donor_b, bace, 500, "online",
                     datetime.datetime.combine(today, datetime.time(11, 0)))
        db.session.commit()

        login(client)
        date_str = today.strftime("%Y-%m-%d")
        html = client.get(
            f"/admin/analytics?date_preset=custom&date_from={date_str}&date_to={date_str}"
        ).data.decode()

        assert "+275.0% vs last period" in html
        assert "+100.0% vs last period" in html

    def test_no_previous_period_data_means_no_growth_badge(self, app, client):
        """No badge at all (not "0%" or an error) when there's nothing in
        the previous period to compare against -- e.g. the very first
        donation this donor population has ever had."""
        from extensions import db
        from models import Campaign
        today = datetime.date.today()
        annadan = Campaign.query.filter_by(name="Annadan").first()
        donor = _mk_donor(db, "Only Ever Donor", "9876500505")
        _mk_donation(db, donor, annadan, 750, "cash",
                     datetime.datetime.combine(today, datetime.time(10, 0)))
        db.session.commit()

        login(client)
        date_str = today.strftime("%Y-%m-%d")
        html = client.get(
            f"/admin/analytics?date_preset=custom&date_from={date_str}&date_to={date_str}"
        ).data.decode()
        assert "vs last period" not in html
