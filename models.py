import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from utils import get_financial_year, to_ist


class Preacher(db.Model):
    """A preacher/devotee who maintains a personal relationship with
    specific donors, so the office can track and report on who's
    following up with whom -- e.g. total donors/donation amount per
    preacher, or "which of my donors haven't been assigned to anyone
    yet". Same admin-editable-lookup-list pattern as BaceProperty/
    Festival/SevaType/LiveToGivePurpose, editable from Admin -> Preachers.
    A donor with no `connected_preacher_id` set just means "not yet
    assigned" -- there's no special "Unassigned" row here, blank/NULL
    already means that."""

    __tablename__ = "preachers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    donors = db.relationship("Donor", backref="connected_preacher", lazy="dynamic")

    def __repr__(self):
        return f"<Preacher {self.name}>"


class AssociatedWith(db.Model):
    """Which preaching program, department, devotee, or initiative a donor
    is connected to for a *specific donation* -- e.g. "IYF Dwarka Temple
    Preaching", "Online Preaching", "HG Achyutanand Pr", "IYF Bhakti
    Vriksha -- Sujeet Pr".

    Deliberately a per-donation field (Donation.associated_with_id), not a
    per-donor one like Preacher/Donor.connected_preacher_id -- the same
    donor can give one donation through one preaching program and a later
    one through a different devotee or initiative entirely, and each
    donation needs to record which. It's also deliberately independent of
    Donation Purpose/Campaign: "IYF Dwarka Temple Preaching" (who/what the
    donor is connected to) and "Temple Construction" (what the money is
    for) are two separate questions a donor can answer in any combination,
    so this is never conflated with campaign_id/live_to_give_purpose_id.

    Same admin-editable-lookup-list pattern as BaceProperty/Festival/
    SevaType/LiveToGivePurpose/Preacher, editable from Admin -> Associated
    With. Offered on the Give to Krishna (Live To Give) and Festival Seva
    forms, plus the admin offline-entry forms; optional everywhere (NULL
    just means "not specified")."""

    __tablename__ = "associated_withs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    # Admin-controlled sort position for the public dropdown (Move Up/Move
    # Down in Admin -> Associated With) -- lower sorts first. Deliberately
    # not alphabetical like every other lookup list here: the office wants
    # to put their most-used options up top rather than wherever they land
    # alphabetically. Ties (including the default 0 a freshly-added option
    # starts at until it's moved) fall back to name so ordering is always
    # deterministic.
    display_order = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    donations = db.relationship("Donation", backref="associated_with", lazy="dynamic")

    def __repr__(self):
        return f"<AssociatedWith {self.name}>"


# Donor.donor_type values -- an IYF (ISKCON Youth Forum) donor vs. a
# Live To Give (Nitya Seva) donor. Kept as a plain string rather than a
# separate lookup table since this is a fixed, small, code-meaningful set
# (unlike Preacher, which is an open-ended list office staff maintain).
DONOR_TYPES = ["iyf", "live_to_give"]
DONOR_TYPE_LABELS = {"iyf": "IYF", "live_to_give": "Live To Give"}

# Donor.donation_frequency values -- how often this donor typically gives,
# as characterized by office staff (not auto-computed from donation
# history, since a donor might be "usually monthly" even between gifts).
DONATION_FREQUENCIES = ["one_time", "monthly", "quarterly", "yearly", "occasional"]
DONATION_FREQUENCY_LABELS = {
    "one_time": "One-time", "monthly": "Monthly", "quarterly": "Quarterly",
    "yearly": "Yearly", "occasional": "Occasional",
}


