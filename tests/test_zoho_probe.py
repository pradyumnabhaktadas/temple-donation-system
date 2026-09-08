"""Tests for zoho_probe --diagnose.

This script exists to answer questions that can only be settled against a
live Zoho account, and its answers decide how the sync is built. So the
parts that don't need the network -- how it reads ordering, how it pages,
what it concludes -- are tested here, because a probe that reports
"newest-first" when the account is oldest-first would send the design in
exactly the wrong direction while looking authoritative.

The specific thing being guarded: the reconciler reads a bounded number of
pages. If Zoho returns oldest entries first, a form with more entries than
that bound never has its recent payments matched, and the message it
produces ("Zoho has no entry carrying this transaction id") reads like the
entry doesn't exist rather than like we stopped looking. The probe is what
tells us whether that is happening.
"""
import datetime
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import zoho_probe


def _entry(added, payment_id=None, name="Jatin, saini", phone="9650150283"):
    """One entry in the shape this account's forms return."""
    entry = {
        "Name": name,
        "Phone": phone,
        "Payment Amount": "100",
        "Payment Status": "Completed",
        "Added Time": added,
    }
    if payment_id:
        entry["Payment Transaction ID"] = (
            f"Txn ID : {payment_id} Order ID : {payment_id.replace('pay_', 'order_')}"
        )
    return entry


def _t(day):
    return f"{day:02d}-Sep-2026 21:55:22"


def _api(pages):
    """Stands in for zoho_api.entries(). `pages` is the full ordered list
    of records; this serves slices of it the way Zoho's from/limit
    pagination does."""
    def _entries(config, form, start_index=1, limit=200):
        return pages[start_index - 1:start_index - 1 + limit], "records"
    return patch("zoho_api.entries", side_effect=_entries)


class TestReadingTheOrdering:
    """The single most consequential thing the probe reports."""

    def test_newest_first_is_recognised(self):
        times = [datetime.datetime(2026, 9, d) for d in (10, 9, 8, 7)]
        order, _why = zoho_probe._describe_order(times)
        assert order == "newest-first"

    def test_oldest_first_is_recognised(self):
        times = [datetime.datetime(2026, 9, d) for d in (7, 8, 9, 10)]
        order, _why = zoho_probe._describe_order(times)
        assert order == "oldest-first"

    def test_an_unsorted_response_is_not_reported_as_either(self):
        """Reporting a guess here is worse than reporting nothing: the
        whole point of the probe is to stop the design resting on an
        assumption."""
        times = [datetime.datetime(2026, 9, d) for d in (8, 10, 7, 9)]
        order, _why = zoho_probe._describe_order(times)
        assert order == "unsorted"

    def test_identical_timestamps_are_admitted_as_unknown(self):
        """Bulk-imported entries can share a timestamp. Ascending and
        descending are both technically true, which tells us nothing."""
        same = [datetime.datetime(2026, 9, 8)] * 5
        order, why = zoho_probe._describe_order(same)
        assert order == "unknown" and "identical" in why

    def test_too_few_timestamps_is_unknown_not_a_guess(self):
        assert zoho_probe._describe_order([])[0] == "unknown"
        assert zoho_probe._describe_order([datetime.datetime(2026, 9, 8)])[0] == "unknown"

    def test_unparseable_timestamps_do_not_shift_the_others(self):
        """None is kept in place rather than dropped, so a record with a
        broken timestamp can't make an ascending list look unsorted."""
        times = [datetime.datetime(2026, 9, 10), None, datetime.datetime(2026, 9, 9)]
        assert zoho_probe._describe_order(times)[0] == "newest-first"

    def test_it_reads_real_added_time_strings(self):
        """Zoho renders these as "04-Sep-2026 21:55:22". If the probe
        can't parse that it reports "unknown" and we learn nothing."""
        records = [_entry(_t(d)) for d in (10, 9, 8)]
        times = zoho_probe._entry_times(records)
        assert all(t is not None for t in times)
        assert zoho_probe._describe_order(times)[0] == "newest-first"


