"""One-off repair for a stuck migration.

What happened: `flask db upgrade` failed with
"psycopg2.errors.DuplicateTable: relation 'bace_properties' already exists".
That means the bace_properties table (and, most likely, festivals /
seva_types too, and the new columns on donations) already exist in the
database -- but Alembic's own bookkeeping table (alembic_version) still
thinks the old revision (b7f4c9d21a08) is current, so it tries to create
everything again and fails. This can happen if `flask db upgrade` was
run more than once close together, or against a database that already had
these tables from an earlier attempt.

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

HEAD_REVISION = "d9a27e6b5c31"

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

    # Reconcile Alembic's bookkeeping so `flask db upgrade` behaves normally
    # (reports "already up to date") the next time it's run.
    db.session.execute(text("DELETE FROM alembic_version"))
    db.session.execute(text(
        "INSERT INTO alembic_version (version_num) VALUES (:v)"
    ), {"v": HEAD_REVISION})
    db.session.commit()
    print(f"Stamped alembic_version -> {HEAD_REVISION} (head)")

print("Done. `flask db upgrade` should now report nothing to do.")