class Donor(db.Model):
    __tablename__ = "donors"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), index=True)
    phone = db.Column(db.String(20), index=True)
    # Separate from `phone` -- the receipt's T&C text specifically asks for
    # a WhatsApp number (that's where Form 10BE gets delivered), which
    # isn't always the same number as the one a donor registers as their
    # primary/callback phone. Falls back to `phone` wherever it's blank
    # (see Donor.whatsapp_or_phone below) so nothing breaks for donors who
    # never fill this in separately.
    whatsapp_number = db.Column(db.String(20))
    pan = db.Column(db.String(10), index=True)
    address = db.Column(db.String(400))
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    pincode = db.Column(db.String(10))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    # -- Relationship-management fields (Admin -> Donors -> Edit only; not
    # collected on any public donation form -- these are gathered by
    # temple staff over time as a relationship develops, not something a
    # donor fills in while paying). See DONOR_TYPES/DONATION_FREQUENCIES
    # above for the fixed option sets.
    donor_type = db.Column(db.String(20))  # "iyf" / "live_to_give", blank = not categorised yet
    connected_preacher_id = db.Column(db.Integer, db.ForeignKey("preachers.id"), nullable=True)
    donation_frequency = db.Column(db.String(20))
    gifts = db.Column(db.String(500))  # free text -- gifts given to/received from this donor
    dob = db.Column(db.Date)
    father_dob = db.Column(db.Date)
    mother_dob = db.Column(db.Date)
    wife_dob = db.Column(db.Date)
    marriage_anniversary = db.Column(db.Date)
    additional_info = db.Column(db.Text)  # notes, preferences, family details, follow-ups, etc.

    donations = db.relationship("Donation", backref="donor", lazy="dynamic")

    @property
    def total_donated(self):
        return sum(d.amount for d in self.donations if d.status == "success")

    @property
    def donation_count(self):
        return self.donations.filter_by(status="success").count()

    @property
    def last_donation_date(self):
        d = (
            self.donations.filter_by(status="success")
            .order_by(Donation.donation_date.desc())
            .first()
        )
        return d.donation_date if d else None

    @property
    def whatsapp_or_phone(self):
        """The number to actually contact this donor on WhatsApp -- their
        dedicated WhatsApp number if they gave one, else their phone."""
        return self.whatsapp_number or self.phone

    def __repr__(self):
        return f"<Donor {self.full_name}>"


class Campaign(db.Model):
    __tablename__ = "campaigns"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    is_80g = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    description = db.Column(db.String(500))
    target_amount = db.Column(db.Numeric(12, 2))
    # Smallest amount accepted for a donation against this campaign, admin-
    # editable from Admin -> Campaigns -> Edit. NULL means no campaign-
    # specific floor beyond the universal "amount must be > 0" check in
    # public.py's create_order(). Currently only "Live To Give" has one set
    # (Rs. 101, migrated in from the old hardcoded check).
    min_amount = db.Column(db.Numeric(12, 2))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    donations = db.relationship("Donation", backref="campaign", lazy="dynamic")

    @property
    def total_collected(self):
        return sum(d.amount for d in self.donations if d.status == "success")

    def __repr__(self):
        return f"<Campaign {self.name}>"


class BaceProperty(db.Model):
    """A specific BACE (Bhaktivedanta Academy for Culture and Education, or
    similar temple-run property) location -- e.g. "Nandgaon BACE",
    "Goverdhan BACE". Donations against the "BACE Contribution" campaign
    record which specific property the payment is for; this list is what
    populates that dropdown on the dedicated BACE Contribution form, and is
    editable from Admin -> BACE so staff can add/rename/retire
    locations without a code change."""

    __tablename__ = "bace_properties"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    donations = db.relationship("Donation", backref="bace_property", lazy="dynamic")

    def __repr__(self):
        return f"<BaceProperty {self.name}>"


class Festival(db.Model):
    """A specific festival/occasion -- e.g. "Janmashtami 2026",
    "Radhashtami". Donations against the "Festivals" campaign record which
    occasion the payment is for; this list is what populates the dropdown
    on the dedicated Festival Seva form, and is editable from Admin ->
    Festivals so staff can add upcoming festivals and retire past ones
    without a code change. `event_date` is optional -- purely for sorting/
    display ("upcoming" ordering), not enforced against the donation date."""

    __tablename__ = "festivals"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    event_date = db.Column(db.Date)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    donations = db.relationship("Donation", backref="festival", lazy="dynamic")

    def __repr__(self):
        return f"<Festival {self.name}>"


class Camp(db.Model):
    """An IYF camp -- e.g. "Utkarsha 2026". Editable from Admin -> IYF
    Camps, and what populates the camp dropdown on the entry form.

    Note what this table is and isn't. It is the *picker*: the list of
    camps staff can choose from. It is not where a donation's camp is
    stored -- Donation.camp_name holds the name as plain text, copied at
    the moment the donation was recorded.

    That looked redundant until the requirement that camps get renamed and
    deleted. With a foreign key, deleting a camp would either take its
    donations with it or leave them pointing at nothing, and the money
    collected at that camp would vanish from the totals. Storing the name
    on the donation means the history is self-contained: delete a camp and
    it simply stops being offered for new entries, while every rupee it
    collected still reports correctly.

    Renaming is handled the other way round -- see admin.camp_edit, which
    rewrites the name onto existing donations too, so a corrected spelling
    doesn't split one camp's total into two.
    """

    __tablename__ = "camps"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    # Retiring a finished camp keeps it out of the entry dropdown without
    # deleting it -- the softer alternative to removal, and reversible.
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def __repr__(self):
        return f"<Camp {self.name}>"