class TestCountingEntries:
    """Whether the ordering matters at all depends on how many entries a
    form holds. Under the crawl's bound, oldest-first is harmless."""

    def test_a_short_form_is_counted_exactly(self, app):
        records = [_entry(_t(1 + i % 28)) for i in range(3)]
        with _api(records):
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert result["total"] == 3 and not result["hit_cap"]

    def test_it_pages_past_the_first_page(self, app):
        records = [_entry(_t(1 + i % 28)) for i in range(450)]
        with _api(records):
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert result["total"] == 450

    def test_a_form_larger_than_the_probes_own_bound_says_so(self, app):
        """It must not report a confident total it didn't actually reach."""
        records = [_entry(_t(1 + i % 28))
                   for i in range(zoho_probe.DIAGNOSE_PAGE * zoho_probe.DIAGNOSE_MAX_PAGES + 10)]
        with _api(records):
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert result["hit_cap"] is True

    def test_shallow_reads_one_page_only(self, app):
        records = [_entry(_t(1 + i % 28)) for i in range(450)]
        with _api(records) as api:
            zoho_probe._diagnose_form(app.config, "EBG", deep=False)
        assert api.call_count == 1

    def test_paging_terminates_when_the_api_keeps_returning_full_pages(self, app):
        """A misbehaving endpoint that always answers must not spin
        forever inside a read-only diagnostic."""
        page = [_entry(_t(8)) for _ in range(zoho_probe.DIAGNOSE_PAGE)]
        with patch("zoho_api.entries", side_effect=lambda *a, **k: (page, "records")) as api:
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert api.call_count <= zoho_probe.DIAGNOSE_MAX_PAGES
        assert result["hit_cap"] is True


class TestExtractingTransactionIds:
    """Every test of this parser so far has run on payloads I wrote. The
    probe is what checks it against the account's real entries."""

    def test_it_finds_ids_in_the_combined_txn_string(self, app):
        records = [_entry(_t(10), "pay_TYkwQc4pqV9HNu"), _entry(_t(9), "pay_TYkDaxITUonK1O")]
        with _api(records):
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert result["ids_on_first_page"] == 2

    def test_entries_without_a_payment_are_not_counted(self, app):
        """Cash registrations and abandoned forms. Expected, not a fault."""
        records = [_entry(_t(10), "pay_Real1"), _entry(_t(9)), _entry(_t(8))]
        with _api(records):
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert result["ids_on_first_page"] == 1

    def test_finding_none_is_reported_rather_than_passed_over(self, app, capsys):
        """If the parser finds nothing on real data, that is the single
        most important thing the probe can tell us."""
        with _api([_entry(_t(10)), _entry(_t(9))]):
            zoho_probe._diagnose_form(app.config, "EBG")
        assert "NONE" in capsys.readouterr().out


class TestTheVerdict:
    """The probe's output is read by a person deciding what to build, so
    the conclusion has to be stated, not left to be inferred."""

    def test_oldest_first_and_large_is_flagged_as_action_needed(self, app, capsys):
        records = [_entry(_t(1 + i % 28)) for i in range(1200)]
        records.sort(key=lambda r: r["Added Time"])   # oldest first
        with _api(records):
            zoho_probe._diagnose_form(app.config, "EBG")
        out = capsys.readouterr().out
        assert "ACTION NEEDED" in out

    def test_oldest_first_but_small_says_nothing_is_being_missed_yet(self, app, capsys):
        records = [_entry(f"{d:02d}-Sep-2026 10:00:00") for d in range(1, 20)]
        with _api(records):
            zoho_probe._diagnose_form(app.config, "EBG")
        out = capsys.readouterr().out
        assert "ACTION NEEDED" not in out
        assert "nothing is being missed today" in out

    def test_newest_first_is_reported_as_safe(self, app, capsys):
        records = [_entry(f"{d:02d}-Sep-2026 10:00:00") for d in range(20, 1, -1)]
        with _api(records):
            zoho_probe._diagnose_form(app.config, "EBG")
        assert "is safe for this form" in capsys.readouterr().out

    def test_an_undetermined_order_is_treated_as_unsafe(self, app, capsys):
        records = [_entry(f"{d:02d}-Sep-2026 10:00:00") for d in (8, 10, 7, 9)]
        with _api(records):
            zoho_probe._diagnose_form(app.config, "EBG")
        assert "unsafe" in capsys.readouterr().out


