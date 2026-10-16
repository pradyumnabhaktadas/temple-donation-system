"""Deployed migrations are frozen.

Alembic runs a revision once per database and records it. Editing the file of
a revision that has already been applied does nothing for that database, so
the edit silently never takes effect there -- on 15 Oct 2026 a new table was
added to an already-deployed migration, production never got it, and the
center edit page crashed with 'relation preaching_center_purposes does not
exist' until a follow-up migration repaired it.

Every migration that has shipped is listed below with a hash of its content.
To change the schema, add a NEW migration. If a deployed file genuinely needs
an edit that cannot change the resulting schema (e.g. making it run on SQLite
too), update its hash here in the same commit and say why in the commit
message. A brand-new migration is not listed until it has been deployed.
"""
import hashlib
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"

DEPLOYED = {
    "069fa9d71441_index_hot_donation_filter_columns.py": "b1b7371114f0c16213d156d170393a2bbda3313aedee45f366b494694703c038",
    "4e5f6a7b8c9d_add_bace_monthly_rent_ledger.py": "e96304d31baddab9f463d3e9ece3740b8754f97045be8b026ed08a3f807f4ab6",
    "5f6a7b8c9d0e_add_bace_student_to_donations.py": "fec426aa5482e3e6e887ab7c354f0cec47ffdcd8aec01790b7544dc2cc29430a",
    "7b4c2e9a1d63_harden_bace_rent_ledger.py": "d7c1edb04950ea17289af70f6106e37359ae895ce79161bc2b45fbf25d507327",
    "8c5d1e7a2b94_add_bace_rent_adjustments.py": "6b4ceb36ad79786f03374aa9b0c1230d5cafb7244639720fa49cb7670e580867",
    "9a2f7c4e8b31_unique_bace_rent_payment_source_donation.py": "910ab2c85a85ae918f5ea336ffe06d3800e093b512c9a9e48c240f452f0ed578",
    "a1c58f6d0e29_add_live_to_give_purpose_is_80g.py": "76c331ec9341420a0a4427e2bc0a4717819c6a604f3940ab4be407b7b0dcdfdb",
    "a1c9d4e8f2b6_add_birthday_alert_recipients.py": "380ac8554341b6d5c3224f4bb7f7cba49d616cae443fe16444be6211cdb0cabb",
    "a3e62e16cb44_add_receipt_pdf_column.py": "143d8f9af495f6791c2b8b1384e0a3cf6117898272a9f078238108ee6b23f82b",
    "a6f0b2c9d4e1_add_zoho_entry_queue.py": "841559c703bead90c0b48454e2656d078d09092c77397de9ee4a44d923a81871",
    "a7c31f9e2b04_add_donation_cancellation_fields.py": "7fa241192bbd7cee4698b05df17254fedd342097d423834fb1fdd96cfbb79bcc",
    "a91c7f3e2b56_dhoti_kurta_contribution.py": "383d072bc3327431a7938a0d179d6f1e089a76f2ed92624e7628d24911c3d369",
    "a9f24b6d1e37_drop_zoho_form_sheet_url.py": "380bd08bea81bd9ee460ae171286f388d031b9b1152b6ba6c4ba11f9a391cb1e",
    "b2e91a7c4d05_add_campaign_min_amount.py": "2a59be50dbb7a3db5ff6e6aa776e8f7095c6bf5460bcd7a52de085139a965bff",
    "b3f9d61a7c22_add_bace_rent_payments_source_donation.py": "6db84ebd74d9da385d7808eb4ab4e2000c9e1b6038e592723034c4bb87d27897",
    "b6a2e4f81c93_daily_report_recipient_frequency.py": "47d8ae37a16a8053305061338f1e9e7d922795619fff677b6a6ebda0c5cee175",
    "b6c30f8a2e91_add_zoho_form_link_name.py": "fdb2402ac5083821ee0c4be1a4c2ecbd64c72a42ca0f7936737e6cba95d4185f",
    "b7f4c9d21a08_add_extended_payment_audit_fields.py": "30c36a46b5011d67edd9fd14b7ad4411c290adf2e06e201afd19ec46dcd2fd2e",
    "c3a1f7d9e2b4_add_preaching_centers.py": "2d0b857646745d04d59f243d3c5dee7a247ec9326acbd0feb588e73be5edf82e",
    "c4d18e6a2f93_add_camp_name_batch_name.py": "82d2f09ceaac0580e5782cb5ba8c6640faac691f6cfdb99df601010d5b30575e",
    "c4d68b1e9a52_add_preachers_and_donor_relationship_fields.py": "e9325c127fed7d0f4a4dcce55791c01a9acae894a7a0796d74d926fc8186c421",
    "c7e39b5a1d84_add_pending_zoho_submissions.py": "f6ea3e5aacb81c41b2d0d6cbb92c492d5adbf419ce5158c7dcb8846e343c5e6e",
    "c8e51f3a9d47_add_bace_properties.py": "687f5d0ab2e8093ab46f9c35de20de967a7a1b80d84fda8cf27f13d46430c044",
    "c8f42a1b9d63_drop_zoho_submissions.py": "837d0e342cbfea5b37e82c61e623b86da261851d2a9ce0d2e017fff7bb7152d7",
    "d3f8a5c19b76_add_admin_activity_logs.py": "90bb2252c2655a82f208019106b7327adb19ef5137a2d7677d2b729253253713",
    "d4b81e6c3f92_add_bace_students_and_rent_payments.py": "d624527578a37d6ca6ef724bfe83af3eff3b7ed349eb7bbc28c622031ca7dcef",
    "d5f27a1b8c64_add_camps_table.py": "9e1025ad74f3409d86d7f3d3e3de4f7e43d21b799da98776736c34fa67388040",
    "d8b41f7c3e56_drop_pending_zoho_submissions.py": "f7901ec73a9a922045f797f066031b48ca891724f9345517105c8c693ed0bcf6",
    "d9a27e6b5c31_add_festivals_and_seva_types.py": "af01ca7fa3e7521b592353a3c245cc70dbde2ef449d5d12fc1ab4b5b21297002",
    "e2b74a1c8f63_add_live_to_give.py": "e8d5f0a2bc06d15d7965a2668340fc663b10a192eeda0cb8811143ad96003bc1",
    "e4c72a9b8d13_add_zoho_forms.py": "ad3b4a9b07acd349e3a664dae7ff12680b0490a15840632d3162155d7c8d2458",
    "e7c14b92a380_add_razorpay_dispute_fields.py": "7ce47b853ee0a3f4d6ceb3eec6e583e5a30ee926e6c4bce85208bec86d891cd5",
    "e7c2b4a9f105_add_bace_students_email.py": "709afda60eec4bede3ddf03bb14a0c5db1b959e388e16f21ea097742e631d50c",
    "f1a92c7d3e56_add_offline_payment_reference_fields.py": "34413078468c56034746207380681a0529021ed65e44a15961b2c4ada5d4bbc8",
    "f3a8c1d5e720_daily_report_recipients.py": "f7ef85ddee1380dd12e12ab26e86c06d9639f2f16f289819b85893edbc97ffc6",
    "f4a08d3c6e12_add_associated_withs.py": "7f991f81bbb1494cdf915fb7a1e319b69a2917435ae9ed027e5dac96aab8cbcf",
    "f5d18e3c7a24_add_zoho_submissions.py": "fb42a614ec369aff64df497eb83df5ceeacaf4414281b3acbb0845e46ffe6358",
    "f6b8c1d2e3a4_create_preaching_center_purposes.py": "56fccae7c1e266a888f72f23ac05a58b8875c1eb937d13bef39440df644fe7b5",
}


def _digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_deployed_migrations_have_not_been_edited():
    changed = [name for name, digest in DEPLOYED.items() if _digest(VERSIONS / name) != digest]
    assert not changed, (
        "These already-deployed migrations were edited: %s. Add a new migration instead "
        "(see this file's docstring)." % changed
    )


def test_no_deployed_migration_was_deleted():
    missing = [name for name in DEPLOYED if not (VERSIONS / name).exists()]
    assert not missing, f"Deployed migrations are missing: {missing}"