class SevaType(db.Model):
    """A seva/sponsorship tier for festival donations -- e.g. "Annakut
    Seva", "Flower Decoration Seva", "Full Sponsorship". `suggested_amount`
    pre-fills the amount field on the Festival Seva form when a donor picks
    this tier (still editable -- it's a suggestion, not a fixed price).
    Editable from Admin -> Seva Types."""

    __tablename__ = "seva_types"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    suggested_amount = db.Column(db.Numeric(12, 2))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    donations = db.relationship("Donation", backref="seva_type", lazy="dynamic")

    def __repr__(self):
        return f"<SevaType {self.name}>"


class LiveToGivePurpose(db.Model):
    """A donation-purpose option for the "Live To Give" (Nitya Seva)
    collection form -- e.g. "Cow Protection", "Temple Construction",
    "Sudama Seva". Donations against the "Live To Give" campaign record
    which purpose the payment is for; this list is what populates the
    dropdown on the dedicated Live To Give form, and is editable from
    Admin -> Live To Give Purposes so staff can add/rename/retire options
    without a code change."""

    __tablename__ = "live_to_give_purposes"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    # Whether a donation against this purpose can legally get an 80G
    # receipt -- only a fixed set of purposes actually qualify (Food for
    # Life, Charity, Donation, Life Membership, Construction, Annadan, per
    # the temple's accounting rules); everything else is strictly Non-80G,
    # not a donor's choice. See Donation.effective_is_80g, which treats
    # this as a hard override -- a donation against a purpose with
    # is_80g=False can never come out 80G regardless of what was
    # requested. Defaults to False (the "rest are non-80G" majority case)
    # so a newly added purpose doesn't silently become 80G-eligible.
    is_80g = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    donations = db.relationship("Donation", backref="live_to_give_purpose", lazy="dynamic")

    def __repr__(self):
        return f"<LiveToGivePurpose {self.name}>"


