"""Tests for the Zoho Forms API client (zoho_api.py).

Everything here runs offline. The point of isolating Zoho's HTTP surface
in one module is that the logic around it can be tested without a live
account, and that the parts which genuinely need one are small and
obvious.

The response-envelope handling gets the most attention because its
failure mode is the dangerous kind: if it doesn't find the records, it
returns an empty list, and "no records" is indistinguishable from "no new
donations". That's the same silent-nothing failure that let Zoho's
webhook lose donations for weeks, so it fails loudly or not at all.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import zoho_api


RECORD = {"Name": "Jatin", "Payment Transaction ID": "Txn ID : pay_X Order ID : order_Y"}


@pytest.fixture(autouse=True)
def _clear_token_cache():
    zoho_api._token_cache.update({"access_token": None, "expires_at": 0.0})
    yield
    zoho_api._token_cache.update({"access_token": None, "expires_at": 0.0})


CONFIG = {
    "ZOHO_CLIENT_ID": "cid",
    "ZOHO_CLIENT_SECRET": "secret",
    "ZOHO_REFRESH_TOKEN": "refresh",
    "ZOHO_ACCOUNTS_BASE": "https://accounts.zoho.in",
    "ZOHO_API_BASE": "https://forms.zoho.in",
}


def _response(status=200, json_body=None, text="", raise_on_json=False):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    if raise_on_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_body if json_body is not None else {}
    return resp


class TestEnvelopeHandling:
    """Zoho wraps records differently by endpoint and API version, and a
    wrong guess returns zero records -- which reads as "nothing new"
    rather than "I couldn't find the data"."""

    @pytest.mark.parametrize("body,expected_key", [
        ({"records": [RECORD]}, "records"),
        ({"data": [RECORD]}, "data"),
        ({"entries": [RECORD]}, "entries"),
        ({"response": {"records": [RECORD]}}, "response.records"),
        ([RECORD], "(top-level list)"),
        ({"someUnexpectedKey": [RECORD]}, "someUnexpectedKey"),
    ])
    def test_it_finds_the_records_whatever_they_are_wrapped_in(self, body, expected_key):
        records, key = zoho_api._records_from(body)
        assert records == [RECORD]
        assert key == expected_key

    def test_an_error_body_yields_nothing_rather_than_nonsense(self):
        records, key = zoho_api._records_from({"code": 401, "message": "invalid oauth"})
        assert records == [] and key is None

    def test_a_non_object_yields_nothing(self):
        assert zoho_api._records_from("boom") == ([], None)

    def test_an_empty_result_still_reports_which_key_it_read(self):
        """Distinguishes "the form has no entries" from "I couldn't find
        the records" -- the probe prints this, and they need telling
        apart."""
        records, key = zoho_api._records_from({"records": []})
        assert records == [] and key == "records"


class TestEntriesEndpoint:
    def test_entries_uses_the_forms_entries_endpoint(self):
        """A wrong endpoint returns a 404 HTML page, not an empty form."""
        with patch("zoho_api.get", return_value={"entries": [RECORD]}) as get:
            records, key = zoho_api.entries(CONFIG, "DonationForm", start_index=21, limit=50)

        assert records == [RECORD]
        assert key == "entries"
        get.assert_called_once_with(
            CONFIG,
            "/api/v1/form/DonationForm/entries",
            params={"from": 21, "limit": 50},
        )


class TestConfiguration:
    def test_missing_credentials_say_which_ones(self):
        with pytest.raises(zoho_api.ZohoApiError) as exc:
            zoho_api._token({"ZOHO_CLIENT_ID": "cid"})
        assert "ZOHO_CLIENT_SECRET" in str(exc.value)
        assert "ZOHO_REFRESH_TOKEN" in str(exc.value)

    def test_a_rejected_refresh_token_points_at_the_data_centre(self):
        """The commonest cause of this error is the wrong regional domain,
        and it presents identically to a bad credential -- so the message
        says so rather than sending someone to re-issue working keys."""
        with patch("zoho_api.requests.post", return_value=_response(json_body={"error": "invalid_code"})):
            with pytest.raises(zoho_api.ZohoApiError) as exc:
                zoho_api._token(CONFIG)
        assert "data centre" in str(exc.value)


class TestTokenHandling:
    def test_a_token_is_reused_rather_than_refetched(self):
        post = MagicMock(return_value=_response(json_body={"access_token": "tok", "expires_in": 3600}))
        with patch("zoho_api.requests.post", post):
            assert zoho_api._token(CONFIG) == "tok"
            assert zoho_api._token(CONFIG) == "tok"
        assert post.call_count == 1, "the second call must come from cache"

    def test_a_token_is_retired_before_zoho_actually_expires_it(self):
        """The cached lifetime is deliberately shorter than the real one,
        so a long sync can't have a token expire underneath it mid-run and
        fail a request that would have worked a second earlier."""
        import time as time_mod

        post = MagicMock(return_value=_response(json_body={"access_token": "tok", "expires_in": 3600}))
        before = time_mod.time()
        with patch("zoho_api.requests.post", post):
            zoho_api._token(CONFIG)

        cached_for = zoho_api._token_cache["expires_at"] - before
        assert cached_for < 3600, "must retire before Zoho's own expiry"
        assert cached_for == pytest.approx(3600 - zoho_api._TOKEN_TTL_MARGIN_SECONDS, abs=5)

    def test_an_implausibly_short_token_does_not_cause_thrashing(self):
        """If the margin were applied blindly, a short-lived token would
        give a non-positive lifetime and every single API call would then
        trigger its own token exchange -- straight into Zoho's rate
        limits. Floored instead."""
        import time as time_mod

        post = MagicMock(return_value=_response(json_body={"access_token": "tok", "expires_in": 60}))
        before = time_mod.time()
        with patch("zoho_api.requests.post", post):
            zoho_api._token(CONFIG)
            zoho_api._token(CONFIG)

        assert zoho_api._token_cache["expires_at"] > before, "must not be already-expired"
        assert post.call_count == 1, "the second call must still come from cache"

    def test_a_401_is_retried_once_with_a_fresh_token(self):
        post = MagicMock(return_value=_response(json_body={"access_token": "tok", "expires_in": 3600}))
        get = MagicMock(side_effect=[
            _response(status=401, text="expired"),
            _response(json_body={"records": [RECORD]}),
        ])
        with patch("zoho_api.requests.post", post), patch("zoho_api.requests.get", get):
            body = zoho_api.get(CONFIG, "/api/x/records")
        assert body == {"records": [RECORD]}
        assert get.call_count == 2

    def test_a_persistent_401_gives_up_rather_than_looping(self):
        post = MagicMock(return_value=_response(json_body={"access_token": "tok", "expires_in": 3600}))
        get = MagicMock(return_value=_response(status=401, text="revoked"))
        with patch("zoho_api.requests.post", post), patch("zoho_api.requests.get", get):
            with pytest.raises(zoho_api.ZohoApiError):
                zoho_api.get(CONFIG, "/api/x/records")
        assert get.call_count == 2, "one retry, not an infinite loop"


class TestErrorsAreLoud:
    """Every failure here must raise. A sync that quietly treats an API
    failure as "no entries" is the exact silent-loss pattern this whole
    piece of work exists to remove."""

    def test_an_http_error_raises(self):
        post = MagicMock(return_value=_response(json_body={"access_token": "tok", "expires_in": 3600}))
        with patch("zoho_api.requests.post", post), \
             patch("zoho_api.requests.get", return_value=_response(status=500, text="boom")):
            with pytest.raises(zoho_api.ZohoApiError):
                zoho_api.get(CONFIG, "/api/x/records")

    def test_a_non_json_response_raises(self):
        post = MagicMock(return_value=_response(json_body={"access_token": "tok", "expires_in": 3600}))
        with patch("zoho_api.requests.post", post), \
             patch("zoho_api.requests.get", return_value=_response(raise_on_json=True, text="<html>")):
            with pytest.raises(zoho_api.ZohoApiError):
                zoho_api.get(CONFIG, "/api/x/records")

    def test_an_unreachable_host_raises(self):
        import requests as real_requests
        post = MagicMock(return_value=_response(json_body={"access_token": "tok", "expires_in": 3600}))
        with patch("zoho_api.requests.post", post), \
             patch("zoho_api.requests.get", side_effect=real_requests.RequestException("dns")):
            with pytest.raises(zoho_api.ZohoApiError):
                zoho_api.get(CONFIG, "/api/x/records")
