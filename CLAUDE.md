# Working notes for Claude

## Run the code. Do not just read it.

The single biggest source of bugs reaching the user in this project has
been verifying changes by compiling and parsing them instead of executing
them. Compiling proves syntax. It does not prove a `<form>` can legally
sit inside a `<tr>`, that a confirmation dialog survives an apostrophe in
a camp name, or that an uploaded file's stream type has the method the
code calls. All three shipped that way.

**Before saying anything is verified, run the test suite.** It is
runnable from the sandbox:

```bash
mkdir -p /tmp/boot && cat > /tmp/boot/sitecustomize.py <<'PY'
import sys
sys.path.append("/sessions/<session>/mnt/temple-donation-system/venv/lib/python3.9/site-packages")
PY
cd /sessions/<session>/mnt/temple-donation-system
PYTHONPATH=/tmp/boot PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q
```

The repo carries its own virtualenv. It's a macOS/py3.9 venv, so:

- **Append** it to `sys.path`, never prepend. The system Linux Pillow must
  win over the venv's macOS build, or `reportlab` fails to import. Flask,
  SQLAlchemy, Jinja and the rest are pure Python and load fine.
- `PYTHONDONTWRITEBYTECODE=1` keeps stray `.pyc` files out of the repo.
- Flask-Limiter isn't installed; the warning at boot is expected.

There is no network, so `pip install` will not work. Everything needed is
already in the venv or the system packages.

## When adding a feature, add tests that drive it

Not tests that assert on internals — tests that POST to the route with the
Flask test client and check what landed in the database. `test_iyf_camps.py`
is the model to copy. Every bug this project has shipped would have been
caught by one.

Pages that render only when data exists (the dashboard's abandoned-donation
panel) or only past a time threshold need that state set up explicitly, or
the test passes without exercising anything.

## Deployment

- Production is Render, Postgres, Python 3.12. Schema is owned by Alembic:
  `flask db upgrade` runs as the pre-deploy command.
- `create_app()` only calls `db.create_all()` outside production. Both
  running together means Alembic loses track of tables it didn't create,
  and the next new-table migration fails with DuplicateTable. This already
  happened once.
- The sandbox cannot push. Hand the user `git pull && git push`, and say
  plainly when a deploy also needs `flask db upgrade`.

## Things that bite in this codebase

- **CSV uploads**: use `_csv_reader_from_upload()`. Wrapping the raw
  upload stream in `io.TextIOWrapper` breaks below Python 3.11.
- **Destructive confirmations**: use `data-confirm="..."` with the shared
  handler in `base_admin.html`. Never splice a user-entered name into an
  inline `onsubmit` — an apostrophe silently removes the prompt.
- **Per-row forms in tables**: declare the `<form>` outside the table and
  attach controls with the HTML5 `form="..."` attribute. A `<form>` inside
  a `<tr>` gets hoisted out and its inputs stop submitting.
- **The test suite must not depend on `.env`.** `app.py` calls
  `load_dotenv()`, so `conftest.py` pins every integration to "not
  configured". Tests needing one enabled must say so explicitly.
- **Receipt PDFs** carry the donor's name, address and PAN, and donation
  ids are sequential. `/receipt/<id>` requires a signed token — build links
  with the `receipt_token()` Jinja global.