class Donation(db.Model):
    __tablename__ = "donations"

    id = db.Column(db.Integer, primary_key=True)
    donor_id = db.Column(db.Integer, db.ForeignKey("donors.id"), nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)
    # Only set for donations against the "BACE Contribution" campaign --
    # which specific property the payment is for. NULL for every other campaign.
    bace_property_id = db.Column(db.Integer, db.ForeignKey("bace_properties.id"), nullable=True)
    # Only set for donations against the "Festivals" campaign via the
    # dedicated Festival Seva form -- which occasion and which seva/
    # sponsorship tier. Both NULL for every other campaign, and seva_type_id
    # can be NULL even for a festival donation (choosing a seva tier is
    # optional on that form).
    festival_id = db.Column(db.Integer, db.ForeignKey("festivals.id"), nullable=True)
    seva_type_id = db.Column(db.Integer, db.ForeignKey("seva_types.id"), nullable=True)
    # Only set for donations against the "Live To Give" campaign -- which
    # purpose (Cow Protection, Temple Construction, Sudama Seva, ...) the
    # payment is for. NULL for every other campaign.
    live_to_give_purpose_id = db.Column(db.Integer, db.ForeignKey("live_to_give_purposes.id"), nullable=True)
    # Which preaching program/department/devotee/initiative this donation
    # is connected to -- see AssociatedWith's docstring. Deliberately
    # independent of campaign_id/live_to_give_purpose_id (Donation
    # Purpose): a donor's answer to "who are you associated with" and
    # "what is this donation for" are two separate questions. Offered on
    # the Give to Krishna (Live To Give) and Festival Seva forms, plus
    # admin offline entry; NULL means "not specified" everywhere.
    associated_with_id = db.Column(db.Integer, db.ForeignKey("associated_withs.id"), nullable=True)
    # The Live To Give form lets the donor choose 80G vs Non-80G for this
    # specific donation (unlike every other form, where 80G-eligibility is
    # fixed per campaign) -- NULL here means "not asked" (every other
    # campaign), in which case effective_is_80g below falls back to the
    # campaign's own is_80g flag.
    is_80g_requested = db.Column(db.Boolean, nullable=True)

    amount = db.Column(db.Numeric(12, 2), nullable=False)
    payment_mode = db.Column(db.String(20), nullable=False)  # online/cash/cheque/bank_transfer
    # pending/success/failed/cancelled. "cancelled" is a deliberate 4th
    # value rather than a separate is_cancelled boolean: every money-total
    # query in this codebase (dashboard stats, campaign progress bars,
    # Donor.total_donated/donation_count/last_donation_date, Form 10BD
    # export, collections export, lapsed-donor report, annual statements)
    # already filters on status == "success" specifically, so a cancelled
    # donation is automatically excluded everywhere for free -- no need to
    # touch a dozen call sites individually. The receipt_number stays
    # assigned and is never reused (see ReceiptCounter docstring); only the
    # status changes, and the /receipt/<id> download route already refuses
    # anything that isn't status == "success", so a cancelled receipt
    # naturally stops being downloadable too. See admin.cancel_donation().
    status = db.Column(db.String(20), default="pending")
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancelled_by = db.Column(db.String(100), nullable=True)  # admin username
    cancellation_reason = db.Column(db.String(300), nullable=True)

    # Offline payment reference details -- only meaningful for
    # payment_mode == "cheque" / "bank_transfer" respectively, entered by
    # staff when logging an offline donation (see admin.manual_donation).
    # Kept as their own columns rather than folded into `remarks` so
    # they're reconcilable fields, not free text -- same reasoning as the
    # razorpay_* fields below, just for the offline equivalents.
    cheque_number = db.Column(db.String(50))
    cheque_bank_name = db.Column(db.String(150))
    bank_transaction_id = db.Column(db.String(100))  # UTR / reference number for bank transfers

    razorpay_order_id = db.Column(db.String(100))
    razorpay_payment_id = db.Column(db.String(100))
    # `receipt` we sent Razorpay when creating the order (donation_<id>) --
    # stored back for a self-contained audit record; known at order-creation
    # time (see public.create_order), not something pulled from a webhook.
    razorpay_order_receipt = db.Column(db.String(100))
    # The fields below are only ever populated by the webhook (see
    # public.razorpay_webhook) -- the browser-side /api/verify-payment call
    # doesn't get this level of detail from Razorpay's checkout response,
    # only the order/payment/signature. If RAZORPAY_WEBHOOK_SECRET isn't
    # configured, these simply stay blank; everything else about the
    # donation flow works exactly the same either way.
    razorpay_status = db.Column(db.String(20))  # created/authorized/captured/failed/refunded
    razorpay_currency = db.Column(db.String(10))  # e.g. "INR"
    razorpay_method = db.Column(db.String(20))  # card/netbanking/wallet/upi/emi
    # Human-readable payment reference matching the method above -- UPI VPA,
    # masked card (network + last 4), bank name, or wallet name. Useful for
    # matching a donation to a bank statement line without needing to look
    # up the raw payload below.
    razorpay_reference = db.Column(db.String(100))
    # UPI-specific: how the payment was initiated -- "collect" (donor pays a
    # request), "intent" (donor opens their UPI app directly), "in_app".
    razorpay_upi_flow = db.Column(db.String(20))
    # Card-specific, broken out separately from razorpay_reference (which
    # already combines these into one display string) for anyone who wants
    # to filter/report on network or type directly.
    razorpay_card_network = db.Column(db.String(30))  # Visa/Mastercard/RuPay/...
    razorpay_card_type = db.Column(db.String(20))  # credit/debit/prepaid
    # Bank-side reference number (RRN/UTR) from Razorpay's acquirer_data --
    # what you'd actually match against a bank statement line for UPI/
    # netbanking payments. Not present for every method or every payment.
    razorpay_utr = db.Column(db.String(50))
    razorpay_fee = db.Column(db.Numeric(12, 2))  # Razorpay's fee, in rupees (what actually lands minus this)
    razorpay_email = db.Column(db.String(200))  # email entered at Razorpay checkout (may differ from donor.email)
    razorpay_contact = db.Column(db.String(20))  # phone entered at Razorpay checkout
    # Full payment.entity payload from the webhook, verbatim, as JSON text --
    # a complete record of everything Razorpay sent, beyond the specific
    # fields pulled out above, in case you ever need to reconcile something
    # those fields don't cover.
    razorpay_raw_payload = db.Column(db.Text)

    # Populated only by the webhook's payment.dispute.* events (see
    # public.razorpay_webhook) -- a donor disputing/charging back a
    # payment we've already captured and possibly issued an 80G receipt
    # for. dispute_status mirrors Razorpay's own values verbatim (created/
    # under_review/action_required/won/lost/closed) rather than being
    # remapped to this app's own vocabulary, so it's always exactly what
    # Razorpay's dashboard shows. All four stay NULL for the overwhelming
    # majority of donations that are never disputed.
    razorpay_dispute_id = db.Column(db.String(100))
    razorpay_dispute_status = db.Column(db.String(30))
    razorpay_dispute_reason = db.Column(db.String(200))
    disputed_at = db.Column(db.DateTime)

    # Captured from the donor's own request when they submitted the form
    # (see public.create_order) -- not from Razorpay at all. Razorpay's
    # webhook payload doesn't include the payer's IP/browser, so this is
    # the closest equivalent: who/what actually hit our own server.
    donor_ip_address = db.Column(db.String(45))  # IPv4 or IPv6
    donor_user_agent = db.Column(db.String(300))

    receipt_number = db.Column(db.String(50), unique=True)
    financial_year = db.Column(db.String(10))
    # The exact PDF as originally issued, stored verbatim (not regenerated
    # on demand) so it stays byte-for-byte the receipt the donor actually
    # got, even if the org's address/logo/template code changes later --
    # important for a legal 80G document. Lives in the database rather than
    # on local disk so it survives redeploys/restarts on hosts with no
    # persistent filesystem, and rides along with your regular Postgres
    # backups instead of needing a separate backup story. See README
    # "Receipt storage" for the migration note on existing installs.
    receipt_pdf = db.Column(db.LargeBinary)

    donation_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    # IYF camp collections. Plain text rather than a Camp table by explicit
    # choice -- camps are short-lived and the data arrives from a Zoho
    # export that already carries the names as strings, so a managed list
    # would mean maintaining records nobody asked for.
    #
    # The trade-off to know about: these group reports by exact string, so
    # "Utkarsha 2026" and "Utkarsha-2026" are two different camps in every
    # total. _normalize_camp_text() below trims and collapses whitespace to
    # take the easy half of that off the table, and the entry form offers
    # the names already in use as a picker, but a genuine misspelling still
    # splits a camp's total until the rows are edited.
    #
    # Indexed because the whole point of storing them is grouping and
    # filtering by camp.
    camp_name = db.Column(db.String(150), index=True)
    batch_name = db.Column(db.String(150), index=True)

    remarks = db.Column(db.String(300))
    recorded_by = db.Column(db.String(100))  # "online" or admin username for manual entries

    # DPDP Act consent trail: previously the donation form's consent
    # checkbox was only gate-checked at submission time ("did they tick
    # it?") and then discarded -- nothing was ever persisted. Now the fact
    # of consent, when it was given, and what text they agreed to are
    # recorded on the donation itself, so there's an actual audit trail
    # per submission rather than just an in-the-moment check.
    consent_given = db.Column(db.Boolean, default=False, nullable=False)
    consent_at = db.Column(db.DateTime)
    consent_version = db.Column(db.String(20))  # see CONSENT_VERSION in config.py

    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    # Auto-updates on every save -- "modified time" for the audit trail
    # (e.g. distinguishing when a donation was first created pending vs.
    # when the webhook/verify-payment call actually finalized it).
    updated_at = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    @property
    def effective_is_80g(self):
        """Whether this specific donation counts as 80G for receipt
        numbering, Form 10BD, and the annual statement. Almost every
        campaign fixes this via Campaign.is_80g -- the one exception is
        Live To Give, where the donor picks 80G/Non-80G per donation
        (is_80g_requested), which takes priority over the campaign default
        when set.

        A donation against a specific Live To Give purpose is further
        constrained by that purpose's own eligibility
        (LiveToGivePurpose.is_80g) -- only a fixed set of purposes (Food
        for Life, Charity, Donation, Life Membership, Construction,
        Annadan) actually qualify for 80G, so a non-eligible purpose can
        never come out 80G here even if is_80g_requested was somehow set
        to True (the public form and admin entry points both validate
        this at intake too, but this is the hard backstop that actually
        controls receipt numbering/Form 10BD, so it can't be bypassed)."""
        if self.live_to_give_purpose_id is not None and not self.live_to_give_purpose.is_80g:
            return False
        if self.is_80g_requested is not None:
            return self.is_80g_requested
        return self.campaign.is_80g

    @property
    def reference_display(self):
        """Single human-readable payment reference for admin tables --
        whichever field is actually populated for this donation's
        payment_mode (Razorpay payment ID for online, cheque number/bank
        for cheque, UTR/transaction ID for bank transfer). None if nothing
        applies (e.g. cash, or an offline entry logged before these fields
        existed)."""
        if self.razorpay_payment_id:
            return self.razorpay_payment_id
        if self.payment_mode == "cheque" and self.cheque_number:
            suffix = f" ({self.cheque_bank_name})" if self.cheque_bank_name else ""
            return f"Cheque #{self.cheque_number}{suffix}"
        # bank_transaction_id is the catch-all reference field: a UTR for a
        # bank transfer, the gateway's transaction/payment id for a payment
        # recorded by hand as "online" (typed in from a Zoho or Razorpay
        # report), a reference number for an IYF camp collection.
        #
        # Deliberately not filtered by payment_mode. It used to allow only
        # bank_transfer, and an online donation logged through the Offline
        # Donation form stored the id but showed it nowhere -- not in the
        # Donations Log, not in the CSV export, not on the receipt, all of
        # which read this one property. Listing the permitted modes here
        # just moves the bug: the IYF camp form offers the same reference
        # field for Cheque while collecting no cheque number, so a
        # mode-by-mode allowlist silently drops that one too. If a human
        # typed a reference into this field, it is the only thread back to
        # the payment in someone else's system, and it gets shown.
        if self.bank_transaction_id:
            return self.bank_transaction_id
        return None

    @property
    def specific_purpose(self):
        """The campaign-specific sub-selection for this donation, if any --
        which BACE property, festival, seva type, or Live To Give purpose
        it was for. Blank for campaigns with no such sub-selection (e.g. a
        plain General Donation). Used anywhere staff need to see *which*
        BACE property (or festival/seva) actually received a contribution,
        not just the parent campaign name -- the Donations Log table,
        donor detail page, and the CSV export all share this."""
        if self.bace_property:
            return self.bace_property.name
        if self.festival:
            name = self.festival.name
            return f"{name} - {self.seva_type.name}" if self.seva_type else name
        if self.seva_type:
            return self.seva_type.name
        if self.live_to_give_purpose:
            return self.live_to_give_purpose.name
        return ""

    def __repr__(self):
        return f"<Donation {self.id} {self.amount}>"


