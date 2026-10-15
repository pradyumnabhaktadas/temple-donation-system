"""Production (Render, Python 3.12, newer SQLAlchemy) rejects
``db.paginate(<legacy Query>)`` with ObjectNotExecutableError, while the
older local environment accepts it -- so it passes tests and breaks the
live site (the Donations and Donors pages both shipped broken this way).
Every list in this codebase is built from ``Model.query``, so paginate on
the Query itself."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_module_calls_db_paginate():
    offenders = []
    for path in ROOT.glob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\bdb\.paginate\(", code):
                offenders.append(f"{path.name}:{n}")
    assert not offenders, f"use query.paginate(...), not db.paginate(query): {offenders}"
