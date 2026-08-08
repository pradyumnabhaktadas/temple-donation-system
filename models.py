import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from utils import get_financial_year


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
    editable from Admin -> BACE Properties so staff can add/rename/retire
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
        when set."""
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
        if self.payment_mode == "bank_transfer" and self.bank_transaction_id:
            return self.bank_transaction_id
        return None

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
        """
        fy = get_financial_year(date)
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
