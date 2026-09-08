"""Refuse to answer questions about production from a database that isn't it.

This exists because of a real, nearly-costly incident. check_payment_duplicates
was run in the Render Shell of the *temple-zoho-reconcile* service -- the cron
job -- rather than the web service. That container deliberately carries almost
no environment: zoho_reconcile.py is a thin HTTP client that POSTs to the web
app precisely because the cron container's own networking and env are minimal.
So DATABASE_URL was unset, config.py fell back to a local SQLite path, and
create_app() -- which calls create_all() outside production -- helpfully built
an empty database on the spot.

The script then did its job perfectly and reported:

    Checked 0 donation(s).
    No payment id appears on more than one donation.
    The unique constraint can be added safely.

Every word true, about the wrong database. Acting on it would have meant
adding a unique constraint to a production table nobody had checked. The same
run reported "No forms to check", which reads like no Zoho form is configured
when in fact one is.

A wrong answer that looks like a right answer is worse than a crash. So any
script whose output is used to make a decision about production data calls
require_configured_database() first, and says out loud which database it is
looking at.
"""
import os
import re


def describe_database(app):
    """The database this app is pointed at, safe to print.

    Passwords are stripped: these scripts are run in a web shell and their
    output gets pasted into chat and issue trackers.
    """
    uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    if not uri:
        return "(none configured)"

    scheme, sep, rest = uri.partition("://")
    if not sep:
        return uri

    # Everything before a "?" or "#" is where credentials can live. A "/" is
    # deliberately NOT treated as a boundary: per RFC it ends the authority,
    # but real DATABASE_URLs get pasted with unencoded "/" inside the
    # password, and honouring the spec there made this return the URI
    # untouched -- password and all.
    cut = len(rest)
    for char in "?#":
        found = rest.find(char)
        if found != -1:
            cut = min(cut, found)

    # The LAST "@" before that, not the first. Generated passwords contain
    # "@" -- Render's do -- and splitting on the first one yields
    # "u:***@ss/w@rd@host..." : redacted-looking, with most of the password
    # still printed. That is worse than not redacting, because it invites
    # the output to be pasted somewhere.
    last_at = rest[:cut].rfind("@")
    if last_at == -1:
        return uri

    credentials, host = rest[:last_at], rest[last_at + 1:]
    if ":" not in credentials:
        # "user@host", or an "@" inside a file path. No password present, so
        # nothing to hide and no reason to mangle it.
        return uri

    user = credentials.split(":", 1)[0]
    if "/" in user or "@" in user:
        # Couldn't confidently tell where the credentials start. Hide the
        # lot rather than print something that might be half a secret.
        user = "***"
    return f"{scheme}://{user}:***@{host}"


def looks_like_production(app):
    """Whether this is a real, externally-configured database.

    Deliberately keyed on DATABASE_URL being set rather than on the URI
    scheme. A missing DATABASE_URL is the precise condition that caused the
    incident above: it is what makes config.py fall back, and it is what
    distinguishes "I am in the web service" from "I am somewhere that just
    invented a database for me".
    """
    return bool((os.environ.get("DATABASE_URL") or "").strip())


def require_configured_database(app, purpose="check production data"):
    """Prints the target database. Returns True if it can be trusted.

    On failure it explains what almost certainly happened, because the
    failure mode is a shell that looks identical to the right one.
    """
    print(f"Database: {describe_database(app)}")

    if looks_like_production(app):
        return True

    print()
    print("REFUSING TO CONTINUE -- DATABASE_URL is not set here.")
    print()
    print(f"Without it this app falls back to a local SQLite file and creates it")
    print(f"empty, so anything reported would be about that file rather than the")
    print(f"real data. This script exists to {purpose}, and a confident answer")
    print("from the wrong database is worse than no answer at all.")
    print()
    print("Most likely cause: this is the Shell of a background worker or cron")
    print("job (e.g. temple-zoho-reconcile), which deliberately carries almost no")
    print("environment. Open the Shell for the WEB SERVICE instead -- the one")
    print("with DATABASE_URL set -- and run this again there.")
    return False
