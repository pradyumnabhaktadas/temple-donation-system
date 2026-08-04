"""Seeds sample campaigns and a default admin user. Safe to run multiple
times -- it skips anything that already exists.

Usage: python seed.py
"""
from app import create_app
from extensions import db
from models import Campaign, AdminUser

CAMPAIGNS = [
    ("Temple Construction", True),
    ("Deity Worship", True),
    ("Youth Preaching", True),
    ("Festivals", True),
    ("Annadan", True),
    ("General Donations", True),
    ("Other Charitable Activities", True),
    ("BACE Rent", False),
    ("Youth Camp Registration Fees", False),
    ("Retreats and Event Registrations", False),
    ("Course Fees", False),
    ("Other Internal Collections", False),
]

app = create_app()

with app.app_context():
    for name, is_80g in CAMPAIGNS:
        if not Campaign.query.filter_by(name=name).first():
            db.session.add(Campaign(name=name, is_80g=is_80g))

    if not AdminUser.query.filter_by(username="admin").first():
        admin = AdminUser(username="admin", role="admin", must_change_password=True)
        admin.set_password("ChangeMe123!")
        db.session.add(admin)
        print("Created admin user -> username: admin | password: ChangeMe123!")
        print("You'll be required to change this password on first login.")

    db.session.commit()
    print("Seed complete.")
