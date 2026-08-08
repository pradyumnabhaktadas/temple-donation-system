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

HEAD_REVISION = "c4d68b1e9a52"

app = create_app()

with app.app_context():

    def existing_tables():
        return set(inspect(db.engine).get_table_names())

    def donation_columns():
        return {c["name"] for c in inspect(db.engine).get_columns("donations")}

    def donor_columns():
        return {c["name"] for c in inspect(db.engine).get_columns("donors")}

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

    # --- offline payment reference fields (cheque_number/cheque_bank_name/bank_transaction_id) ---
    if "cheque_number" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN cheque_number VARCHAR(50)"))
        db.session.commit()
        print("Added donations.cheque_number")
    else:
        print("donations.cheque_number already exists -- skipping")

    if "cheque_bank_name" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN cheque_bank_name VARCHAR(150)"))
        db.session.commit()
        print("Added donations.cheque_bank_name")
    else:
        print("donations.cheque_bank_name already exists -- skipping")

    if "bank_transaction_id" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN bank_transaction_id VARCHAR(100)"))
        db.session.commit()
        print("Added donations.bank_transaction_id")
    else:
        print("donations.bank_transaction_id already exists -- skipping")

    # --- cancellation fields ---
    if "cancelled_at" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN cancelled_at TIMESTAMP"))
        db.session.commit()
        print("Added donations.cancelled_at")
    else:
        print("donations.cancelled_at already exists -- skipping")

    if "cancelled_by" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN cancelled_by VARCHAR(100)"))
        db.session.commit()
        print("Added donations.cancelled_by")
    else:
        print("donations.cancelled_by already exists -- skipping")

    if "cancellation_reason" not in donation_columns():
        db.session.execute(text("ALTER TABLE donations ADD COLUMN cancellation_reason VARCHAR(300)"))
        db.session.commit()
        print("Added donations.cancellation_reason")
    else:
        print("donations.cancellation_reason already exists -- skipping")

    # --- preachers + donor relationship/family fields ---
    if "preachers" not in tables:
        db.session.execute(text("""
            CREATE TABLE preachers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(150) NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL,
                created_at TIMESTAMP
            )
        """))
        db.session.commit()
        print("Created preachers")
    else:
        print("preachers already exists -- skipping")

    _donor_text_columns = {
        "donor_type": "VARCHAR(20)",
        "donation_frequency": "VARCHAR(20)",
        "gifts": "VARCHAR(500)",
        "additional_info": "TEXT",
    }
    for col_name, col_type in _donor_text_columns.items():
        if col_name not in donor_columns():
            db.session.execute(text(f"ALTER TABLE donors ADD COLUMN {col_name} {col_type}"))
            db.session.commit()
            print(f"Added donors.{col_name}")
        else:
            print(f"donors.{col_name} already exists -- skipping")

    for col_name in ["dob", "father_dob", "mother_dob", "wife_dob", "marriage_anniversary"]:
        if col_name not in donor_columns():
            db.session.execute(text(f"ALTER TABLE donors ADD COLUMN {col_name} DATE"))
            db.session.commit()
            print(f"Added donors.{col_name}")
        else:
            print(f"donors.{col_name} already exists -- skipping")

    if "connected_preacher_id" not in donor_columns():
        db.session.execute(text("ALTER TABLE donors ADD COLUMN connected_preacher_id INTEGER"))
        db.session.execute(text(
            "ALTER TABLE donors ADD CONSTRAINT fk_donors_connected_preacher_id "
            "FOREIGN KEY (connected_preacher_id) REFERENCES preachers(id)"
        ))
        db.session.commit()
        print("Added donors.connected_preacher_id + FK")
    else:
        print("donors.connected_preacher_id already exists -- skipping")

    # Reconcile Alembic's bookkeeping so `flask db upgrade` behaves normally
    # (reports "already up to date") the next time it's run.
    db.session.execute(text("DELETE FROM alembic_version"))
    db.session.execute(text(
        "INSERT INTO alembic_version (version_num) VALUES (:v)"
    ), {"v": HEAD_REVISION})
    db.session.commit()
    print(f"Stamped alembic_version -> {HEAD_REVISION} (head)")

print("Done. `flask db upgrade` should now report nothing to do.")
