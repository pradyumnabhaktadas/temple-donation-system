"""Every CSV upload route, exercised with a real uploaded file.

None of these routes had a test that actually posted a file. That gap hid
a real bug: they wrapped Werkzeug's SpooledTemporaryFile in
io.TextIOWrapper, which needs readable() -- absent before Python 3.11. So
every import reported "Couldn't read that file" on any Python older than
3.11, while working fine on the 3.12 that production runs. Reading the
code could not have shown that; uploading a file does.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _post_csv(client, url, text, field="csv_file"):
    return client.post(
        url,
        data={field: (io.BytesIO(text.encode()), "data.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


class TestCsvUploadsAreReadable:
    """Each route must get past "can I read this file at all"."""

    UNREADABLE = b"Couldn't read that file"

    def test_offline_donation_bulk_import(self, app, client):
        from models import Donation
        login(client)
        resp = _post_csv(client, "/admin/donations/bulk-import",
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Ravi Sharma,Annadan,1100,cash,2026-08-01\n")
        assert self.UNREADABLE not in resp.data
        assert Donation.query.count() == 1

    def test_legacy_donation_import(self, app, client):
        login(client)
        resp = _post_csv(client, "/admin/donations/import-legacy",
            "full_name,campaign_name,amount,payment_mode,donation_date\n"
            "Ravi Sharma,Annadan,1100,cash,2026-08-01\n")
        assert self.UNREADABLE not in resp.data

    def test_donor_master_import(self, app, client):
        from models import Donor
        login(client)
        resp = _post_csv(client, "/admin/donors/import",
            "full_name,phone\nRavi Sharma,9876543210\n")
        assert self.UNREADABLE not in resp.data
        assert Donor.query.filter_by(full_name="Ravi Sharma").count() == 1

    def test_iyf_camp_import(self, app, client):
        from models import Donation
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Utkarsha 2026"},
                    follow_redirects=True)
        resp = _post_csv(client, "/admin/iyf-camps/bulk",
            "full_name,amount,camp_name\nRavi Sharma,1100,Utkarsha 2026\n")
        assert self.UNREADABLE not in resp.data
        assert Donation.query.count() == 1


class TestCsvUploadEdgeCases:
    def test_utf8_bom_is_stripped(self, app, client):
        """Excel writes a BOM; without utf-8-sig the first column name
        becomes '﻿full_name' and every required-column check fails."""
        from models import Donation
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Camp A"}, follow_redirects=True)
        resp = client.post("/admin/iyf-camps/bulk", data={"csv_file": (
            io.BytesIO("﻿full_name,amount,camp_name\nRavi,500,Camp A\n".encode("utf-8")),
            "excel.csv")}, content_type="multipart/form-data", follow_redirects=True)
        assert b"missing required column" not in resp.data
        assert Donation.query.count() == 1

    def test_undecodable_byte_does_not_kill_the_file(self, app, client):
        """One stray byte from a spreadsheet export shouldn't make a whole
        import unreadable."""
        from models import Donation
        login(client)
        client.post("/admin/iyf-camps/manage", data={"name": "Camp A"}, follow_redirects=True)
        raw = b"full_name,amount,camp_name\nRav\xffi,500,Camp A\n"
        resp = client.post("/admin/iyf-camps/bulk",
            data={"csv_file": (io.BytesIO(raw), "odd.csv")},
            content_type="multipart/form-data", follow_redirects=True)
        assert b"Couldn't read that file" not in resp.data
        assert Donation.query.count() == 1

    def test_empty_file_is_reported_not_crashed(self, app, client):
        login(client)
        resp = _post_csv(client, "/admin/iyf-camps/bulk", "")
        assert resp.status_code == 200

    def test_no_file_selected(self, app, client):
        login(client)
        resp = client.post("/admin/iyf-camps/bulk", data={},
                           content_type="multipart/form-data", follow_redirects=True)
        assert b"choose a CSV file" in resp.data
