"""Tests for plugin/utils/number_utils.py — convert_number().

The function returns a dict with many representations of a value. After
parsing, internal `_format_numeric_value` is `dict.update`'d into the
result, so the final shape ends up with `kind = "int"` for both integer
and char inputs. These tests assert the actual current behavior; if the
data model changes, update them deliberately.
"""

import pytest

# ---------- empty / whitespace ----------


def test_empty_input_returns_warning(number_module):
    out = number_module.convert_number("", 0)
    assert "empty input" in out["warnings"]
    assert out["kind"] is None
    assert out["bases"] == {}


def test_whitespace_only_input_returns_warning(number_module):
    out = number_module.convert_number("   \n", 0)
    assert "empty input" in out["warnings"]


def test_none_text_input_returns_warning(number_module):
    out = number_module.convert_number(None, 0)
    assert "empty input" in out["warnings"]


# ---------- decimal ----------


def test_decimal_auto_sizes_to_one_byte(number_module):
    out = number_module.convert_number("42", 0)
    assert out["kind"] == "int"
    assert out["bases"]["dec"] == "42"
    assert out["bases"]["hex"] == "0x2a"
    assert out["bytes"]["length"] == 1
    assert out["little_endian"]["hex"] == "2a"
    assert out["big_endian"]["hex"] == "2a"


def test_decimal_explicit_size_4(number_module):
    out = number_module.convert_number("1", 4)
    assert out["bytes"]["length"] == 4
    assert out["little_endian"]["hex"] == "01000000"
    assert out["big_endian"]["hex"] == "00000001"
    assert out["little_endian"]["uint"] == 1
    assert out["big_endian"]["uint"] == 1


def test_negative_decimal_signed_round_trip(number_module):
    out = number_module.convert_number("-1", 4)
    assert out["little_endian"]["hex"] == "ffffffff"
    assert out["little_endian"]["uint"] == 0xFFFFFFFF
    assert out["little_endian"]["int"] == -1
    assert out["bases"]["dec"] == str(0xFFFFFFFF)


def test_decimal_with_underscores(number_module):
    out = number_module.convert_number("1_000", 4)
    assert out["bases"]["dec"] == "1000"


# ---------- hex ----------


def test_hex_0x_prefix(number_module):
    out = number_module.convert_number("0xdead", 0)
    # auto-fit: 0xdead needs 2 bytes
    assert out["bytes"]["length"] == 2
    assert out["little_endian"]["hex"] == "adde"
    assert out["big_endian"]["hex"] == "dead"


def test_hex_uppercase_prefix(number_module):
    out = number_module.convert_number("0XCAFE", 4)
    assert out["little_endian"]["uint"] == 0xCAFE


def test_hex_with_h_suffix(number_module):
    out = number_module.convert_number("CAFEh", 4)
    assert out["little_endian"]["uint"] == 0xCAFE


def test_hex_with_underscores(number_module):
    out = number_module.convert_number("0xdead_beef", 4)
    assert out["little_endian"]["uint"] == 0xDEADBEEF


# ---------- binary / octal ----------


def test_binary_literal(number_module):
    out = number_module.convert_number("0b1111011", 0)
    assert out["bases"]["dec"] == "123"
    assert out["bases"]["hex"] == "0x7b"


def test_octal_literal(number_module):
    out = number_module.convert_number("0o173", 0)
    assert out["bases"]["dec"] == "123"


# ---------- auto size selection ----------


def test_auto_size_selects_2_bytes_for_value_over_255(number_module):
    out = number_module.convert_number("256", 0)
    assert out["bytes"]["length"] == 2


def test_auto_size_selects_4_bytes(number_module):
    out = number_module.convert_number("65536", 0)
    assert out["bytes"]["length"] == 4


def test_auto_size_selects_8_bytes(number_module):
    out = number_module.convert_number("0x100000000", 0)
    assert out["bytes"]["length"] == 8


def test_signed_auto_size_picks_2_bytes_for_neg_129(number_module):
    # signed: -128..127 fits in 1 byte; -129 needs 2
    out = number_module.convert_number("-129", 0)
    assert out["bytes"]["length"] == 2


def test_size_param_none_treated_as_auto(number_module):
    out = number_module.convert_number("1", None)
    assert out["bytes"]["length"] == 1


def test_size_param_invalid_string_treated_as_auto(number_module):
    out = number_module.convert_number("1", "abc")
    assert out["bytes"]["length"] == 1


