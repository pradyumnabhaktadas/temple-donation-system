"""Thin client for the Zoho Forms API.

Deliberately small and boring. Everything this module knows about Zoho's
HTTP surface -- token exchange, base URLs, how a page of entries is
requested -- is here and nowhere else, so the parts that can only be
confirmed against a live account are confined to one file. The logic that
turns entries into donations (zoho_sync.py) works on plain dicts and can
be tested without touching the network.

WHY PULL INSTEAD OF BEING PUSHED TO
-----------------------------------
Zoho's webhook fires once, at submission time, *before* the donor has
paid, so it carries no transaction ID -- and on this account it often
never calls again. It also has to be configured by hand on every form:
of the six forms that took money in September 2026, four had never been
configured, so those donations were invisible to this app entirely and
only surfaced by reconciling Razorpay's own records weeks later.

A Zoho entry holds everything needed on its own: donor details, amount,
payment status, and the Razorpay transaction ID. Reading entries removes
the per-form setup, picks up forms added later without anyone remembering
to wire them up, and doesn't depend on Zoho's webhook working at all.

AUTHENTICATION
--------------
OAuth 2.0, refresh-token grant (Zoho API Console -> Self Client). The
refresh token is the stored credential; access tokens are short-lived,
fetched on demand, cached in memory for the life of the process, and
never written anywhere.

DATA CENTRES
------------
Zoho serves separate domains per data centre (.in, .com, .eu, ...) and an
account only answers on its own. Using the wrong one fails as an auth
error, which reads like bad credentials and sends you looking in the
wrong place -- hence ZOHO_ACCOUNTS_BASE / ZOHO_API_BASE being configurable
and the explicit hint in _token()'s error.

ON THE RESPONSE SHAPE
---------------------
Field names in a Zoho entry are per-form (they follow the form's own
labels), and the exact envelope differs by API version. Nothing here
assumes a particular shape beyond "a JSON object, somewhere inside which
is a list of records" -- see entries(), which is deliberately forgiving,
and zoho_probe.py, which prints raw responses so the mapping can be built
against what the account actually returns rather than what documentation
suggests it should.
"""
import time

import requests

# Access tokens last an hour; refreshed a little early so a long sync
# can't have one expire underneath it mid-run.
_TOKEN_TTL_MARGIN_SECONDS = 300

_token_cache = {"access_token": None, "expires_at": 0.0}


class ZohoApiError(RuntimeError):
    """Anything that went wrong talking to Zoho. Carries a message meant
    to be shown to whoever is running the sync, not a stack trace."""


def _require(config, *keys):
    missing = [k for k in keys if not (config.get(k) or "").strip()]
    if missing:
        raise ZohoApiError(
            "Zoho API isn't configured -- missing: " + ", ".join(missing)
            + ". See config.py's ZOHO_CLIENT_ID block and the README."
        )


# Whoever is waiting decides how long to wait. A background sweep can
# afford a minute; a web request cannot -- gunicorn's worker timeout on
# this deployment is 30 seconds, so a diagnostics page using the default
# would 502 rather than report that Zoho is slow, which is the one thing
# it exists to report.
DEFAULT_TOKEN_TIMEOUT = 30
DEFAULT_READ_TIMEOUT = 60


def _token(config, force_refresh=False, timeout=DEFAULT_TOKEN_TIMEOUT):
    """Returns a valid access token, exchanging the refresh token when the
    cached one is missing or close to expiry."""
    _require(config, "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")

    now = time.time()
    if not force_refresh and _token_cache["access_token"] and _token_cache["expires_at"] > now:
        return _token_cache["access_token"]

    url = config["ZOHO_ACCOUNTS_BASE"].rstrip("/") + "/oauth/v2/token"
    try:
        resp = requests.post(url, params={
            "refresh_token": config["ZOHO_REFRESH_TOKEN"],
            "client_id": config["ZOHO_CLIENT_ID"],
            "client_secret": config["ZOHO_CLIENT_SECRET"],
            "grant_type": "refresh_token",
        }, timeout=timeout)
    except requests.RequestException as exc:
        raise ZohoApiError(f"Couldn't reach Zoho accounts at {url}: {exc}") from exc

    try:
        body = resp.json()
    except ValueError:
        raise ZohoApiError(
            f"Zoho accounts returned {resp.status_code} and not JSON: {resp.text[:200]}"
        ) from None

    access_token = body.get("access_token")
    if not access_token:
        raise ZohoApiError(
            f"Zoho refused the refresh token ({body.get('error') or resp.status_code}). "
            "If the credentials are definitely right, check ZOHO_ACCOUNTS_BASE matches the "
            "data centre this account lives in (.in / .com / .eu) -- the wrong domain fails "
            "exactly like a bad credential."
        )

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + max(
        int(body.get("expires_in") or 3600) - _TOKEN_TTL_MARGIN_SECONDS, 60
    )
    return access_token


def get(config, path, params=None, _retried=False, timeout=None):
    """One authenticated GET against the Forms API. Returns parsed JSON.

    Retries once on a 401 with a freshly minted token, since the usual
    cause is a cached token that expired mid-run -- worth one silent
    retry, but only one, so a genuinely revoked credential still fails
    loudly instead of looping."""
    url = config["ZOHO_API_BASE"].rstrip("/") + "/" + path.lstrip("/")
    read_timeout = DEFAULT_READ_TIMEOUT if timeout is None else timeout
    token = _token(
        config, force_refresh=_retried,
        timeout=DEFAULT_TOKEN_TIMEOUT if timeout is None else timeout,
    )

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params=params or {},
            timeout=read_timeout,
        )
    except requests.RequestException as exc:
        raise ZohoApiError(f"Couldn't reach Zoho at {url}: {exc}") from exc

    if resp.status_code == 401 and not _retried:
        return get(config, path, params=params, _retried=True, timeout=timeout)

    if resp.status_code >= 400:
        raise ZohoApiError(f"Zoho returned {resp.status_code} for {url}: {resp.text[:300]}")

    try:
        return resp.json()
    except ValueError:
        raise ZohoApiError(f"Zoho returned non-JSON for {url}: {resp.text[:200]}") from None


def _records_from(body):
    """Digs the list of records out of a response envelope.

    Zoho wraps records under different keys depending on the endpoint and
    API version. Rather than hardcode one and break silently against a
    different account or a later version -- returning zero entries, which
    would look exactly like "no new donations" -- this takes the first
    list-of-dicts it finds among the likely keys, then falls back to any
    list of dicts in the body.

    Returns (records, envelope_key) so callers can report which shape was
    actually seen, instead of leaving it a mystery."""
    if isinstance(body, list):
        return body, "(top-level list)"
    if not isinstance(body, dict):
        return [], None

    for key in ("records", "data", "entries", "result", "response"):
        value = body.get(key)
        if isinstance(value, list) and (not value or isinstance(value[0], dict)):
            return value, key
        # One level of nesting, e.g. {"response": {"records": [...]}}
        if isinstance(value, dict):
            inner, inner_key = _records_from(value)
            if inner:
                return inner, f"{key}.{inner_key}"

    for key, value in body.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value, key

    return [], None


def entries(config, form_link_name, start_index=1, limit=200, timeout=None):
    """One page of entries for a form. Returns (records, envelope_key).

    start_index is 1-based, matching Zoho's own convention. The caller
    pages; this deliberately doesn't, so that a paging bug can't hide
    inside the network layer where it's hardest to test."""
    body = get(
        config,
        f"/api/{form_link_name}/records",
        params={"from": start_index, "limit": limit},
        timeout=timeout,
    )
    return _records_from(body)