class TestFailingSafely:
    """It runs against production credentials. It must never write, and
    must never die halfway through leaving a partial impression."""

    def test_an_unreachable_form_is_reported_not_raised(self, app, capsys):
        import zoho_api
        with patch("zoho_api.entries", side_effect=zoho_api.ZohoApiError("bad token")):
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert result["reachable"] is False
        assert "UNREACHABLE" in capsys.readouterr().out

    def test_one_unreachable_form_does_not_stop_the_others(self, app):
        """Six forms are checked in one run; the third failing must not
        hide what the other five would have said."""
        import zoho_api
        calls = {"n": 0}

        def _flaky(config, form, start_index=1, limit=200):
            calls["n"] += 1
            if form == "BROKEN":
                raise zoho_api.ZohoApiError("nope")
            return [_entry(_t(10), "pay_Ok1")], "records"

        with patch("zoho_api.entries", side_effect=_flaky):
            results = [zoho_probe._diagnose_form(app.config, f)
                       for f in ("GOOD1", "BROKEN", "GOOD2")]

        assert [r["reachable"] for r in results] == [True, False, True]

    def test_an_empty_form_is_not_an_error(self, app):
        with _api([]):
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert result["reachable"] is True and result["total"] == 0

    def test_a_page_failing_midway_keeps_the_count_it_reached(self, app):
        """Better to report "at least 200" than to throw away the page we
        did read."""
        import zoho_api
        page = [_entry(_t(8)) for _ in range(zoho_probe.DIAGNOSE_PAGE)]
        calls = {"n": 0}

        def _dies_on_second(config, form, start_index=1, limit=200):
            calls["n"] += 1
            if calls["n"] > 1:
                raise zoho_api.ZohoApiError("rate limited")
            return page, "records"

        with patch("zoho_api.entries", side_effect=_dies_on_second):
            result = zoho_probe._diagnose_form(app.config, "EBG")

        assert result["total"] == zoho_probe.DIAGNOSE_PAGE
        assert result["reachable"] is True

    def test_a_garbage_record_does_not_break_the_run(self, app):
        """Zoho's shape differs by API version; the probe exists precisely
        for the case where it isn't what we expect."""
        with _api([{"unexpected": None}, {"Added Time": "not a date"}, 12345]):
            result = zoho_probe._diagnose_form(app.config, "EBG")
        assert result["reachable"] is True


class TestChoosingWhichFormsToCheck:
    def test_an_explicit_form_wins(self, app):
        assert zoho_probe._forms_to_diagnose(app.config, "EBG") == ["EBG"]

    def test_otherwise_it_uses_the_configured_forms(self, app):
        """So it can be run with no arguments and check everything that
        actually takes money."""
        from extensions import db
        from models import Campaign, ZohoForm
        campaign = Campaign.query.first()
        db.session.add(ZohoForm(form_key="EssenceofBhagavadGita", campaign_id=campaign.id))
        db.session.add(ZohoForm(form_key="IGFForm", link_name="IGF_Registration",
                                campaign_id=campaign.id))
        db.session.add(ZohoForm(form_key="Retired", campaign_id=campaign.id, is_active=False))
        db.session.commit()

        names = zoho_probe._forms_to_diagnose(app.config, None)

        assert "EssenceofBhagavadGita" in names
        assert "IGF_Registration" in names      # link_name wins over form_key
        assert "Retired" not in names           # inactive forms skipped
