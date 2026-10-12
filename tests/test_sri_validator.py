"""
Tests for SRI Modulo 11 check digit algorithm and canonical comment parsing.
"""
import unittest
from src.domain.sri_validator import (
    calculate_modulo11_check_digit,
    validate_sri_access_key,
    parse_canonical_sri_comment,
    extract_liquidation_code_from_sri_key
)



class TestSriValidator(unittest.TestCase):

    def test_calculate_modulo11_check_digit_known_valid(self):
        # Sample 48 digits: 111020260109923456780011001002000001234123456781
        first_48 = "111020260109923456780011001002000001234123456781"
        computed = calculate_modulo11_check_digit(first_48)
        self.assertIn(computed, range(10))
        
        # Test full 49-digit key with computed digit
        full_key = first_48 + str(computed)
        self.assertTrue(validate_sri_access_key(full_key))

    def test_validate_sri_access_key_invalid_digit(self):
        first_48 = "111020260109923456780011001002000001234123456781"
        computed = calculate_modulo11_check_digit(first_48)
        wrong_digit = (computed + 1) % 10
        invalid_key = first_48 + str(wrong_digit)
        self.assertFalse(validate_sri_access_key(invalid_key))

    def test_validate_sri_access_key_wrong_length(self):
        self.assertFalse(validate_sri_access_key("12345"))
        self.assertFalse(validate_sri_access_key(""))
        self.assertFalse(validate_sri_access_key("1" * 48))
        self.assertFalse(validate_sri_access_key("1" * 50))

    def test_validate_sri_access_key_non_numeric(self):
        key = ("1" * 48) + "X"
        self.assertFalse(validate_sri_access_key(key))

    def test_parse_canonical_sri_comment_valid(self):
        first_48 = "111020260109923456780011001002000001234123456781"
        computed = calculate_modulo11_check_digit(first_48)
        access_key = first_48 + str(computed)

        comment = f"LC:001-002-000001234|CA:{access_key}|ERP:TRANS-9901"
        parsed = parse_canonical_sri_comment(comment)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.liquidation_code, "001-002-000001234")
        self.assertEqual(parsed.access_key, access_key)
        self.assertEqual(parsed.erp_internal_id, "TRANS-9901")
        self.assertTrue(parsed.is_valid_modulo11)

    def test_parse_canonical_sri_comment_invalid_pattern(self):
        self.assertIsNone(parse_canonical_sri_comment("Random comment text"))
        self.assertIsNone(parse_canonical_sri_comment("LC:001|CA:123"))
        self.assertIsNone(parse_canonical_sri_comment(None))
        self.assertIsNone(parse_canonical_sri_comment(""))

    def test_extract_liquidation_code_from_sri_key(self):
        # Key with estab '001', pto '002', secuencial '000001234'
        # Digits 1-24: 111020260109923456780011 (24 digits)
        # Digits 25-27: 001
        # Digits 28-30: 002
        # Digits 31-39: 000001234
        # Digits 40-49: 1234567812 (10 digits)
        key = "111020260109923456780011" + "001" + "002" + "000001234" + "1234567812"
        self.assertEqual(len(key), 49)
        lc = extract_liquidation_code_from_sri_key(key)
        self.assertEqual(lc, "001-002-000001234")

        # Invalid cases
        self.assertIsNone(extract_liquidation_code_from_sri_key(""))
        self.assertIsNone(extract_liquidation_code_from_sri_key("123"))

    def test_parse_canonical_sri_comment_with_whitespace(self):
        first_48 = "111020260109923456780011001002000001234123456781"
        computed = calculate_modulo11_check_digit(first_48)
        access_key = first_48 + str(computed)

        comment = f"  LC: 001-002-000001234  |  CA: {access_key}  |  ERP: TRANS-9901  "
        parsed = parse_canonical_sri_comment(comment)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.liquidation_code, "001-002-000001234")
        self.assertEqual(parsed.access_key, access_key)
        self.assertEqual(parsed.erp_internal_id, "TRANS-9901")
        self.assertTrue(parsed.is_valid_modulo11)

    def test_build_canonical_sri_comment(self):
        from src.domain.sri_validator import build_canonical_sri_comment
        res = build_canonical_sri_comment(
            estab="1", pto="2", secuencial="1234",
            access_key="1" * 49, erp_id="ERP-1"
        )
        self.assertEqual(res, "LC:001-002-000001234|CA:" + ("1" * 49) + "|ERP:ERP-1")



if __name__ == "__main__":
    unittest.main()
