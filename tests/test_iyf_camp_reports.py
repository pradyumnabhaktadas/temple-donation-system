"""The IYF Camps Collections tab and its CSV exports."""
import csv
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _seed(client, rows):
    """rows: (camp, amount, YYYY-MM-DD)."""
    for camp in {r[0] for r in rows}:
        client.post("/admin/iyf-camps/manage", data={"name": camp}, follow_redirects=True)
    for camp, amount, date in rows:
        client.post("/admin/iyf-camps/single", data={
            "camp_name": camp, "full_name": f"Student {amount}",
            "amount": str(amount), "donation_date": date,
        }, follow_redirects=True)


def _csv_rows(resp):
    return list(csv.reader(io.StringIO(resp.data.decode())))


class TestCollectionsPage:
    def test_loads_empty(self, app, client):
        login(client)
        resp = client.get("/admin/iyf-camps/collections")
        assert resp.status_code == 200
        assert b"No camp collections recorded" in resp.data

    def test_camp_and_monthly_totals(self, app, client):
        login(client)
        _seed(client, [
            ("Camp A", 100, "2026-07-05"),
            ("Camp A", 250, "2026-08-05"),
            ("Camp B", 400, "2026-08-06"),
        ])
        html = client.get("/admin/iyf-camps/collections").data.decode()
        assert "Camp A" in html and "Camp B" in html
        assert "Jul 2026" in html and "Aug 2026" in html
        assert "750" in html          # grand total across both panels

    def test_filter_by_camp(self, app, client):
        login(client)
        _seed(client, [("Camp A", 100, "2026-08-05"), ("Camp B", 400, "2026-08-06")])
        html = client.get("/admin/iyf-camps/collections?camp=Camp+A").data.decode()
        # The dropdown still lists every camp, so check the totals table.
        table = html[html.find("Collected per camp"):html.find("Collected by month")]
        assert "Camp A" in table and "Camp B" not in table

    def test_filter_by_date_range(self, app, client):
        login(client)
        _seed(client, [("Camp A", 100, "2026-07-05"), ("Camp A", 250, "2026-08-05")])
        html = client.get(
            "/admin/iyf-camps/collections?date_from=2026-08-01&date_to=2026-08-31").data.decode()
        assert "Aug 2026" in html and "Jul 2026" not in html

    def test_cancelled_donations_excluded(self, app, client):
        """These figures reconcile against what a camp actually raised."""
        from extensions import db
        from models import Donation
        login(client)
        _seed(client, [("Camp A", 100, "2026-08-05"), ("Camp A", 900, "2026-08-06")])
        d = Donation.query.filter_by(amount=900).first()
        d.status = "cancelled"
        db.session.commit()
        html = client.get("/admin/iyf-camps/collections").data.decode()
        table = html[html.find("Collected per camp"):html.find("Collected by month")]
        assert "900" not in table

    def test_bad_dates_do_not_500(self, app, client):
        login(client)
        assert client.get(
            "/admin/iyf-camps/collections?date_from=nonsense&date_to=x").status_code == 200

    def test_tabs_link_to_all_three_pages(self, app, client):
        login(client)
        html = client.get("/admin/iyf-camps/collections").data.decode()
        assert "/admin/iyf-camps/collections" in html
        assert "tab=single" in html and "tab=bulk" in html

    def test_entry_page_no_longer_shows_totals(self, app, client):
        """Totals moved to their own tab; the entry page shouldn't repeat
        them."""
        login(client)
        _seed(client, [("Camp A", 100, "2026-08-05")])
        html = client.get("/admin/iyf-camps").data.decode()
        assert "Collected per camp" not in html


class TestExports:
    def test_camp_wise_export(self, app, client):
        login(client)
        _seed(client, [("Camp A", 100, "2026-08-05"), ("Camp A", 250, "2026-08-06"),
                       ("Camp B", 400, "2026-08-06")])
        rows = _csv_rows(client.get("/admin/iyf-camps/export/camp.csv"))
        assert rows[0] == ["Camp", "Donations", "Total"]
        data = {r[0]: (int(r[1]), float(r[2])) for r in rows[1:]}
        assert data["Camp A"] == (2, 350.0)
        assert data["Camp B"] == (1, 400.0)

    def test_monthly_export(self, app, client):
        login(client)
        _seed(client, [("Camp A", 100, "2026-07-05"), ("Camp A", 250, "2026-08-05")])
        rows = _csv_rows(client.get("/admin/iyf-camps/export/monthly.csv"))
        assert rows[0] == ["Month", "Donations", "Total"]
        data = {r[0]: float(r[2]) for r in rows[1:]}
        assert data["Jul 2026"] == 100.0
        assert data["Aug 2026"] == 250.0

    def test_detail_export_has_student_camp_batch_receipt(self, app, client):
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Camp A"}, follow_redirects=True)
        client.post("/admin/iyf-camps/single", data={
            "camp_name": "Camp A", "batch_name": "Batch A", "full_name": "Ravi Sharma",
            "amount": "1100", "phone": "9876543210", "donation_date": "2026-08-05",
        }, follow_redirects=True)
        rows = _csv_rows(client.get("/admin/iyf-camps/export/detail.csv"))
        assert rows[0][:7] == ["Date", "Receipt No", "Student", "Phone", "Email", "Camp", "Batch"]
        row = rows[1]
        assert row[2] == "Ravi Sharma" and row[5] == "Camp A" and row[6] == "Batch A"
        assert row[1]                      # receipt number present

    def test_exports_honour_the_filters(self, app, client):
        """A download must match what's on screen, or the numbers in a
        report and the numbers in a meeting won't agree."""
        login(client)
        _seed(client, [("Camp A", 100, "2026-07-05"), ("Camp A", 250, "2026-08-05"),
                       ("Camp B", 400, "2026-08-06")])
        rows = _csv_rows(client.get("/admin/iyf-camps/export/camp.csv?camp=Camp+A"))
        assert [r[0] for r in rows[1:]] == ["Camp A"]

        rows = _csv_rows(client.get(
            "/admin/iyf-camps/export/monthly.csv?date_from=2026-08-01"))
        assert [r[0] for r in rows[1:]] == ["Aug 2026"]

    def test_filename_names_the_selection(self, app, client):
        login(client)
        _seed(client, [("Camp A", 100, "2026-08-05")])
        resp = client.get("/admin/iyf-camps/export/camp.csv?camp=Camp+A")
        assert "Camp_A" in resp.headers["Content-Disposition"]

    def test_unknown_report_404s(self, app, client):
        login(client)
        assert client.get("/admin/iyf-camps/export/bogus.csv").status_code == 404

    def test_exports_require_login(self, app, client):
        resp = client.get("/admin/iyf-camps/export/detail.csv", follow_redirects=False)
        assert resp.status_code in (301, 302)
        assert "/admin/login" in resp.headers.get("Location", "")
