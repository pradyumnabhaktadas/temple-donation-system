import datetime

from conftest import login


def _add_donation(app, donor, campaign, amount, donated_at):
    from extensions import db
    from models import Donation
    db.session.add(Donation(
        donor_id=donor.id, campaign_id=campaign.id, amount=amount,
        payment_mode="cash", status="success", donation_date=donated_at,
    ))
    db.session.commit()


def test_intelligence_identifies_last_month_donor_missing_this_month(client, app):
    login(client)
    from extensions import db
    from models import Donor, Campaign
    from utils import now_ist
    donor = Donor(full_name="Follow-up Donor", phone="9319880507")
    db.session.add(donor)
    db.session.commit()
    campaign = Campaign.query.filter_by(name="Annadan").one()
    first_of_this_month = now_ist().date().replace(day=1)
    last_month = first_of_this_month - datetime.timedelta(days=1)
    _add_donation(app, donor, campaign, 2500, datetime.datetime.combine(last_month, datetime.time(12)))

    response = client.get("/admin/donor-intelligence")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Follow-up Donor" in body
    assert "Priority follow-up" in body


def test_intelligence_marks_a_first_gift_this_month_as_new(client, app):
    login(client)
    from extensions import db
    from models import Donor, Campaign
    donor = Donor(full_name="New Intelligence Donor", phone="9319880508")
    db.session.add(donor)
    db.session.commit()
    campaign = Campaign.query.filter_by(name="Annadan").one()
    _add_donation(app, donor, campaign, 1500, datetime.datetime.utcnow())

    response = client.get("/admin/donor-intelligence")
    assert "New Intelligence Donor" in response.get_data(as_text=True)
