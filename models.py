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


class Donation(db.Model):
    __tablename__ = "donations"

    id = db.Column(db.Integer, primary_key=True)
    donor_id = db.Column(db.Integer, db.ForeignKey("donors.id"), nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"), nullable=False)

    amount = db.Column(db.Numeric(12, 2), nullable=False)
    payment_mode = db.Column(db.String(20), nullable=False)  # online/cash/cheque/bank_transfer
    status = db.Column(db.String(20), default="pending")  # pending/success/failed

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
