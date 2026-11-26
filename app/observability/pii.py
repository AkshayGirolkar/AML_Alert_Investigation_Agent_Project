"""PII masking for traces (F13): pure, deep, idempotent.

Two layers: (1) values under known PII keys are replaced whole; (2) free text is
scanned with patterns (email, phone, IBAN, SSN, long digit runs such as account
numbers). Evidence IDs (`ev-...`), amounts and other numbers are left alone.
"""

import re
from typing import Any

MASK = "[MASKED]"

# Keys whose whole value is personal data (matched case-insensitively on the key name).
PII_KEYS = frozenset(
    {
        "name",
        "full_name",
        "customer_name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "address",
        "date_of_birth",
        "dob",
        "occupation",
        "national_id",
        "ssn",
        "account",
        "account_number",
        "account_id",
        "entity_id",
        "sender_account",
        "receiver_account",
        "analyst_id",
        "api_key",
        "authorization",
    }
)

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # email
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),  # IBAN
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN
    re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)"),  # phone-like
    re.compile(r"\b(?=[0-9A-F]*[A-F])[0-9A-F]{8,}\b"),  # IBM account ids are hex
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),  # API keys
]


def mask_text(text: str) -> str:
    for pat in _PATTERNS:
        text = pat.sub(MASK, text)
    return text


def mask(obj: Any) -> Any:
    """Return a masked deep copy of dict/list/str structures."""
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        return {
            k: (
                MASK
                if isinstance(k, str) and k.lower() in PII_KEYS and v is not None
                else mask(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list | tuple):
        return [mask(v) for v in obj]
    return obj