class ReceiptCounter(db.Model):
    __tablename__ = "receipt_counters"

    # The temple's requested numbering scheme is one flat, never-resetting
    # sequence shared by every donation -- 80G-eligible or not, in any
    # financial year -- formatted as "032511/ISK500000", "032511/ISK500001",
    # ... . `financial_year` and `series` are kept as columns (fixed to the
    # sentinel values below) purely so this reuses the same row-locked
    # counter table/mechanism as before; they no longer vary and are no
    # longer encoded into the receipt number string.
    RECEIPT_PREFIX = "032511/ISK"
    START_NUMBER = 500000
    _FY_KEY = "ALL"
    _SERIES_KEY = "ISK"

    id = db.Column(db.Integer, primary_key=True)
    financial_year = db.Column(db.String(10), nullable=False)
    series = db.Column(db.String(10), nullable=False)
    last_number = db.Column(db.Integer, default=0, nullable=False)

    __table_args__ = (db.UniqueConstraint("financial_year", "series", name="uq_fy_series"),)

    @classmethod
    def next_receipt_number(cls, is_80g, date=None):
        """Returns a receipt number guaranteed unique and strictly
        increasing -- this is the number that ultimately appears in the
        Form 10BD filing, so it must never repeat and never be assigned to
        more than one donation.

        The number itself is one global sequence starting at
        "032511/ISK500000" and counting up by 1 for every successful
        donation, regardless of `is_80g` or financial year (per the
        temple's own numbering scheme). `financial_year` is still computed
        and returned here -- callers store it on the donation separately
        for annual statements and the 80G-only Form 10BD export -- it's
        just no longer part of the receipt number string.

        `with_for_update()` takes a row lock on the counter so two donations
        finalizing at the same instant can't both read the same
        `last_number` and collide (Postgres/MySQL enforce this lock;
        SQLite is a no-op here but already serializes writers at the
        database-file level, and the unique index on Donation.receipt_number
        is the hard backstop either way).

        `date` is converted to IST before determining the financial year --
        callers pass either a genuine UTC "now" timestamp (an online
        payment finalizing) or a midnight-anchored calendar date (an
        explicit date typed into a manual-entry/import form); +5:30 never
        crosses a day boundary for the latter (00:00 -> 05:30, same day),
        so this is safe either way, and fixes the former case where a
        donation made just after midnight IST would otherwise still read
        as UTC's previous day and get filed under the wrong financial year.
        """
        fy = get_financial_year(to_ist(date))
        counter = (
            cls.query.filter_by(financial_year=cls._FY_KEY, series=cls._SERIES_KEY)
            .with_for_update()
            .first()
        )
        if counter is None:
            counter = cls(
                financial_year=cls._FY_KEY,
                series=cls._SERIES_KEY,
                last_number=cls.START_NUMBER - 1,
            )
            db.session.add(counter)
            db.session.flush()
        counter.last_number += 1
        db.session.flush()
        return f"{cls.RECEIPT_PREFIX}{counter.last_number:06d}", fy


