from typing import Any

_HEX_DIGITS = set("0123456789abcdefABCDEF")


class AddressParseError(ValueError):
    """Raised when an address literal is missing or not in an accepted form."""


def _clean_numeric_text(value: str) -> str:
    return value.strip().replace("_", "")


def is_address_literal(value: Any) -> bool:
    """Return True when a value looks intended as an address literal.

    Accepted unambiguous forms are integers, decimal digit strings, and
    ``0x``-prefixed hex strings. Bare hex with A-F characters also returns
    True so callers can reject it with a clearer ambiguity error.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if not isinstance(value, str):
        return False

    text = _clean_numeric_text(value)
    if not text:
        return False
    if text.lower().startswith("0x"):
        return True
    if text.isdigit():
        return True
    return bool(set(text) <= _HEX_DIGITS and any(c in "abcdefABCDEF" for c in text))


def parse_address(value: Any, *, field: str = "address") -> int:
    """Parse an address from an unambiguous literal.

    Hex addresses must use a ``0x`` prefix. Decimal addresses must be digits
    only. Bare hex strings such as ``4010ab`` are rejected because different
    endpoints historically treated them differently.
    """
    if isinstance(value, bool):
        raise AddressParseError(f"Invalid {field}: booleans are not addresses")
    if isinstance(value, int):
        if value < 0:
            raise AddressParseError(f"Invalid {field}: address must be non-negative")
        return value
    if value is None:
        raise AddressParseError(f"Missing {field}")

    text = _clean_numeric_text(str(value))
    if not text:
        raise AddressParseError(f"Missing {field}")

    if text.lower().startswith("0x"):
        digits = text[2:]
        if not digits or not set(digits) <= _HEX_DIGITS:
            raise AddressParseError(f"Invalid {field} {value!r}; use 0x followed by hex digits")
        return int(digits, 16)

    if text.isdigit():
        return int(text, 10)

    if set(text) <= _HEX_DIGITS and any(c in "abcdefABCDEF" for c in text):
        raise AddressParseError(
            f"Ambiguous {field} {value!r}; use 0x{text} for hex or decimal digits for decimal"
        )

    raise AddressParseError(f"Invalid {field} {value!r}; use 0x... for hex or decimal digits")


def parse_optional_address(value: Any, *, field: str = "address") -> int | None:
    if value is None or value == "":
        return None
    return parse_address(value, field=field)
