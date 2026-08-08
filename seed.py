"""Seeds sample campaigns and a default admin user. Safe to run multiple
times -- it skips anything that already exists.

Usage: python seed.py
"""
from app import create_app
from extensions import db
from models import Campaign, AdminUser, BaceProperty, Festival, SevaType, LiveToGivePurpose

CAMPAIGNS = [
    ("Temple Construction", True),
    ("Deity Worship", True),
    ("Youth Preaching", True),
    ("Festivals", True),
    ("Annadan", True),
    ("General Donations", True),
    ("Other Charitable Activities", True),
    ("BACE Contribution", False),
    ("Youth Camp Registration Fees", False),
    ("Retreats and Event Registrations", False),
    ("Course Fees", False),
    ("Other Internal Collections", False),
    # is_80g here is just the campaign's default -- the Live To Give form
    # lets each donor pick 80G/Non-80G per donation, which overrides this
    # (see Donation.effective_is_80g).
    ("Live To Give", True),
]

# Starting list for the BACE Contribution form's property dropdown -- editable
# afterwards from Admin -> BACE without touching code.
BACE_PROPERTIES = [
    "Gaur Hari Dham BACE",
    "Barsana Dham BACE",
    "Goverdhan BACE",
    "Nandgaon BACE",
    "Yogapitha BACE",
]

# Starting lists for the Festival Seva form -- both editable afterwards
# from Admin -> Festivals / Admin -> Seva Types. No dates are seeded here
# since these shift every year; add/edit actual occasions and dates from
# the admin panel closer to each one.
FESTIVALS = [
    "Janmashtami",
    "Radhashtami",
    "Gaura Purnima",
    "Rama Navami",
    "Nrisimha Chaturdashi",
    "Govardhan Puja",
    "Diwali",
]

SEVA_TYPES = [
    ("Deity Decoration Seva", None),
    ("Flower Decoration Seva", None),
    ("Annakut / Prasad Seva", None),
    ("Abhishek Seva", None),
    ("Full Festival Sponsorship", None),
]

# Starting list for the Live To Give form's donation-purpose dropdown --
# editable afterwards from Admin -> Live To Give Purposes without touching
# code. ("Sudama Seva" appeared twice in the reference list this was
# sourced from -- seeded once here.)
LIVE_TO_GIVE_PURPOSES = [
    "ISKCON Life Membership",
    "Cow Protection (गौ माता की सेवा के लिए)",
    "Temple Construction (मंदिर निर्माण के लिए)",
    "Food for Life (भोजन/प्रसाद वितरण के लिए)",
    "Spreading Sanatan Dharma (सनातन धर्म के प्रचार के लिए)",
    "College Preaching",
    "As per the Need of the Service (सेवा की आवश्यकता अनुसार)",
    "Sudama Seva",
    "Mango Festival",
    "Festivals",
    "IYF Camp / Yatra",
]

app = create_app()

with app.app_context():
    for name, is_80g in CAMPAIGNS:
        if not Campaign.query.filter_by(name=name).first():
            db.session.add(Campaign(name=name, is_80g=is_80g))

    for name in BACE_PROPERTIES:
        if not BaceProperty.query.filter_by(name=name).first():
            db.session.add(BaceProperty(name=name))

    for name in FESTIVALS:
        if not Festival.query.filter_by(name=name).first():
            db.session.add(Festival(name=name))

    for name, suggested_amount in SEVA_TYPES:
        if not SevaType.query.filter_by(name=name).first():
            db.session.add(SevaType(name=name, suggested_amount=suggested_amount))

    for name in LIVE_TO_GIVE_PURPOSES:
        if not LiveToGivePurpose.query.filter_by(name=name).first():
            db.session.add(LiveToGivePurpose(name=name))

    if not AdminUser.query.filter_by(username="admin").first():
        admin = AdminUser(username="admin", role="admin", must_change_password=True)
        admin.set_password("ChangeMe123!")
        db.session.add(admin)
        print("Created admin user -> username: admin | password: ChangeMe123!")
        print("You'll be required to change this password on first login.")

    db.session.commit()
    print("Seed complete.")
