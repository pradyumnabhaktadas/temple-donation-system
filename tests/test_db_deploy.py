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


def _prod_env(db_path, **overrides):
    """The environment a production *database* process runs with.

    MIGRATIONS_ONLY=1 because every helper here builds an app purely to get
    an application context to inspect or seed a database through -- none of
    them serve a request, which is exactly what that flag means (see
    create_app()'s docstring in app.py). Without it they'd have to satisfy
    the live-traffic guards: real Razorpay credentials, and Flask-Limiter
    installed -- and Flask-Limiter deliberately isn't, in the sandbox this
    suite is expected to run in (see CLAUDE.md), so no amount of
    environment could satisfy that one.

    Pass MIGRATIONS_ONLY="" to get an environment for a genuinely serving
    app, as TestRuntimeConfigDoesNotBlockMigrations does.
    """
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{db_path}",
        "FLASK_ENV": "production",          # the path production takes
        "SECRET_KEY": "x" * 40,             # required in production
        "MIGRATIONS_ONLY": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.update(overrides)
    return env


def _run_deploy(db_path, env=None):
    """Run db_deploy.py as a subprocess, the way Render does.

    A subprocess, not an import: Alembic and create_app both carry global
    state, and the point is to test the command as it actually runs.
    """
    return subprocess.run(
        [sys.executable, "db_deploy.py"],
        cwd=REPO, env=env or _prod_env(db_path), capture_output=True, text=True, timeout=300,
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
    out = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=_prod_env(db_path),
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
            "                  # suppress_receipt (a91c7f3e2b56) is newer than the\n"
            "                  # b2e91a7c4d05 baseline this helper stamps to, but\n"
            "                  # db.create_all() above just built it from the current\n"
            "                  # models.py, which already has the column -- strip it\n"
            "                  # like every other post-baseline column/table here, or\n"
            "                  # the forward migration's add_column collides with one\n"
            "                  # that already exists (the create_all()-races-migration\n"
            "                  # failure mode, this time for a column instead of a\n"
            "                  # table).\n"
            "                  'ALTER TABLE campaigns DROP COLUMN suppress_receipt',\n"
            "                  'ALTER TABLE donations DROP COLUMN preaching_center_name',\n"
            "                  # 069fa9d71441 -- same reasoning again, but for indexes:\n"
            "                  # create_all() above already built these from the\n"
            "                  # current models.py (index=True on each column), so the\n"
            "                  # forward migration's create_index would collide.\n"
            "                  'DROP INDEX ix_donations_donor_id',\n"
            "                  'DROP INDEX ix_donations_campaign_id',\n"
            "                  'DROP INDEX ix_donations_bace_property_id',\n"
            "                  'DROP INDEX ix_donations_status',\n"
            "                  'DROP INDEX ix_donations_donation_date',\n"
            "                  'DROP INDEX ix_donations_razorpay_order_id',\n"
            # Before the batch drop_column of bace_student_id below:
            # Alembic's batch mode recreates every index it reflected on
            # the table, including one on a column that is no longer there
            # ("no such column: bace_student_id").
            "                  'DROP INDEX ix_donations_bace_student_id',\n"
            # admin_users.bace_property_id + its index arrive with the BACE
            # rent ledger work, also after this baseline.
            "                  'DROP INDEX ix_admin_users_bace_property_id',\n"
            "                  'DROP INDEX ix_donations_razorpay_payment_id'):\n"
            "            conn.execute(sa.text(s))\n"
            # Every table that did not exist at the baseline revision is
            # dropped below, found by *subtraction* from this fixed list of
            # the baseline's own tables rather than by naming each newer one.
            "    BASELINE_TABLES = {'admin_activity_logs', 'admin_users', 'bace_properties',\n"
            "        'campaigns', 'donations', 'donor_login_otps', 'donors', 'festivals',\n"
            "        'live_to_give_purposes', 'preachers', 'receipt_counters', 'seva_types',\n"
            "        'alembic_version'}\n"
            # The same idea for columns. A column on a baseline table that
            # carries a foreign key to a post-baseline table (associated_with_id,
            # bace_student_id, preaching_center_id ...) has to go too: leaving
            # it behind leaves a foreign key pointing at nothing, and SQLite's
            # batch mode reflects every FK on a table it recreates, so an
            # *earlier* migration dies with NoSuchTableError. Plain ALTER TABLE
            # DROP COLUMN can't remove a column that is in a foreign key
            # ('unknown column ... in foreign key definition'), so go through
            # Alembic's batch mode, which recreates the table. Found by
            # inspection, so the next new foreign-key column is handled with
            # no edit here. (Plain new columns/indexes with no foreign key
            # still have to be named in the DROP list above.)
            "    from alembic.migration import MigrationContext\n"
            "    from alembic.operations import Operations\n"
            "    with db.engine.connect() as conn:\n"
            "        insp = sa.inspect(conn)\n"
            "        fk_columns = {}\n"
            "        for t in BASELINE_TABLES - {'alembic_version'}:\n"
            "            for fk in insp.get_foreign_keys(t):\n"
            "                if fk['referred_table'] not in BASELINE_TABLES:\n"
            "                    fk_columns.setdefault(t, set()).update(fk['constrained_columns'])\n"
            "        # Alembic's batch mode re-creates every index it reflected,\n"
            "        # including one on a column that is about to be removed.\n"
            "        for t, cols in fk_columns.items():\n"
            "            for ix in insp.get_indexes(t):\n"
            "                if ix['name'] and not ix['name'].startswith('sqlite_') and set(ix['column_names']) & cols:\n"
            "                    conn.execute(sa.text('DROP INDEX ' + ix['name']))\n"
            "        conn.commit()\n"
            "        ctx = MigrationContext.configure(conn)\n"
            "        alembic_op = Operations(ctx)\n"
            "        for t, cols in fk_columns.items():\n"
            "            with alembic_op.batch_alter_table(t) as batch_op:\n"
            "                for c in sorted(cols):\n"
            "                    batch_op.drop_column(c)\n"
            "    with db.engine.begin() as conn:\n"
            "        # Foreign keys are not enforced by SQLite unless switched on,\n"
            "        # so drop order between the dropped tables doesn't matter.\n"
            "        for t in sorted(set(sa.inspect(conn).get_table_names()) - BASELINE_TABLES):\n"
            "            conn.execute(sa.text(f'DROP TABLE {t}'))\n"

            f"    stamp(revision='{revision}')\n"
        )
        env = _prod_env(db_path)
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
        env = _prod_env(db_path)
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
        env = _prod_env(db_path)
        subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=300)

        result = _run_deploy(db_path)
        assert result.returncode == 1, "should refuse, not proceed on a guess"
        assert "Refusing" in result.stderr
        assert "flask db stamp" in result.stderr, "should say how to resolve it"


