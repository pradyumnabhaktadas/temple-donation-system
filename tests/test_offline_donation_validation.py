"""Malformed offline submissions must be rejected before any record/PDF work."""

import pytest

from conftest import login
from models import Campaign, Donation


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "10000000000"])
def test_manual_donation_rejects_non_positive_non_finite_and_oversized_amounts(app, client, amount):
    login(client)
    campaign = Campaign.query.filter_by(name="Annadan").one()

    response = client.post(
        "/admin/donations/manual",
        data={
            "campaign_id": str(campaign.id),
            "amount": amount,
            "full_name": "Amount Validation Donor",
            "phone": "9876543210",
            "payment_mode": "cash",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert Donation.query.count() == 0


def test_manual_donation_rejects_a_deleted_campaign_without_an_error_page(app, client):
    login(client)

    response = client.post(
        "/admin/donations/manual",
        data={
            "campaign_id": "999999",
            "amount": "501",
            "full_name": "Campaign Validation Donor",
            "phone": "9876543210",
            "payment_mode": "cash",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert Donation.query.count() == 0