class DonorLoginOTP(db.Model):
    """One-time codes for donor self-service login. Hashed at rest (same
    approach as passwords) even though these expire in minutes -- no reason
    to store a working credential in plaintext even briefly."""

    __tablename__ = "donor_login_otps"

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    otp_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    consumed = db.Column(db.Boolean, default=False, nullable=False)

    def is_valid(self):
        return not self.consumed and self.expires_at > datetime.datetime.utcnow()

    def set_otp(self, otp):
        from werkzeug.security import generate_password_hash
        self.otp_hash = generate_password_hash(otp, method="pbkdf2:sha256")

    def check_otp(self, otp):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.otp_hash, otp)


class AdminActivityLog(db.Model):
    """Audit trail of mutating admin actions -- who did what, to which
    record, and when. Covers the actions most likely to matter for
    accountability: donor edits/merges, donation cancel/restore, campaign
    CRUD, and admin user management (add/reset-password/unlock/role-
    change/delete). Deliberately does NOT log every read-only page view --
    only actions that actually change data, so this stays a signal-dense
    "what changed" log rather than a full request log.

    `admin_username` is stored as a plain string snapshot (not a foreign
    key to AdminUser) so a log entry survives that account being deleted
    later -- same reasoning as Donation.cancelled_by/recorded_by
    elsewhere in this codebase. See admin.py's log_activity() for how
    rows get written, and admin.activity_log() for the admin-only page
    that lists them."""

    __tablename__ = "admin_activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    admin_username = db.Column(db.String(80), nullable=False)
    action = db.Column(db.String(50), nullable=False)  # e.g. "donor_edit", "donation_cancel"
    target_type = db.Column(db.String(50))  # e.g. "donor", "donation", "campaign", "admin_user"
    target_id = db.Column(db.Integer)
    details = db.Column(db.String(500))  # free-text summary of what changed
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False, index=True)

    def __repr__(self):
        return f"<AdminActivityLog {self.action} by {self.admin_username}>"


class AdminUser(UserMixin, db.Model):
    __tablename__ = "admin_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="staff")  # staff/manager/admin
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    def is_locked(self):
        return bool(self.locked_until and self.locked_until > datetime.datetime.utcnow())

    def register_failed_attempt(self, max_attempts, lockout_minutes):
        self.failed_attempts += 1
        if self.failed_attempts >= max_attempts:
            self.locked_until = datetime.datetime.utcnow() + datetime.timedelta(minutes=lockout_minutes)
            self.failed_attempts = 0

    def register_successful_login(self):
        self.failed_attempts = 0
        self.locked_until = None

    def set_password(self, password):
        # Explicitly use pbkdf2 rather than Werkzeug's default (scrypt).
        # Some Python builds (notably macOS's system/Homebrew Python linked
        # against LibreSSL instead of OpenSSL 1.1+) don't have
        # hashlib.scrypt, which raises AttributeError on generate_password_hash().
        # pbkdf2:sha256 has no such dependency and is broadly supported.
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