class TestRuntimeConfigDoesNotBlockMigrations:
    """create_app() refuses to build a *serving* app in production without
    Razorpay credentials and Flask-Limiter (app.py). That guard is right
    for a web dyno and wrong for the pre-deploy step, which serves nothing.

    This shipped the wrong way round once: db_deploy.py's own create_app()
    tripped the guard, so `python db_deploy.py` died with "Razorpay
    credentials are required in production" before touching the database --
    failing every deploy on a host where the keys weren't set yet, which is
    exactly the bootstrap order render.yaml documents."""

    def test_deploy_works_before_razorpay_is_configured(self, tmp_path):
        """db_deploy.py sets MIGRATIONS_ONLY for itself, so this holds even
        from an environment that says nothing about it -- which is what
        Render's pre-deploy step actually passes."""
        db_path = tmp_path / "fresh.db"
        env = _prod_env(db_path, MIGRATIONS_ONLY="")
        env.pop("RAZORPAY_KEY_ID", None)
        env.pop("RAZORPAY_KEY_SECRET", None)

        result = _run_deploy(db_path, env=env)

        assert result.returncode == 0, result.stderr
        assert "Razorpay" not in result.stderr

    def test_a_serving_app_still_refuses_without_razorpay(self, tmp_path):
        """The guard itself must stay intact -- the fix scopes it, it
        doesn't remove it."""
        db_path = tmp_path / "serving.db"
        env = _prod_env(db_path, MIGRATIONS_ONLY="")
        env.pop("RAZORPAY_KEY_ID", None)
        env.pop("RAZORPAY_KEY_SECRET", None)

        out = subprocess.run(
            [sys.executable, "-c", "from app import create_app; create_app()"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
        )

        assert out.returncode != 0
        assert "required in production" in out.stderr
