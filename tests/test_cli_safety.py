"""Tests for the guard that stops a script answering about the wrong database.

Written after check_payment_duplicates was run in the Shell of the
temple-zoho-reconcile cron job instead of the web service. That container has
no DATABASE_URL, config.py fell back to a local SQLite path, and create_app()
built the database empty on the spot. The script then reported, correctly and
uselessly:

    Checked 0 donation(s).
    No payment id appears on more than one donation.
    The unique constraint can be added safely.

Acting on that would have meant adding a unique constraint to a production
table nobody had looked at. The same run said "No forms to check" while a form
was in fact configured.

So these tests are about one thing: the scripts must refuse rather than
reassure, and must never print a password while doing it.
"""
import os
import re
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cli_safety import describe_database, looks_like_production, require_configured_database


def _app(uri):
    return types.SimpleNamespace(config={"SQLALCHEMY_DATABASE_URI": uri})


class TestNamingTheDatabaseWithoutLeakingTheCredential:
    """The output of these scripts gets pasted into chat."""

    def test_the_password_is_redacted(self):
        shown = describe_database(_app(
            "postgresql://temple_user:sup3rs3cret@dpg-abc123.oregon-postgres.render.com/temple"
        ))
        assert "sup3rs3cret" not in shown
        assert "***" in shown

    def test_the_useful_parts_survive_redaction(self):
        """A redacted string nobody can identify defeats the purpose --
        the whole point is that you can see which database it is."""
        shown = describe_database(_app(
            "postgresql://temple_user:pw@dpg-abc123.oregon-postgres.render.com/temple_db"
        ))
        assert "temple_user" in shown
        assert "dpg-abc123.oregon-postgres.render.com" in shown
        assert "temple_db" in shown

    def test_a_uri_with_no_password_is_left_readable(self):
        assert describe_database(_app("sqlite:////opt/render/project/src/instance/temple.db")) \
            == "sqlite:////opt/render/project/src/instance/temple.db"

    @pytest.mark.parametrize("password", [
        "p@ss/w@rd",          # the case that broke the first implementation
        "a@b@c@d",
        "plain",
        "with:colon",
        "sl/ash",
    ])
    def test_no_fragment_of_the_password_survives(self, password):
        """Asserted as "no part of it appears", not "the whole string is
        absent". The first implementation split on the leading '@' and
        printed 'u:***@ss/w@rd@host...' -- redacted-looking, with most of
        the password intact -- and an assertion phrased the loose way
        passed on it."""
        shown = describe_database(_app(f"postgresql://temple_user:{password}@host.example.com/db"))
        for fragment in re.split(r"[@:/]", password):
            if len(fragment) >= 2:
                assert fragment not in shown, f"{fragment!r} leaked from {password!r}"
        assert "***" in shown
        assert "host.example.com/db" in shown
        assert "temple_user" in shown

    def test_an_unidentifiable_username_is_hidden_rather_than_printed(self):
        """When the credentials can't be confidently located -- a "/" or a
        second "@" turning up inside what should be the username -- the
        safe move is to hide it. Printing something that might be half a
        secret is the failure this whole function exists to prevent."""
        shown = describe_database(_app("postgresql://weird/user:pw@host.example.com/db"))
        assert "pw" not in shown
        assert "weird/user" not in shown
        assert "***:***@host.example.com/db" in shown or shown.count("***") >= 1
        assert "host.example.com/db" in shown

    def test_a_second_at_sign_in_the_username_position_is_hidden_too(self):
        shown = describe_database(_app("postgresql://a@b:secret@host.example.com/db"))
        assert "secret" not in shown
        assert "a@b" not in shown

    def test_a_username_with_no_password_is_not_given_a_fake_one(self):
        shown = describe_database(_app("postgresql://temple_user@host.example.com/db"))
        assert shown == "postgresql://temple_user@host.example.com/db"

    def test_no_configured_uri_says_so_rather_than_crashing(self):
        assert describe_database(_app(None)) == "(none configured)"
        assert describe_database(types.SimpleNamespace(config={})) == "(none configured)"


class TestDecidingWhetherTheDatabaseCanBeTrusted:
    def test_an_unset_database_url_is_not_trusted(self, monkeypatch):
        """The exact condition from the incident."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert looks_like_production(_app("sqlite:///instance/temple.db")) is False

    def test_a_set_database_url_is_trusted(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
        assert looks_like_production(_app("postgresql://u:p@host/db")) is True

    def test_an_empty_database_url_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "   ")
        assert looks_like_production(_app("sqlite:///x.db")) is False

    def test_it_keys_on_the_env_var_not_the_uri_scheme(self, monkeypatch):
        """Someone genuinely running against SQLite on purpose, with
        DATABASE_URL set, is not the failure being guarded against -- and
        blocking them would be wrong."""
        monkeypatch.setenv("DATABASE_URL", "sqlite:////data/real.db")
        assert looks_like_production(_app("sqlite:////data/real.db")) is True


class TestRefusing:
    def test_it_returns_false_when_the_database_is_not_configured(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert require_configured_database(_app("sqlite:///instance/temple.db")) is False

    def test_it_returns_true_when_it_is(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
        assert require_configured_database(_app("postgresql://u:p@host/db")) is True

    def test_the_refusal_names_the_likely_cause(self, monkeypatch, capsys):
        """The two shells look identical. Saying "DATABASE_URL is unset"
        without saying "you are probably in the cron job's shell" leaves
        the reader exactly where they started."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        require_configured_database(_app("sqlite:///instance/temple.db"))
        out = capsys.readouterr().out
        assert "REFUSING" in out
        assert "cron" in out.lower()
        assert "WEB SERVICE" in out

    def test_it_always_prints_which_database_it_looked_at(self, monkeypatch, capsys):
        """Including on success -- so a wrong-database run is visible in
        the output even when nothing refuses."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:secret@host/db")
        require_configured_database(_app("postgresql://u:secret@host/db"))
        out = capsys.readouterr().out
        assert "Database:" in out and "host/db" in out
        assert "secret" not in out

    def test_the_purpose_is_quoted_back(self, monkeypatch, capsys):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        require_configured_database(_app("sqlite:///x.db"), purpose="do the thing")
        assert "do the thing" in capsys.readouterr().out


class TestTheScriptsActuallyUseIt:
    """A guard nothing calls is decoration."""

    def test_the_duplicate_check_refuses_without_a_database(self, monkeypatch, capsys):
        import check_payment_duplicates
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert check_payment_duplicates.main() == 2
        assert "REFUSING" in capsys.readouterr().out

    def test_it_does_not_report_a_verdict_when_it_refuses(self, monkeypatch, capsys):
        """The whole failure was a reassuring verdict. It must not appear."""
        import check_payment_duplicates
        monkeypatch.delenv("DATABASE_URL", raising=False)
        check_payment_duplicates.main()
        out = capsys.readouterr().out
        assert "can be added safely" not in out
        assert "Checked 0 donation(s)" not in out
