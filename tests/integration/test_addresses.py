"""Address-level read endpoints: /hexdump, /readInt, /readPointer,
/getXrefsTo, /getTagsAt.

These all parse an address parameter and return data at or near it.
Each one is probed with both hex and decimal forms because the
shared address parser sits in front of them — a regression there
would silently break every endpoint listed in this file.

Anchor addresses come from the `anchors` session fixture so they
stay in sync with the fixture binary across rebuilds.
"""

UNMAPPED_ADDR = "0xdeadbeef0"


# ---------- /hexdump ----------


def test_hexdump_returns_function_header_and_bytes(binja_session, base_url, anchors):
    r = binja_session.get(
        f"{base_url}/hexdump",
        params={"address": anchors["compute_secret"], "length": 16},
        timeout=10,
    )
    r.raise_for_status()
    text = r.text
    # The dump starts with an "address: symbol:" header so callers
    # know what they're looking at.
    assert "_compute_secret" in text
    # Then a hex line containing 16 bytes; the function prologue on
    # arm64 starts with `sub sp, sp, #N` which encodes to ff XX 00 d1.
    assert "ff 43 00 d1" in text


def test_hexdump_accepts_decimal_address(binja_session, base_url, anchors):
    """Decimal and hex forms of the same address must hit the same
    bytes — the address parser is shared with every other endpoint."""
    addr_hex = anchors["compute_secret"]
    addr_dec = str(int(addr_hex, 16))
    by_hex = binja_session.get(
        f"{base_url}/hexdump",
        params={"address": addr_hex, "length": 8},
        timeout=10,
    ).text
    by_dec = binja_session.get(
        f"{base_url}/hexdump",
        params={"address": addr_dec, "length": 8},
        timeout=10,
    ).text
    assert by_hex == by_dec


def test_hexdump_length_negative_one_reads_defined_size(binja_session, base_url, anchors):
    """length=-1 means "give me the whole defined object" — for a
    function start, that's the entire function body."""
    r = binja_session.get(
        f"{base_url}/hexdump",
        params={"address": anchors["compute_secret"], "length": -1},
        timeout=10,
    )
    r.raise_for_status()
    text = r.text
    # The function is multi-block (60+ bytes), so the dump should
    # span several lines, not just one.
    body_lines = [ln for ln in text.splitlines() if ln and not ln.endswith(":")]
    assert len(body_lines) > 1, f"length=-1 returned only {len(body_lines)} line(s): {text}"


# ---------- /readInt ----------


