"""Bring the database up to date, whatever state it's in. Run as Render's
pre-deploy command, before traffic reaches the new code.

Why this exists rather than just `flask db upgrade`:

This project's migration chain has no initial-schema migration. Its
earliest revision (a3e62e16cb44) adds columns to `donations`, assuming the
table is already there -- because it always was, created by
db.create_all() on boot. Alembic has only ever handled the incremental
changes on top.

That means neither command works on its own for a brand new database:

  - `flask db upgrade` alone fails immediately with "no such table:
    donations", because nothing in the chain creates it.
  - create_all() plus `flask db upgrade` fails the other way: create_all
    builds the *current* schema, so every migration then tries to add a
    column or table that already exists (this is the DuplicateTable
    failure seen deploying the camps table).

Neither shows up while a database is being grown incrementally, which is
why production has been fine. It shows up the day you need to rebuild
from nothing -- a new host, or recovery from data loss -- which is the
worst possible day to discover it.

So the state is checked first, and exactly one of two things happens:

  Existing database (has alembic_version)
      Normal `upgrade()`. This is every ordinary deploy, including
      production today. Behaviour is unchanged.

  Empty database (no tables at all)
      create_all() to build the current schema, then stamp Alembic at
      head to record that it is up to date. No migration is replayed,
      so nothing collides.

A third state -- tables present but no alembic_version -- is ambiguous:
the database could be at any point in the chain's history, and guessing
wrong either skips migrations that were needed or replays ones that
weren't. This refuses to act and explains the two ways out. On a
database holding donation records and issued 80G receipts, stopping is
the right answer.
"""
import sys


# Tables that exist from the very first version of this app, so their
# presence means "this database has real history", not "half-built".
_CORE_TABLES = {"donors", "donations", "campaigns"}


def main():
    from flask_migrate import stamp, upgrade

    from app import create_app
    from extensions import db

    app = create_app()
    with app.app_context():
        tables = set(db.inspect(db.engine).get_table_names())

        if "alembic_version" in tables:
            print("Existing database detected -- running migrations.")
            upgrade()
            print("Migrations up to date.")
            return 0

        if not tables:
            print("Empty database detected -- creating the schema from the models.")
            db.create_all()
            stamp()
            print("Schema created and stamped at head. No migrations replayed.")
            return 0

        print(
            "Refusing to touch this database automatically.\n\n"
            f"It already has tables ({', '.join(sorted(tables & _CORE_TABLES)) or 'some'}) "
            "but no alembic_version table, so there's no record of which migrations have "
            "run. Guessing would either skip a migration that was needed or replay one "
            "that wasn't.\n\n"
            "If this database is already fully up to date with the current models:\n"
            "    flask db stamp head\n\n"
            "If it predates some migrations, stamp it at the revision it actually matches "
            "and then upgrade:\n"
            "    flask db stamp <revision>\n"
            "    flask db upgrade\n\n"
            "`flask db history` lists the revisions. Take a backup first.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
