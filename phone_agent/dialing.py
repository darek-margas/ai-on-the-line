# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Which numbers the agent may dial, and in what form it hands them to the PBX.

The rules are settings (DIAL_*), checked with Google's libphonenumber data, so
any country's numbering plan works. Emergency numbers are always refused,
whatever the settings say: an AI should never ring an emergency line.
"""

import re
from dataclasses import dataclass, field

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType, shortnumberinfo

# Number types that can be allowed with DIAL_ALLOW.
TYPES = {
    "mobile": {PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE},
    "fixed_line": {PhoneNumberType.FIXED_LINE, PhoneNumberType.FIXED_LINE_OR_MOBILE},
    "toll_free": {PhoneNumberType.TOLL_FREE},
    "shared_cost": {PhoneNumberType.SHARED_COST},  # e.g. Australian 13/1300
    "premium": {PhoneNumberType.PREMIUM_RATE},
    "voip": {PhoneNumberType.VOIP},
    "personal": {PhoneNumberType.PERSONAL_NUMBER},
    "uan": {PhoneNumberType.UAN},
}

# Refused in every country, before anything else is looked at.
ALWAYS_EMERGENCY = {"000", "106", "110", "111", "112", "119", "911", "999", "0118"}


@dataclass
class DialRules:
    country: str = "AU"
    allow: set[str] = field(default_factory=lambda: {"mobile", "fixed_line", "toll_free"})
    allow_international: bool = False
    # If set (e.g. "02"), numbers dialled without an area code are accepted when
    # they're valid with it, and passed to the PBX as typed.
    local_area_code: str = ""
    extensions: str = r"[1-9]\d{1,4}"  # internal extensions, dialled as typed
    allow_pattern: str = ""  # extra numbers to allow (regex on the digits)
    block_pattern: str = ""  # numbers to refuse even if otherwise allowed
    dial_format: str = "national"  # or "e164"

    def describe(self) -> str:
        parts = [f"{', '.join(sorted(self.allow))} numbers in {self.country}"]
        if self.local_area_code:
            parts.append(f"local numbers in area {self.local_area_code}")
        if self.allow_international:
            parts.append("international numbers")
        parts.append("internal extensions")
        return "; ".join(parts) + " (never emergency numbers)"


def _is_emergency(digits: str, region: str) -> bool:
    bare = digits.lstrip("+")
    if bare in ALWAYS_EMERGENCY:
        return True
    try:
        # "connects": 9111 in the US still reaches 911.
        return (shortnumberinfo.is_emergency_number(bare, region)
                or shortnumberinfo.connects_to_emergency_number(bare, region))
    except Exception:
        return False


def _prefixed_emergency(digits: str, region: str) -> bool:
    """9000 or 0112: an emergency number behind the 9 or 0 many PBXs take for an
    outside line."""
    return len(digits) >= 3 and digits[0] in "90" and _is_emergency(digits[1:], region)


def _public_short(digits: str, region: str) -> bool:
    """A short number the public network knows (988, 1223, 101 in the UK): not ours."""
    try:
        return shortnumberinfo.is_valid_short_number_for_region(phonenumbers.parse(digits, region), region)
    except Exception:
        return False


def is_extension(phone: str, rules: DialRules) -> bool:
    """An internal extension: matches DIAL_EXTENSIONS and isn't a public short
    number, which would otherwise skip approval and calling hours."""
    digits = dial_digits(phone)
    return bool(rules.extensions and re.fullmatch(rules.extensions, digits)
                and not _public_short(digits, rules.country.upper()))


def dial_digits(phone: str) -> str:
    """The number as typed, down to digits and a leading +: "02 5550-1234" -> "0255501234"."""
    return re.sub(r"[^\d+]", "", phone)


def normalize_number(phone: str, rules: DialRules | None = None) -> str:
    """The number to dial, in the form the PBX expects, or ValueError if the
    rules don't allow it."""
    rules = rules or DialRules()
    number = _normalize(phone, rules)
    if rules.block_pattern:
        # However it was written: "+61 491 570 156" is the blocked 0491570156.
        forms = {dial_digits(phone).lstrip("+"), number.lstrip("+")}
        try:
            parsed = phonenumbers.parse(number, rules.country.upper())
            forms.add(re.sub(r"\D", "", phonenumbers.format_number(parsed, PhoneNumberFormat.NATIONAL)))
            forms.add(re.sub(r"\D", "", phonenumbers.format_number(parsed, PhoneNumberFormat.E164)))
        except phonenumbers.NumberParseException:
            pass  # an extension
        if any(re.fullmatch(rules.block_pattern, form) for form in forms):
            raise ValueError(f"won't dial {phone!r}: the dialling rules allow {rules.describe()}")
    return number


def _normalize(phone: str, rules: DialRules) -> str:
    region = rules.country.upper()
    digits = dial_digits(phone)
    refuse = f"won't dial {phone!r}: the dialling rules allow {rules.describe()}"
    if not digits or _is_emergency(digits, region):
        raise ValueError(f"won't dial {phone!r}: emergency or empty number")
    listed = bool(rules.allow_pattern and re.fullmatch(rules.allow_pattern, digits.lstrip("+")))
    if _prefixed_emergency(digits, region) and not listed:
        raise ValueError(f"won't dial {phone!r}: behind an outside-line prefix it is an emergency number "
                         "(if it's a real extension, list it in DIAL_ALLOW_PATTERN)")
    if is_extension(digits, rules):
        return digits
    if listed:
        return digits.lstrip("+")

    def parse(text: str):
        try:
            number = phonenumbers.parse(text, region)
        except phonenumbers.NumberParseException:
            return None
        return number if phonenumbers.is_valid_number(number) else None

    number = parse(digits)
    if number is None and rules.local_area_code and not digits.startswith(("+", "0")):
        # "5550 1234" in Sydney: valid as (02) 5550 1234, dialled as typed.
        with_area = parse(rules.local_area_code + digits)
        if with_area is not None and _type_allowed(with_area, rules):
            return digits
    if number is None:
        # Some apps save numbers as 61... without the +.
        number = parse("+" + digits) if not digits.startswith("+") else None
    if number is None:
        raise ValueError(refuse)
    home = phonenumbers.country_code_for_region(region)
    # Sharing a country code isn't being in the country: +1 876 is Jamaica.
    abroad = number.country_code != home or not phonenumbers.is_valid_number_for_region(number, region)
    if abroad and not rules.allow_international:
        raise ValueError(refuse)
    if not _type_allowed(number, rules):
        raise ValueError(refuse)
    if rules.dial_format == "e164":
        return phonenumbers.format_number(number, PhoneNumberFormat.E164)
    if number.country_code != home:
        return re.sub(r"\D", "", phonenumbers.format_out_of_country_calling_number(number, region))
    return re.sub(r"\D", "", phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL))


def _type_allowed(number, rules: DialRules) -> bool:
    kind = phonenumbers.number_type(number)
    return any(kind in TYPES.get(name, set()) for name in rules.allow)