def test_read_int_returns_value_and_hex(binja_session, base_url, anchors):
    addr = anchors["compute_secret"]
    r = binja_session.get(
        f"{base_url}/readInt",
        params={"address": addr, "size": 4, "signed": "false"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["address"] == addr
    assert body["size"] == 4
    assert body["signed"] is False
    # First instruction of _compute_secret on arm64: `sub sp, sp, #0x10`
    # encoded little-endian as ff 43 00 d1 → 0xd10043ff.
    assert body["hex"] == "0xd10043ff"
    assert body["value"] == 0xD10043FF


def test_read_int_signed_interpretation_differs(binja_session, base_url, anchors):
    """0xd10043ff has the high bit set, so signed vs unsigned must
    disagree at sizes 4 and 8."""
    addr = anchors["compute_secret"]
    u = binja_session.get(
        f"{base_url}/readInt",
        params={"address": addr, "size": 4, "signed": "false"},
        timeout=5,
    ).json()["value"]
    s = binja_session.get(
        f"{base_url}/readInt",
        params={"address": addr, "size": 4, "signed": "true"},
        timeout=5,
    ).json()["value"]
    assert u != s
    assert u == s + (1 << 32)


def test_read_int_accepts_decimal_address(binja_session, base_url, anchors):
    addr_hex = anchors["compute_secret"]
    addr_dec = str(int(addr_hex, 16))
    by_hex = binja_session.get(
        f"{base_url}/readInt",
        params={"address": addr_hex, "size": 4},
        timeout=5,
    ).json()
    by_dec = binja_session.get(
        f"{base_url}/readInt",
        params={"address": addr_dec, "size": 4},
        timeout=5,
    ).json()
    # The echoed `address` differs (the server normalises to hex,
    # but the input field passes through differently); the actual
    # bytes read must match.
    assert by_hex["value"] == by_dec["value"]
    assert by_hex["hex"] == by_dec["hex"]


def test_read_int_unmapped_address_errors(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/readInt",
        params={"address": UNMAPPED_ADDR, "size": 4},
        timeout=5,
    )
    assert r.status_code == 400
    assert UNMAPPED_ADDR in r.json().get("error", "")


# ---------- /readPointer ----------


def test_read_pointer_returns_pointer_sized_value(binja_session, base_url, anchors):
    addr = anchors["compute_secret"]
    r = binja_session.get(
        f"{base_url}/readPointer",
        params={"address": addr},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    # The response carries the raw value, its hex form, and an
    # optional `points_to` resolution (None when the bytes don't
    # point at a known symbol).
    assert body["address"] == addr
    assert isinstance(body["value"], int)
    assert body["hex"].startswith("0x")
    assert "points_to" in body


def test_read_pointer_unmapped_address_errors(binja_session, base_url):
    r = binja_session.get(f"{base_url}/readPointer", params={"address": UNMAPPED_ADDR}, timeout=5)
    assert r.status_code == 400
    assert UNMAPPED_ADDR in r.json().get("error", "")


# ---------- /getXrefsTo ----------


def test_xrefs_to_function_finds_call_site(binja_session, base_url, anchors):
    """`_compute_secret` is called from the entry function. The
    callsite address is what's unambiguous — the entry function has
    multiple symbol aliases (`_main` / `_start`) and BN may report
    either, so we don't constrain the caller name here."""
    r = binja_session.get(
        f"{base_url}/getXrefsTo", params={"address": anchors["compute_secret"]}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    refs = body["code_references"]
    call_addrs = {ref["address"] for ref in refs}
    assert anchors["compute_secret_call_site"] in call_addrs, (
        f"expected call site {anchors['compute_secret_call_site']} in {refs}"
    )
    assert body["data_references"] == []


def test_xrefs_to_accepts_decimal_address(binja_session, base_url, anchors):
    addr_hex = anchors["compute_secret"]
    addr_dec = str(int(addr_hex, 16))
    by_hex = binja_session.get(
        f"{base_url}/getXrefsTo", params={"address": addr_hex}, timeout=5
    ).json()
    by_dec = binja_session.get(
        f"{base_url}/getXrefsTo", params={"address": addr_dec}, timeout=5
    ).json()
    assert by_hex == by_dec


def test_xrefs_to_address_with_no_references_empty(binja_session, base_url, anchors):
    """An instruction-body address that no other code branches to or
    references must yield both empty arrays. Use the second insn of
    `_compute_secret` — well inside the function body, not a branch
    target."""
    cs_int = int(anchors["compute_secret"], 16)
    r = binja_session.get(
        f"{base_url}/getXrefsTo", params={"address": f"0x{cs_int + 8:x}"}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    assert body["code_references"] == []
    assert body["data_references"] == []


# ---------- /getTagsAt ----------


def test_tags_at_returns_empty_buckets_when_no_tags(binja_session, base_url, anchors):
    """The fixture is freshly loaded with no user tags, so each
    bucket (data/address/function) is empty and total is zero. The
    full shape is pinned so a future drift (None vs [], renamed
    keys) shows up as one clear failure."""
    addr = anchors["compute_secret"]
    r = binja_session.get(f"{base_url}/getTagsAt", params={"address": addr}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body == {
        "address": addr,
        "data_tags": [],
        "address_tags": [],
        "function_tags": [],
        "total": 0,
    }
