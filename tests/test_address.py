import pytest

# ---------- is_address_literal ----------


def test_is_address_literal_accepts_int(address_module):
    assert address_module.is_address_literal(0) is True
    assert address_module.is_address_literal(0x401000) is True


def test_is_address_literal_rejects_bool(address_module):
    # bool is a subclass of int, but we don't want True/False treated as addresses
    assert address_module.is_address_literal(True) is False
    assert address_module.is_address_literal(False) is False


def test_is_address_literal_rejects_non_address_types(address_module):
    assert address_module.is_address_literal(None) is False
    assert address_module.is_address_literal(3.14) is False
    assert address_module.is_address_literal([0x400000]) is False
    assert address_module.is_address_literal({"x": 1}) is False


def test_is_address_literal_accepts_decimal_string(address_module):
    assert address_module.is_address_literal("1234") is True
    assert address_module.is_address_literal("  4096  ") is True
    assert address_module.is_address_literal("4_096") is True


def test_is_address_literal_accepts_hex_prefixed(address_module):
    assert address_module.is_address_literal("0x401000") is True
    assert address_module.is_address_literal("0X401000") is True
    assert address_module.is_address_literal("  0xdeadbeef  ") is True


def test_is_address_literal_accepts_bare_hex_with_letters(address_module):
    # bare hex returns True so parse_address can produce an "ambiguous" message
    assert address_module.is_address_literal("deadbeef") is True
    assert address_module.is_address_literal("4010ab") is True


def test_is_address_literal_rejects_empty_or_whitespace(address_module):
    assert address_module.is_address_literal("") is False
    assert address_module.is_address_literal("   ") is False


def test_is_address_literal_rejects_non_hex_characters(address_module):
    assert address_module.is_address_literal("main") is False
    assert address_module.is_address_literal("sub_4010") is False


def test_is_address_literal_accepts_any_0x_prefix(address_module):
    # Anything starting with 0x is "intended as an address" — parse_address
    # is responsible for rejecting bad hex digits with a clear message.
    assert address_module.is_address_literal("0xZZZZ") is True


# ---------- parse_address ----------


def test_parse_address_int_returns_value(address_module):
    assert address_module.parse_address(0) == 0
    assert address_module.parse_address(0x401000) == 0x401000


def test_parse_address_negative_int_rejected(address_module):
    with pytest.raises(address_module.AddressParseError, match="non-negative"):
        address_module.parse_address(-1)


def test_parse_address_rejects_bool(address_module):
    with pytest.raises(address_module.AddressParseError, match="booleans"):
        address_module.parse_address(True)
    with pytest.raises(address_module.AddressParseError, match="booleans"):
        address_module.parse_address(False)


def test_parse_address_none_rejected(address_module):
    with pytest.raises(address_module.AddressParseError, match="Missing address"):
        address_module.parse_address(None)


def test_parse_address_empty_rejected(address_module):
    with pytest.raises(address_module.AddressParseError, match="Missing address"):
        address_module.parse_address("")
    with pytest.raises(address_module.AddressParseError, match="Missing address"):
        address_module.parse_address("   ")


def test_parse_address_hex_string(address_module):
    assert address_module.parse_address("0x401000") == 0x401000
    assert address_module.parse_address("0X401000") == 0x401000
    assert address_module.parse_address("0xdeadbeef") == 0xDEADBEEF


def test_parse_address_hex_with_underscores(address_module):
    assert address_module.parse_address("0x40_10_00") == 0x401000


def test_parse_address_hex_with_whitespace(address_module):
    assert address_module.parse_address("  0x401000  ") == 0x401000


def test_parse_address_decimal_string(address_module):
    assert address_module.parse_address("1234") == 1234
    assert address_module.parse_address("4_096") == 4096


def test_parse_address_ambiguous_bare_hex_rejected(address_module):
    # Bare hex with hex-letters must be rejected (suggest 0x prefix)
    with pytest.raises(address_module.AddressParseError, match="Ambiguous"):
        address_module.parse_address("deadbeef")
    with pytest.raises(address_module.AddressParseError, match="Ambiguous"):
        address_module.parse_address("4010ab")


def test_parse_address_invalid_hex_after_prefix(address_module):
    with pytest.raises(address_module.AddressParseError, match="use 0x followed by hex"):
        address_module.parse_address("0x")
    with pytest.raises(address_module.AddressParseError, match="use 0x followed by hex"):
        address_module.parse_address("0xZZZ")


def test_parse_address_invalid_format(address_module):
    with pytest.raises(address_module.AddressParseError, match="Invalid address"):
        address_module.parse_address("sub_401000")
    with pytest.raises(address_module.AddressParseError, match="Invalid address"):
        address_module.parse_address("not-a-number")


def test_parse_address_field_name_in_error(address_module):
    with pytest.raises(address_module.AddressParseError, match="Invalid callsite"):
        address_module.parse_address("nope", field="callsite")


def test_parse_address_accepts_object_with_str(address_module):
    """Non-str/int inputs are stringified before parsing."""

    class Wrapped:
        def __str__(self):
            return "0x1000"

    assert address_module.parse_address(Wrapped()) == 0x1000


# ---------- parse_optional_address ----------


def test_parse_optional_address_none_returns_none(address_module):
    assert address_module.parse_optional_address(None) is None


def test_parse_optional_address_empty_string_returns_none(address_module):
    assert address_module.parse_optional_address("") is None


def test_parse_optional_address_zero_is_parsed(address_module):
    # 0 is a valid address — must not be confused with "missing"
    assert address_module.parse_optional_address(0) == 0


def test_parse_optional_address_whitespace_only_raises(address_module):
    # Whitespace-only is not "no value" — falls through to parse_address
    with pytest.raises(address_module.AddressParseError):
        address_module.parse_optional_address("   ")


def test_parse_optional_address_propagates_valid_value(address_module):
    assert address_module.parse_optional_address("0x401000") == 0x401000
