import datetime
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import get_financial_year, amount_to_words_inr, format_inr, is_valid_pan, is_valid_phone, normalize_phone


class TestFinancialYear:
    def test_april_start_of_new_fy(self):
        assert get_financial_year(datetime.date(2026, 4, 1)) == "2026-27"

    def test_march_end_of_prior_fy(self):
        assert get_financial_year(datetime.date(2026, 3, 31)) == "2025-26"

    def test_mid_year(self):
        assert get_financial_year(datetime.date(2026, 7, 28)) == "2026-27"

    def test_january_still_prior_fy(self):
        assert get_financial_year(datetime.date(2027, 1, 15)) == "2026-27"


class TestAmountToWords:
    def test_zero(self):
        assert amount_to_words_inr(0) == "Zero Rupees Only"

    def test_one_is_singular(self):
        assert amount_to_words_inr(1) == "One Rupee Only"

    def test_hundred(self):
        assert amount_to_words_inr(100) == "One Hundred Rupees Only"

    def test_lakh(self):
        assert amount_to_words_inr(123456) == "One Lakh Twenty Three Thousand Four Hundred Fifty Six Rupees Only"

    def test_crore(self):
        assert amount_to_words_inr(10000000) == "One Crore Rupees Only"

    def test_handles_float_input(self):
        # Amounts come through as Decimal/float from the DB; should round cleanly.
        assert amount_to_words_inr(999.6) == "One Thousand Rupees Only"


class TestIndianNumberFormat:
    def test_small_number_no_grouping(self):
        assert format_inr(999) == "999"

    def test_thousands(self):
        assert format_inr(1000) == "1,000"

    def test_lakh_grouping(self):
        assert format_inr(123456) == "1,23,456"

    def test_crore_grouping(self):
        assert format_inr(12345678) == "1,23,45,678"

    def test_negative(self):
        assert format_inr(-5000) == "-5,000"

    def test_decimals(self):
        assert format_inr(1234.5, decimals=2) == "1,234.50"


class TestPanValidation:
    def test_blank_is_valid_since_optional(self):
        assert is_valid_pan("") is True
        assert is_valid_pan(None) is True

    def test_correct_format(self):
        assert is_valid_pan("ABCDE1234F") is True

    def test_lowercase_is_normalised(self):
        assert is_valid_pan("abcde1234f") is True

    def test_wrong_length_rejected(self):
        assert is_valid_pan("ABCDE123F") is False

    def test_wrong_pattern_rejected(self):
        assert is_valid_pan("1234ABCDEF") is False


class TestPhoneNormalization:
    def test_plain_10_digit_unchanged(self):
        assert normalize_phone("8802081265") == "8802081265"

    def test_plus_91_with_spaces(self):
        assert normalize_phone("+91 88020 81265") == "8802081265"

    def test_91_prefix_no_plus(self):
        assert normalize_phone("918802081265") == "8802081265"

    def test_leading_trunk_zero(self):
        assert normalize_phone("08802081265") == "8802081265"

    def test_space_grouped_no_country_code(self):
        assert normalize_phone("88020 81265") == "8802081265"

    def test_dashes_and_parens(self):
        assert normalize_phone("(+91)-88020-81265") == "8802081265"

    def test_blank_stays_blank(self):
        assert normalize_phone("") == ""
        assert normalize_phone(None) == ""

    def test_unrecognised_shape_left_unchanged(self):
        # Too short to be a real number -- normalize_phone only narrows
        # *recognised* formats, it doesn't guess at garbage.
        assert normalize_phone("12345") == "12345"


class TestPhoneValidation:
    def test_blank_is_valid_since_optional(self):
        assert is_valid_phone("") is True
        assert is_valid_phone(None) is True

    def test_correct_format(self):
        assert is_valid_phone("8802081265") is True

    def test_accepts_any_recognised_format(self):
        assert is_valid_phone("+91 88020 81265") is True
        assert is_valid_phone("88020 81265") is True
        assert is_valid_phone("918802081265") is True

    def test_missing_digit_rejected(self):
        assert is_valid_phone("880208126") is False

    def test_extra_digit_rejected(self):
        assert is_valid_phone("88020812655") is False

    def test_extra_digit_with_country_code_rejected(self):
        assert is_valid_phone("+91 88020 812655") is False

    def test_non_mobile_prefix_rejected(self):
        # Indian mobile numbers only start 6/7/8/9.
        assert is_valid_phone("5802081265") is False
        assert is_valid_phone("1234567890") is False

    def test_garbage_rejected(self):
        assert is_valid_phone("abcdefghij") is False
