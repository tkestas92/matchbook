"""Synthetic payment and contact details printed on invoices (decision 037).

They exist only to exercise PII masking (task 2.3) and never go into the ground-truth JSON:
extraction never needs them (data minimization). Every value is fake by construction:
- IBAN: Lithuanian format with the bank code 99999, which no Lithuanian bank uses;
  the check digits are valid (ISO 13616 mod-97), so masking must check them.
- email: the reserved `.example` TLD (RFC 2606), never a real domain.
- phone: the UK range 020 7946 0000-0999, which Ofcom reserves for drama, so it is never a
  real subscriber.
"""

import re
from dataclasses import dataclass

from generator.world import rng_for

IBAN_COUNTRY = "LT"
FAKE_BANK_CODE = "99999"
IBAN_ACCOUNT_DIGITS = 11
PHONE_PREFIX = "+44 20 7946 0"


@dataclass(frozen=True)
class VendorContact:
    iban: str  # compact form, no spaces
    email: str
    phone: str


def vendor_contact(seed: int, vendor: str) -> VendorContact:
    rng = rng_for(seed, "documents", "contact", vendor)
    account = "".join(str(rng.randrange(10)) for _ in range(IBAN_ACCOUNT_DIGITS))
    return VendorContact(
        iban=make_iban(IBAN_COUNTRY, FAKE_BANK_CODE + account),
        email=f"billing@{slug(vendor)}.example",
        phone=f"{PHONE_PREFIX}{rng.randrange(1000):03d}",
    )


def make_iban(country: str, bban: str) -> str:
    return f"{country}{iban_check_digits(country, bban)}{bban}"


def iban_check_digits(country: str, bban: str) -> str:
    """ISO 13616: 98 - (BBAN + country + "00" as digits) mod 97, two digits."""
    return f"{98 - iban_number(bban + country + '00') % 97:02d}"


def iban_is_valid(iban: str) -> bool:
    compact = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    return iban_number(compact[4:] + compact[:4]) % 97 == 1


def iban_number(text: str) -> int:
    """Letters become 10-35, digits stay, and the result is read as one integer."""
    return int("".join(str(int(char, 36)) for char in text))


def grouped_iban(iban: str) -> str:
    """The IBAN in groups of four, as printed on invoices."""
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
