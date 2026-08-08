"""One-off repair for a stuck migration.

What happened (most recently, 2026-08-07): `flask db upgrade` failed with
"psycopg2.errors.DuplicateTable: relation 'live_to_give_purposes' already
exists", even though `flask db current` reported the database was still on
the *previous* revision (d9a27e6b5c31). That means the table (and probably
the new donations columns alongside it) already exist in the database --
but Alembic's own bookkeeping table (alembic_version) doesn't know that, so
it tries to create everything again and fails. Same class of issue as the
earlier bace_properties incident this script was originally written for --
can happen if `flask db upgrade` was run more than once close together, or
a table got created outside of Alembic (db.create_all(), a half-finished
prior attempt, etc.) without alembic_version being updated to match.

This script reconciles the two: it checks what actually exists (tables and
columns), creates only what's missing, and then stamps alembic_version to
the latest revision so `flask db upgrade` behaves normally again afterwards.

Safe to run more than once -- every step checks first and skips if the
thing already exists.

Usage (Render Shell, from the project root):
    python3 fix_migration_state.py
"""
from app import create_app
from extensions import db
from sqlalchemy import inspect, text

HEAD_REVISION = "e2b74a1c8f63"

app = create_app()

with app.app_context():

    def existing_tables():
        return set(inspect(db.engine).get_table_names())

    def donation_columns():
        return {c["name"] for c in inspect(db.engine).get_columns("donations")}

    tables = existing_tables()
    print("Existing tables:", sorted(tables))
    print("donations columns:", sorted(donation_columns()))

    # --- bace_properties ---
    if "bace_properties" not in tables:
        db.session.execute(text("""
            CREATE TABLE bace_properties (
                id SERIAL PRIMARY KEY,
                name VARCHAR(150) NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL,
                created_at TIMESTAMP
            )
        """))
        db.session.commit()
        print("Created bace_properties")
    else:
        print("bace_properties already exists -- skipping")

    if "bace_property_id" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN bace_property_id INTEGER"))
        db.session.execute(text(
            "ALTER TABLE donations ADD CONSTRAINT fk_donations_bace_property_id "
            "FOREIGN KEY (bace_property_id) REFERENCES bace_properties(id)"
        ))
        db.session.commit()
        print("Added donations.bace_property_id + FK")
    else:
        print("donations.bace_property_id already exists -- skipping")

    # --- festivals ---
    if "festivals" not in tables:
        db.session.execute(text("""
            CREATE TABLE festivals (
                id SERIAL PRIMARY KEY,
                name VARCHAR(150) NOT NULL UNIQUE,
                event_date DATE,
                is_active BOOLEAN NOT NULL,
                created_at TIMESTAMP
            )
        """))
        db.session.commit()
        print("Created festivals")
    else:
        print("festivals already exists -- skipping")

    # --- seva_types ---
    if "seva_types" not in tables:
        db.session.execute(text("""
            CREATE TABLE seva_types (
                id SERIAL PRIMARY KEY,
                name VARCHAR(150) NOT NULL UNIQUE,
                suggested_amount NUMERIC(12,2),
                is_active BOOLEAN NOT NULL,
                created_at TIMESTAMP
            )
        """))
        db.session.commit()
        print("Created seva_types")
    else:
        print("seva_types already exists -- skipping")

    if "festival_id" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN festival_id INTEGER"))
        db.session.execute(text(
            "ALTER TABLE donations ADD CONSTRAINT fk_donations_festival_id "
            "FOREIGN KEY (festival_id) REFERENCES festivals(id)"
        ))
        db.session.commit()
        print("Added donations.festival_id + FK")
    else:
        print("donations.festival_id already exists -- skipping")

    if "seva_type_id" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN seva_type_id INTEGER"))
        db.session.execute(text(
            "ALTER TABLE donations ADD CONSTRAINT fk_donations_seva_type_id "
            "FOREIGN KEY (seva_type_id) REFERENCES seva_types(id)"
        ))
        db.session.commit()
        print("Added donations.seva_type_id + FK")
    else:
        print("donations.seva_type_id already exists -- skipping")

    # --- live_to_give_purposes ---
    if "live_to_give_purposes" not in tables:
        db.session.execute(text("""
            CREATE TABLE live_to_give_purposes (
                id SERIAL PRIMARY KEY,
                name VARCHAR(150) NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL,
                created_at TIMESTAMP
            )
        """))
        db.session.commit()
        print("Created live_to_give_purposes")
    else:
        print("live_to_give_purposes already exists -- skipping")

    if "live_to_give_purpose_id" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN live_to_give_purpose_id INTEGER"))
        db.session.execute(text(
            "ALTER TABLE donations ADD CONSTRAINT fk_donations_live_to_give_purpose_id "
            "FOREIGN KEY (live_to_give_purpose_id) REFERENCES live_to_give_purposes(id)"
        ))
        db.session.commit()
        print("Added donations.live_to_give_purpose_id + FK")
    else:
        print("donations.live_to_give_purpose_id already exists -- skipping")

    if "is_80g_requested" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN is_80g_requested BOOLEAN"))
        db.session.commit()
        print("Added donations.is_80g_requested")
    else:
        print("donations.is_80g_requested already exists -- skipping")

    # Reconcile Alembic's bookkeeping so `flask db upgrade` behaves normally
    # (reports "already up to date") the next time it's run.
    db.session.execute(text("DELETE FROM alembic_version"))
    db.session.execute(text(
        "INSERT INTO alembic_version (version_num) VALUES (:v)"
    ), {"v": HEAD_REVISION})
    db.session.commit()
    print(f"Stamped alembic_version -> {HEAD_REVISION} (head)")

print("Done. `flask db upgrade` should now report nothing to do.")