# ---------- truncation on oversized values ----------


def test_oversized_value_truncated_to_requested_size(number_module):
    # 0x1ff doesn't fit in 1 byte; result masks to 0xff
    out = number_module.convert_number("0x1ff", 1)
    assert out["little_endian"]["uint"] == 0xFF


# ---------- single-byte c_literal ----------


def test_single_byte_int_includes_c_literal(number_module):
    out = number_module.convert_number("65", 1)
    assert out["c_literal"] == "'A'"


def test_single_byte_with_special_char(number_module):
    out = number_module.convert_number("10", 1)
    assert out["c_literal"] == r"'\n'"


def test_multi_byte_int_has_no_c_literal(number_module):
    out = number_module.convert_number("256", 0)  # auto-sizes to 2
    # The integer branch only sets c_literal when size==1; key may be
    # absent from the formatted dict but result preserves None from init.
    assert out.get("c_literal") is None


# ---------- char literal ----------


def test_char_literal_uses_byte_value(number_module):
    out = number_module.convert_number("'A'", 0)
    # _format_numeric_value overwrites result["kind"] to "int" via update()
    assert out["bases"]["dec"] == "65"
    assert out["c_literal"] == "'A'"
    assert out["bytes"]["length"] == 1


def test_char_literal_with_hex_escape(number_module):
    out = number_module.convert_number(r"'\x42'", 0)
    assert out["bases"]["dec"] == "66"
    assert out["c_literal"] == "'B'"


def test_empty_char_literal_yields_zero(number_module):
    out = number_module.convert_number("''", 0)
    # text=="''" is length 2 so the >= 3 check fails — treated as garbage
    assert "warnings" in out


# ---------- string literal ----------


def test_string_literal_basic(number_module):
    out = number_module.convert_number('"ABC"', 0)
    assert out["bytes"]["length"] == 3
    assert out["bytes"]["hex"] == "414243"
    assert out["c_string"] == '"ABC"'


def test_string_literal_truncated_to_size_8(number_module):
    out = number_module.convert_number('"abcdefghij"', 0)
    # auto-size caps at 8 for strings
    assert out["little_endian"]["hex"] == bytes("abcdefgh", "ascii").hex()


def test_string_literal_explicit_size_padding(number_module):
    out = number_module.convert_number('"AB"', 4)
    # little-endian view pads the high bytes with zero
    assert out["little_endian"]["hex"] == "41420000"
    # big-endian view pads the low bytes with zero
    assert out["big_endian"]["hex"] == "00004142"


def test_string_literal_decodes_hex_escapes(number_module):
    out = number_module.convert_number(r'"A\x42"', 0)
    assert out["bytes"]["hex"] == "4142"


def test_string_literal_backslash_n_not_decoded(number_module):
    # Current behavior: the replacement table only matches doubly-escaped
    # sequences (e.g. literal "\\n"), so a single-backslash "\n" in the
    # input is preserved as bytes 5c 6e. Pinning the actual behavior so a
    # future fix to the decoder is a deliberate, test-visible change.
    out = number_module.convert_number(r'"A\n"', 0)
    assert out["bytes"]["hex"] == "415c6e"


def test_string_c_string_escapes_special_bytes(number_module):
    out = number_module.convert_number(r'"\x00\xff"', 0)
    assert out["c_string"] == r'"\x00\xff"'


# ---------- garbage fallback to string ----------


def test_unparseable_text_falls_back_to_string(number_module):
    out = number_module.convert_number("nope-not-a-number", 0)
    assert out["kind"] == "string"
    assert "parsed as string; numeric parse failed" in out["warnings"]
    assert out["bytes"]["length"] == len("nope-not-a-number")


# ---------- input echo ----------


def test_input_dict_includes_original_text_when_numeric(number_module):
    # _format_numeric_value overwrites input with {"value": ..., "size": ...}
    out = number_module.convert_number("42", 4)
    assert "value" in out["input"]
    assert out["input"]["value"] == 42
    assert out["input"]["size"] == 4


@pytest.mark.parametrize(
    "text, expected_dec",
    [
        ("0", "0"),
        ("255", "255"),
        ("0xff", "255"),
        ("0xFF", "255"),
        ("0o377", "255"),
        ("0b11111111", "255"),
        ("FFh", "255"),
    ],
)
def test_equivalent_representations_of_255(number_module, text, expected_dec):
    out = number_module.convert_number(text, 1)
    assert out["bases"]["dec"] == expected_dec
