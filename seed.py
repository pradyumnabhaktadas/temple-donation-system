"""Seeds sample campaigns and a default admin user. Safe to run multiple
times -- it skips anything that already exists.

Usage: python seed.py
"""
from app import create_app
from extensions import db
from models import Campaign, AdminUser, BaceProperty, Festival, SevaType, LiveToGivePurpose, AssociatedWith

CAMPAIGNS = [
    # (name, is_80g, min_amount, suppress_receipt) -- min_amount is None
    # unless noted; only Live To Give currently has a floor (Rs. 101,
    # admin-editable from Admin -> Campaigns -> Edit). suppress_receipt is
    # False for everything except Dhoti Kurta Contribution: a receipt
    # number/PDF is still generated for those donations same as any
    # other, it's just never emailed or sent on WhatsApp to the
    # contributor (see Campaign.suppress_receipt's docstring). This only
    # affects fresh installs/resets (seeding skips anything that already
    # exists by name) -- keeping this in sync with whatever's actually
    # configured live means a reset_data.py + seed.py cycle restores the
    # real settings instead of silently dropping back to defaults.
    ("Temple Construction", True, None, False),
    ("Deity Worship", True, None, False),
    ("Youth Preaching", True, None, False),
    ("Festivals", False, None, False),
    ("Annadan", True, None, False),
    ("General Donations", True, None, False),
    ("Other Charitable Activities", True, None, False),
    ("BACE Contribution", False, None, False),
    ("Youth Camp Registration Fees", False, None, False),
    ("Retreats and Event Registrations", False, None, False),
    ("Course Fees", False, None, False),
    ("Other Internal Collections", False, None, False),
    # is_80g here is just the campaign's default -- the Live To Give form
    # lets each donor pick 80G/Non-80G per donation, which overrides this
    # (see Donation.effective_is_80g).
    ("Live To Give", True, 101, False),
    # Deliberately not linked from anywhere in the main site -- only
    # reachable via the small footer link (see base.html). A receipt is
    # generated for these same as any other donation, just never emailed
    # or WhatsApp'd (suppress_receipt=True); see the dedicated
    # /dhoti-kurta-contribution form for the (intentionally minimal)
    # Name/Mobile/Amount collection flow.
    ("Dhoti Kurta Contribution", False, None, True),
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
#
# is_80g: only six purposes are actually 80G-eligible per the temple's
# accounting rules -- Food for Life, Charity, Donation, Life Membership,
# Construction, Annadan -- everything else is Non-80G. This only affects
# fresh installs (seeding skips anything that already exists by name); an
# existing deployment sets this per-purpose from Admin -> Live To Give
# Purposes -> "Mark 80G Eligible" instead.
LIVE_TO_GIVE_PURPOSES = [
    ("ISKCON Life Membership", True),
    ("Cow Protection (गौ माता की सेवा के लिए)", False),
    ("Temple Construction (मंदिर निर्माण के लिए)", True),
    ("Food for Life (भोजन/प्रसाद वितरण के लिए)", True),
    ("Spreading Sanatan Dharma (सनातन धर्म के प्रचार के लिए)", False),
    ("College Preaching", False),
    ("As per the Need of the Service (सेवा की आवश्यकता अनुसार)", False),
    ("Sudama Seva", False),
    ("Mango Festival", False),
    ("Festivals", False),
    ("IYF Camp / Yatra", False),
    ("Charity", True),
    ("Donation", True),
    ("Annadan", True),
]

# Starting list for the "I am associated with" dropdown on the donation
# forms -- editable afterwards from Admin -> Associated With without
# touching code. Order here becomes the initial display_order (0, 10, 20,
# ...), matching the seed rows the associated_withs migration itself
# inserts, so a fresh install (this file) and an existing deployment
# (migration) start out identical.
ASSOCIATED_WITH_OPTIONS = [
    "IYF Dwarka Temple Preaching",
    "Online Preaching",
    "HG Achyutanand Pr",
    "IYF Bhakti Vriksha - Sujeet Pr",
    "IYF Bhakti Vriksha - HG Sri Gaur Pr",
    "IYF Bhakti Vriksha - General",
    "College Preaching",
    "HG Veer Chaitanya Pr",
]

app = create_app()

with app.app_context():
    for name, is_80g, min_amount, suppress_receipt in CAMPAIGNS:
        if not Campaign.query.filter_by(name=name).first():
            db.session.add(Campaign(
                name=name, is_80g=is_80g, min_amount=min_amount, suppress_receipt=suppress_receipt,
            ))

    for name in BACE_PROPERTIES:
        if not BaceProperty.query.filter_by(name=name).first():
            db.session.add(BaceProperty(name=name))

    for name in FESTIVALS:
        if not Festival.query.filter_by(name=name).first():
            db.session.add(Festival(name=name))

    for name, suggested_amount in SEVA_TYPES:
        if not SevaType.query.filter_by(name=name).first():
            db.session.add(SevaType(name=name, suggested_amount=suggested_amount))

    for name, is_80g in LIVE_TO_GIVE_PURPOSES:
        if not LiveToGivePurpose.query.filter_by(name=name).first():
            db.session.add(LiveToGivePurpose(name=name, is_80g=is_80g))

    for i, name in enumerate(ASSOCIATED_WITH_OPTIONS):
        if not AssociatedWith.query.filter_by(name=name).first():
            db.session.add(AssociatedWith(name=name, display_order=i * 10))

    if not AdminUser.query.filter_by(username="admin").first():
        admin = AdminUser(username="admin", role="admin", must_change_password=True)
        admin.set_password("ChangeMe123!")
        db.session.add(admin)
        print("Created admin user -> username: admin | password: ChangeMe123!")
        print("You'll be required to change this password on first login.")

    db.session.commit()
    print("Seed complete.")
