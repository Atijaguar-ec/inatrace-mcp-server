"""
SRI Access Key and Invoice Settlement Validator (Ecuador).
Implements the Modulo 11 verification algorithm for the 49-digit SRI access key
and handles canonical reconciliation strings:
LC:{estab}-{pto}-{secuencial}|CA:{clave_acceso_49d}|ERP:{id_interno_erp}
"""
import re
from typing import Optional
from pydantic import BaseModel, Field


class SriCommentMetadata(BaseModel):
    """Parsed metadata from the canonical SRI comments field."""
    liquidation_code: str = Field(..., description="Liquidación de compra secuencial (ej. 001-002-000000123)")
    access_key: str = Field(..., min_length=49, max_length=49, description="Clave de acceso de 49 dígitos del SRI")
    erp_internal_id: str = Field(..., description="ID interno de transacción en el sistema ERP/báscula")
    is_valid_modulo11: bool = Field(..., description="Indica si la clave de acceso cumple el dígito verificador módulo 11")


CANONICAL_COMMENT_PATTERN = re.compile(
    r"^\s*LC:\s*(?P<lc>[^|]+?)\s*\|\s*CA:\s*(?P<ca>\d{49})\s*\|\s*ERP:\s*(?P<erp>[^|]+?)\s*$"
)


def extract_liquidation_code_from_sri_key(key: str) -> Optional[str]:
    """
    Extracts the official liquidation sequential ({estab}-{pto}-{secuencial})
    from digits 25-39 of an Ecuadorian SRI electronic access key:
    - Digits 25-27: Establecimiento (3 digits)
    - Digits 28-30: Punto de emisión (3 digits)
    - Digits 31-39: Secuencial (9 digits)
    """
    if not key or len(key) != 49 or not key.isdigit():
        return None
    estab = key[24:27]
    pto = key[27:30]
    secuencial = key[30:39]
    return f"{estab}-{pto}-{secuencial}"



def calculate_modulo11_check_digit(digits_48: str) -> int:
    """
    Computes the Modulo 11 check digit for a 48-digit numerical string.
    Weights cycle right-to-left: [2, 3, 4, 5, 6, 7].
    Special cases:
        11 - (sum % 11) == 11 -> 0
        11 - (sum % 11) == 10 -> 1
    """
    if len(digits_48) != 48 or not digits_48.isdigit():
        raise ValueError("Input must be exactly 48 numeric digits.")

    weights = [2, 3, 4, 5, 6, 7]
    total_sum = 0
    weight_idx = 0

    # Process digits from right to left
    for ch in reversed(digits_48):
        digit = int(ch)
        total_sum += digit * weights[weight_idx]
        weight_idx = (weight_idx + 1) % len(weights)

    remainder = total_sum % 11
    verifier = 11 - remainder

    if verifier == 11:
        return 0
    elif verifier == 10:
        return 1
    return verifier


def validate_sri_access_key(key: str) -> bool:
    """
    Validates that a 49-digit SRI access key has the correct Modulo 11 check digit.
    """
    if not key or len(key) != 49 or not key.isdigit():
        return False

    first_48 = key[:48]
    expected_check_digit = int(key[-1])
    computed_check_digit = calculate_modulo11_check_digit(first_48)

    return computed_check_digit == expected_check_digit


def parse_canonical_sri_comment(comment: Optional[str]) -> Optional[SriCommentMetadata]:
    """
    Parses a comments string formatted as:
    LC:{estab}-{pto}-{secuencial}|CA:{clave_acceso_49d}|ERP:{id_interno_erp}
    Returns None if format does not match.
    """
    if not comment:
        return None

    match = CANONICAL_COMMENT_PATTERN.match(comment.strip())
    if not match:
        return None

    lc = match.group("lc")
    ca = match.group("ca")
    erp = match.group("erp")
    is_valid = validate_sri_access_key(ca)

    return SriCommentMetadata(
        liquidation_code=lc,
        access_key=ca,
        erp_internal_id=erp,
        is_valid_modulo11=is_valid
    )


def build_canonical_sri_comment(estab: str, pto: str, secuencial: str, access_key: str, erp_id: str) -> str:
    """
    Builds the canonical reconciliation comment string:
    LC:{estab}-{pto}-{secuencial}|CA:{clave_acceso_49d}|ERP:{id_interno_erp}
    """
    lc = f"{estab.zfill(3)}-{pto.zfill(3)}-{secuencial.zfill(9)}"
    return f"LC:{lc}|CA:{access_key}|ERP:{erp_id}"
