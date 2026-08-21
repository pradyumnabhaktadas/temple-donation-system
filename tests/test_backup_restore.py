"""Backup and restore, exercised end to end.

Restore is the most destructive action in the admin panel: it can
overwrite or delete every donation record the temple has. So these tests
care less about the happy path than about the guards -- that a preview
writes nothing, that applying without the typed confirmation writes
nothing, that a safety backup exists before anything is overwritten, and
that a corrupt upload leaves the data untouched.
"""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import login


def _seed_donation(client, name="Ravi Sharma", amount="1100"):
    from models import Campaign
    campaign = Campaign.query.filter_by(name="Annadan").first()
    client.post("/admin/donations/manual", data={
        "campaign_id": campaign.id, "full_name": name, "phone": "9876543210",
        "amount": amount, "payment_mode": "cash", "donation_date": "2026-08-01",
    }, follow_redirects=True)


def _take_backup(client):
    resp = client.get("/admin/settings/backup/download")
    assert resp.status_code == 200
    return resp.data


def _upload(client, zip_bytes, mode="preview", confirm="", wipe=False, filename="backup.zip"):
    data = {
        "backup_zip": (io.BytesIO(zip_bytes), filename),
        "mode": mode,
        "confirm": confirm,
    }
    if wipe:
        data["wipe"] = "yes"
    return client.post("/admin/settings/backup/restore", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


class TestBackupContents:
    def test_backup_contains_the_expected_tables(self, app, client):
        login(client)
        _seed_donation(client)
        with zipfile.ZipFile(io.BytesIO(_take_backup(client))) as zf:
            names = set(zf.namelist())
        assert {"donors.csv", "donations.csv", "campaigns.csv"} <= names

    def test_backup_includes_associated_withs(self, app, client):
        """Regression: this table was added to the app (models.py,
        reset_data.py, seed.py) but initially missed here -- a weekly
        backup would have silently dropped the whole lookup list, and a
        restore would leave donations.associated_with_id referencing rows
        that no longer existed anywhere in the backup."""
        login(client)
        with zipfile.ZipFile(io.BytesIO(_take_backup(client))) as zf:
            names = set(zf.namelist())
        assert "associated_withs.csv" in names

    def test_associated_with_data_round_trips_through_restore(self, app, client):
        from extensions import db
        from models import AssociatedWith
        login(client)
        client.post("/admin/associated-with", data={"name": "IYF Dwarka Temple Preaching"},
                    follow_redirects=True)
        backup = _take_backup(client)

        item = AssociatedWith.query.one()
        item.name = "Renamed After Backup"
        db.session.commit()

        resp = _upload(client, backup, mode="apply", confirm="RESTORE")
        assert b"Restore complete" in resp.data
        db.session.expire_all()
        assert AssociatedWith.query.one().name == "IYF Dwarka Temple Preaching"

    def test_backup_excludes_credentials(self, app, client):
        """Login secrets must not leave the database in a portable file."""
        login(client)
        with zipfile.ZipFile(io.BytesIO(_take_backup(client))) as zf:
            names = set(zf.namelist())
        assert not any("admin" in n or "otp" in n for n in names)


class TestRestorePreview:
    def test_preview_writes_nothing(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        _seed_donation(client)
        backup = _take_backup(client)

        # Change the data, then preview the old backup over it.
        d = Donation.query.one()
        d.amount = 9999
        db.session.commit()

        resp = _upload(client, backup, mode="preview")
        assert b"Preview only" in resp.data
        db.session.expire_all()
        assert float(Donation.query.one().amount) == 9999.0, "preview modified data"

    def test_preview_reports_what_would_change(self, app, client):
        login(client)
        _seed_donation(client)
        resp = _upload(client, _take_backup(client), mode="preview")
        assert b"donations" in resp.data

    def test_preview_does_not_need_the_confirmation(self, app, client):
        login(client)
        _seed_donation(client)
        resp = _upload(client, _take_backup(client), mode="preview", confirm="")
        assert b"Preview only" in resp.data


class TestRestoreApply:
    def test_apply_requires_typed_confirmation(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        _seed_donation(client)
        backup = _take_backup(client)
        d = Donation.query.one()
        d.amount = 9999
        db.session.commit()

        resp = _upload(client, backup, mode="apply", confirm="")
        assert b"Type RESTORE" in resp.data
        db.session.expire_all()
        assert float(Donation.query.one().amount) == 9999.0, "restore ran without confirmation"

    def test_wrong_confirmation_text_is_refused(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        _seed_donation(client)
        backup = _take_backup(client)
        Donation.query.one().amount = 9999
        db.session.commit()

        _upload(client, backup, mode="apply", confirm="yes")
        db.session.expire_all()
        assert float(Donation.query.one().amount) == 9999.0

    def test_apply_restores_the_data(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        _seed_donation(client)
        backup = _take_backup(client)
        Donation.query.one().amount = 9999
        db.session.commit()

        resp = _upload(client, backup, mode="apply", confirm="RESTORE")
        assert b"Restore complete" in resp.data
        db.session.expire_all()
        assert float(Donation.query.one().amount) == 1100.0

    def test_confirmation_is_case_insensitive_but_must_be_the_word(self, app, client):
        from extensions import db
        from models import Donation
        login(client)
        _seed_donation(client)
        backup = _take_backup(client)
        Donation.query.one().amount = 9999
        db.session.commit()

        _upload(client, backup, mode="apply", confirm="restore")
        db.session.expire_all()
        assert float(Donation.query.one().amount) == 1100.0

    def test_safety_backup_is_taken_before_applying(self, app, client):
        """The current data has to be recoverable afterwards -- that's what
        makes this button safe to offer at all."""
        from unittest.mock import patch
        login(client)
        _seed_donation(client)
        backup = _take_backup(client)

        with patch("admin.run_backup", wraps=None) as safety:
            safety.return_value = {"filename": "safety.zip"}
            _upload(client, backup, mode="apply", confirm="RESTORE")
        safety.assert_called_once()

    def test_restore_aborts_if_the_safety_backup_fails(self, app, client):
        """Going ahead without a way back is not a trade worth making."""
        from unittest.mock import patch
        from extensions import db
        from models import Donation
        login(client)
        _seed_donation(client)
        backup = _take_backup(client)
        Donation.query.one().amount = 9999
        db.session.commit()

        with patch("admin.run_backup", side_effect=RuntimeError("disk full")):
            resp = _upload(client, backup, mode="apply", confirm="RESTORE")
        assert b"restore was cancelled" in resp.data
        db.session.expire_all()
        assert float(Donation.query.one().amount) == 9999.0

    def test_upsert_keeps_newer_rows(self, app, client):
        """Without wipe, the backup layers over what's there."""
        from models import Donation
        login(client)
        _seed_donation(client, "Ravi Sharma", "1100")
        backup = _take_backup(client)
        _seed_donation(client, "Later Donor", "500")     # not in the backup

        _upload(client, backup, mode="apply", confirm="RESTORE")
        assert Donation.query.count() == 2, "upsert deleted a row it shouldn't have"

    def test_wipe_makes_the_data_match_the_backup(self, app, client):
        from models import Donation
        login(client)
        _seed_donation(client, "Ravi Sharma", "1100")
        backup = _take_backup(client)
        _seed_donation(client, "Later Donor", "500")

        _upload(client, backup, mode="apply", confirm="RESTORE", wipe=True)
        assert Donation.query.count() == 1
        assert float(Donation.query.one().amount) == 1100.0

    def test_admin_logins_survive_a_restore(self, app, client):
        """Restoring credentials from an old backup would silently roll
        back passwords -- so they're never in the backup, and a restore
        must leave them alone."""
        from models import AdminUser
        login(client)
        _seed_donation(client)
        before = {u.username: u.password_hash for u in AdminUser.query.all()}
        _upload(client, _take_backup(client), mode="apply", confirm="RESTORE")
        after = {u.username: u.password_hash for u in AdminUser.query.all()}
        assert before == after


class TestRestoreBadInput:
    def test_not_a_zip_leaves_data_untouched(self, app, client):
        from models import Donation
        login(client)
        _seed_donation(client)
        resp = _upload(client, b"this is not a zip file", mode="apply", confirm="RESTORE")
        assert b"Restore failed" in resp.data
        assert Donation.query.count() == 1

    def test_zip_without_backup_csvs_is_rejected(self, app, client):
        from models import Donation
        login(client)
        _seed_donation(client)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("holiday-photo.txt", "not a backup")
        resp = _upload(client, buf.getvalue(), mode="apply", confirm="RESTORE")
        assert b"Restore failed" in resp.data
        assert Donation.query.count() == 1

    def test_no_file_selected(self, app, client):
        login(client)
        resp = client.post("/admin/settings/backup/restore", data={"mode": "preview"},
                           content_type="multipart/form-data", follow_redirects=True)
        assert b"choose a backup ZIP" in resp.data

    def test_partial_backup_reports_what_it_skipped(self, app, client):
        """A ZIP with only some tables restores those and says so, rather
        than failing or silently emptying the rest."""
        login(client)
        _seed_donation(client)
        full = _take_backup(client)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(full)) as src, zipfile.ZipFile(buf, "w") as dst:
            dst.writestr("campaigns.csv", src.read("campaigns.csv"))
        resp = _upload(client, buf.getvalue(), mode="preview")
        assert b"donations.csv" in resp.data      # named as skipped


class TestRestorePermissions:
    def test_staff_cannot_restore(self, app, client):
        """Staff can record donations; wiping the database is not theirs
        to do."""
        login(client, username="teststaff")
        resp = client.post("/admin/settings/backup/restore", data={"mode": "preview"},
                           content_type="multipart/form-data", follow_redirects=True)
        assert b"choose a backup ZIP" not in resp.data

    def test_logged_out_is_redirected_to_login(self, app, client):
        resp = client.post("/admin/settings/backup/restore", data={"mode": "preview"},
                           content_type="multipart/form-data", follow_redirects=False)
        assert resp.status_code in (301, 302)
        assert "/admin/login" in resp.headers.get("Location", "")

    def test_restore_is_recorded_in_the_activity_log(self, app, client):
        from models import AdminActivityLog
        login(client)
        _seed_donation(client)
        _upload(client, _take_backup(client), mode="apply", confirm="RESTORE")
        assert AdminActivityLog.query.filter_by(action="backup_restore").count() == 1

    def test_preview_is_not_logged_as_a_restore(self, app, client):
        from models import AdminActivityLog
        login(client)
        _seed_donation(client)
        _upload(client, _take_backup(client), mode="preview")
        assert AdminActivityLog.query.filter_by(action="backup_restore").count() == 0
