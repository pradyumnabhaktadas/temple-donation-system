"""db_deploy.py -- the pre-deploy database step.

This is the code that runs before traffic reaches new code, against the
live donation database. A mistake here is a failed deploy at best and a
damaged database at worst, and it's the one part of the system that never
runs during ordinary use, so nothing else would notice it rotting.

These tests drive real SQLite databases through the real migration chain,
rather than mocking Alembic -- which is the only way they'd have caught
the two failures already seen: `flask db upgrade` on an empty database
("no such table: donations"), and create_all racing a new-table migration
(DuplicateTable).
"""
import os
import subprocess
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _run_deploy(db_path):
    """Run db_deploy.py as a subprocess, the way Render does.

    A subprocess, not an import: Alembic and create_app both carry global
    state, and the point is to test the command as it actually runs.
    """
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{db_path}",
        "FLASK_ENV": "production",          # the path production takes
        "SECRET_KEY": "x" * 40,             # required in production
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return subprocess.run(
        [sys.executable, "db_deploy.py"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )


def _inspect(db_path):
    """Open the database and report its shape, in a fresh process so the
    models aren't cached from a previous state."""
    script = (
        "from app import create_app\n"
        "from extensions import db\n"
        "app = create_app()\n"
        "with app.app_context():\n"
        "    insp = db.inspect(db.engine)\n"
        "    tables = sorted(insp.get_table_names())\n"
        "    cols = sorted(c['name'] for c in insp.get_columns('donations')) "
        "if 'donations' in tables else []\n"
        "    ver = db.session.execute(db.text('SELECT version_num FROM alembic_version'))"
        ".scalar() if 'alembic_version' in tables else None\n"
        "    print(repr((tables, cols, ver)))\n"
    )
    env = dict(os.environ)
    env.update({"DATABASE_URL": f"sqlite:///{db_path}", "FLASK_ENV": "production",
                "SECRET_KEY": "x" * 40, "PYTHONDONTWRITEBYTECODE": "1"})
    out = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    return eval(out.stdout.strip().splitlines()[-1])


class TestEmptyDatabase:
    """Disaster recovery: a brand new host, nothing in the database."""

    def test_bootstraps_and_stamps(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        result = _run_deploy(db_path)
        assert result.returncode == 0, result.stderr

        tables, cols, version = _inspect(db_path)
        assert "donations" in tables and "donors" in tables and "camps" in tables
        assert "alembic_version" in tables
        assert version, "database was left unstamped -- the next deploy would replay everything"

    def test_schema_is_current(self, tmp_path):
        """Columns from the newest migrations must be present, or the app
        500s on its first request."""
        db_path = tmp_path / "fresh.db"
        assert _run_deploy(db_path).returncode == 0
        _, cols, _ = _inspect(db_path)
        for col in ("camp_name", "batch_name", "receipt_pdf", "razorpay_dispute_id"):
            assert col in cols, f"donations.{col} missing from a freshly built database"

    def test_second_run_is_a_no_op(self, tmp_path):
        """Render can retry a pre-deploy; it must not fail the second time."""
        db_path = tmp_path / "fresh.db"
        assert _run_deploy(db_path).returncode == 0
        second = _run_deploy(db_path)
        assert second.returncode == 0, second.stderr
        assert "Existing database" in second.stdout


class TestExistingDatabase:
    """The ordinary deploy, and the one production does."""

    def _build_at_revision(self, db_path, revision):
        """A database with real data, stamped at an older revision -- the
        state production is in between deploys."""
        script = (
            "import sqlalchemy as sa\n"
            "from app import create_app\n"
            "from extensions import db\n"
            "from flask_migrate import stamp\n"
            "app = create_app()\n"
            "with app.app_context():\n"
            "    db.create_all()\n"
            "    from models import Campaign, Donor, Donation\n"
            "    c = Campaign(name='Annadan', is_80g=True); db.session.add(c); db.session.flush()\n"
            "    d = Donor(full_name='Existing Donor', phone='9876543210'); db.session.add(d)\n"
            "    db.session.flush()\n"
            "    db.session.add(Donation(donor_id=d.id, campaign_id=c.id, amount=1100,\n"
            "        payment_mode='cash', status='success', receipt_number='032511/ISK500001'))\n"
            "    db.session.commit()\n"
            "    with db.engine.begin() as conn:\n"
            "        for s in ('DROP INDEX ix_donations_camp_name',\n"
            "                  'DROP INDEX ix_donations_batch_name',\n"
            "                  'ALTER TABLE donations DROP COLUMN camp_name',\n"
            "                  'ALTER TABLE donations DROP COLUMN batch_name',\n"
            "                  'DROP TABLE camps'):\n"
            "            conn.execute(sa.text(s))\n"
            f"    stamp(revision='{revision}')\n"
        )
        env = dict(os.environ)
        env.update({"DATABASE_URL": f"sqlite:///{db_path}", "FLASK_ENV": "production",
                    "SECRET_KEY": "x" * 40, "PYTHONDONTWRITEBYTECODE": "1"})
        out = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, out.stderr

    def test_pending_migrations_are_applied(self, tmp_path):
        db_path = tmp_path / "existing.db"
        self._build_at_revision(db_path, "b2e91a7c4d05")   # before the camps work

        result = _run_deploy(db_path)
        assert result.returncode == 0, result.stderr
        assert "Existing database" in result.stdout

        tables, cols, _ = _inspect(db_path)
        assert "camps" in tables
        assert "camp_name" in cols and "batch_name" in cols

    def test_existing_data_survives(self, tmp_path):
        """The whole point: a deploy must not touch the donation records."""
        db_path = tmp_path / "existing.db"
        self._build_at_revision(db_path, "b2e91a7c4d05")
        assert _run_deploy(db_path).returncode == 0

        script = (
            "from app import create_app\n"
            "from extensions import db\n"
            "from models import Donation\n"
            "app = create_app()\n"
            "with app.app_context():\n"
            "    d = Donation.query.one()\n"
            "    print(repr((d.donor.full_name, d.receipt_number, float(d.amount))))\n"
        )
        env = dict(os.environ)
        env.update({"DATABASE_URL": f"sqlite:///{db_path}", "FLASK_ENV": "production",
                    "SECRET_KEY": "x" * 40, "PYTHONDONTWRITEBYTECODE": "1"})
        out = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, out.stderr
        assert eval(out.stdout.strip().splitlines()[-1]) == (
            "Existing Donor", "032511/ISK500001", 1100.0)

    def test_already_current_is_a_no_op(self, tmp_path):
        db_path = tmp_path / "existing.db"
        self._build_at_revision(db_path, "b2e91a7c4d05")
        assert _run_deploy(db_path).returncode == 0
        again = _run_deploy(db_path)
        assert again.returncode == 0, again.stderr


class TestAmbiguousDatabase:
    def test_tables_without_alembic_version_is_refused(self, tmp_path):
        """Tables but no migration history: the database could be at any
        point in the chain. Guessing on donation records isn't acceptable,
        so it stops and says what to do."""
        db_path = tmp_path / "ambiguous.db"
        script = (
            "from app import create_app\n"
            "from extensions import db\n"
            "app = create_app()\n"
            "with app.app_context():\n"
            "    db.create_all()\n"
        )
        env = dict(os.environ)
        env.update({"DATABASE_URL": f"sqlite:///{db_path}", "FLASK_ENV": "production",
                    "SECRET_KEY": "x" * 40, "PYTHONDONTWRITEBYTECODE": "1"})
        subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=300)

        result = _run_deploy(db_path)
        assert result.returncode == 1, "should refuse, not proceed on a guess"
        assert "Refusing" in result.stderr
        assert "flask db stamp" in result.stderr, "should say how to resolve it"
