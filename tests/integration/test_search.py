"""Search and expression endpoints: /findBytes, /findText,
/findConstant, /parseExpression.

These all return `{count, matches}` (find_*) or a resolved address
record (parseExpression). The find_* endpoints accept optional
`start` / `end` bounds (hex or decimal) and a `limit` cap.

Anchor data in the fixture:
  - 0x100000460 — first instruction of `_compute_secret` on arm64,
    encoded as the byte sequence `ff 43 00 d1`.
  - 0x100000578 — the `"usage: %s <n>\\n"` format string in __cstring.
  - 0x100000500 — inside `_start`, where that format string is loaded
    (the literal appears in the lea'd address so /findText sees it).
"""

PROLOGUE_BYTES = "ff 43 00 d1"
PROLOGUE_ADDR = "0x100000460"
USAGE_STRING_ADDR = "0x100000578"
COMPUTE_SECRET_ADDR_HEX = "0x100000460"
ENTRY_FN_ADDR_HEX = "0x1000004c4"


# ---------- /findBytes ----------


def test_find_bytes_locates_prologue(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/findBytes",
        params={"pattern": PROLOGUE_BYTES, "limit": 10},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["pattern"] == "ff4300d1"  # spaces are stripped
    assert body["count"] >= 1
    addrs = {m["address"] for m in body["matches"]}
    assert PROLOGUE_ADDR in addrs
    # When the match lies inside a function, the response surfaces the
    # function name so callers don't have to make a second round trip.
    hit = next(m for m in body["matches"] if m["address"] == PROLOGUE_ADDR)
    assert hit["function"] == "_compute_secret"


def test_find_bytes_pattern_strips_whitespace(binja_session, base_url):
    """Pasting bytes with spaces (from BN's own hex view) must work
    interchangeably with a contiguous form."""
    spaced = binja_session.get(
        f"{base_url}/findBytes", params={"pattern": "ff 43 00 d1", "limit": 5}, timeout=10
    ).json()
    tight = binja_session.get(
        f"{base_url}/findBytes", params={"pattern": "ff4300d1", "limit": 5}, timeout=10
    ).json()
    assert spaced == tight


def test_find_bytes_no_match_returns_empty(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/findBytes",
        params={"pattern": "de ad be ef ca fe ba be", "limit": 5},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["count"] == 0
    assert body["matches"] == []


def test_find_bytes_respects_end_bound(binja_session, base_url):
    """end= is exclusive: searching strictly before the prologue
    address must miss it, then including it must hit."""
    miss = binja_session.get(
        f"{base_url}/findBytes",
        params={"pattern": PROLOGUE_BYTES, "start": "0x100000000", "end": PROLOGUE_ADDR},
        timeout=10,
    ).json()
    assert miss["count"] == 0

    hit = binja_session.get(
        f"{base_url}/findBytes",
        params={
            "pattern": PROLOGUE_BYTES,
            "start": "0x100000000",
            "end": "0x100000470",
        },
        timeout=10,
    ).json()
    assert hit["count"] >= 1


# ---------- /findText ----------


def test_find_text_locates_fixture_string(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/findText",
        params={"text": "usage", "limit": 10},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["text"] == "usage"
    assert body["case_sensitive"] is True
    addrs = {m["address"] for m in body["matches"]}
    # The literal lives in __cstring at 0x100000578.
    assert USAGE_STRING_ADDR in addrs


def test_find_text_case_sensitive_skips_wrong_case(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/findText",
        params={"text": "USAGE", "limit": 10, "caseSensitive": "1"},
        timeout=10,
    )
    r.raise_for_status()
    assert r.json()["count"] == 0


def test_find_text_no_match_returns_empty(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/findText",
        params={"text": "this_string_cannot_possibly_appear_xyz", "limit": 5},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["count"] == 0
    assert body["matches"] == []


def test_find_text_respects_bounds(binja_session, base_url):
    """Bounding the search to the __cstring region around the literal
    must still find it; bounding strictly past it must not."""
    inside = binja_session.get(
        f"{base_url}/findText",
        params={
            "text": "usage",
            "start": "0x100000570",
            "end": "0x100000600",
            "limit": 10,
        },
        timeout=10,
    ).json()
    assert inside["count"] >= 1

    past = binja_session.get(
        f"{base_url}/findText",
        params={
            "text": "usage",
            "start": "0x100000600",
            "limit": 10,
        },
        timeout=10,
    ).json()
    assert past["count"] == 0


# ---------- /findConstant ----------


def test_find_constant_zero_in_data_padding(binja_session, base_url):
    """BN's `find_next_constant` reliably finds zero-valued bytes in
    Mach-O / ELF padding regions — a decent canary that the endpoint
    plumbing works end-to-end."""
    r = binja_session.get(f"{base_url}/findConstant", params={"value": 0, "limit": 5}, timeout=15)
    r.raise_for_status()
    body = r.json()
    assert body["value"] == "0x0"
    assert body["count"] >= 1


def test_find_constant_unlikely_value_returns_empty(binja_session, base_url):
    """A value chosen to be absent from the entire view yields zero
    matches without erroring."""
    r = binja_session.get(
        f"{base_url}/findConstant",
        params={"value": "0xdeadc0debadf00d", "limit": 5},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["count"] == 0
    assert body["matches"] == []


def test_find_constant_accepts_hex_and_decimal(binja_session, base_url):
    """The endpoint normalises both forms of the same value to the
    same canonical `0x...` echo."""
    by_dec = binja_session.get(
        f"{base_url}/findConstant", params={"value": "16", "limit": 1}, timeout=15
    ).json()
    by_hex = binja_session.get(
        f"{base_url}/findConstant", params={"value": "0x10", "limit": 1}, timeout=15
    ).json()
    assert by_dec["value"] == "0x10"
    assert by_hex["value"] == "0x10"


# ---------- /parseExpression ----------


def test_parse_expression_resolves_symbol(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/parseExpression", params={"expr": "_compute_secret"}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["expression"] == "_compute_secret"
    assert body["address"] == COMPUTE_SECRET_ADDR_HEX
    assert body["value"] == int(COMPUTE_SECRET_ADDR_HEX, 16)


def test_parse_expression_hex_literal_round_trips(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/parseExpression",
        params={"expr": COMPUTE_SECRET_ADDR_HEX},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["address"] == COMPUTE_SECRET_ADDR_HEX


def test_parse_expression_supports_arithmetic(binja_session, base_url):
    """`_compute_secret + 4` resolves to the second instruction of
    the function."""
    r = binja_session.get(
        f"{base_url}/parseExpression",
        params={"expr": "_compute_secret+4"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["value"] == int(COMPUTE_SECRET_ADDR_HEX, 16) + 4


def test_parse_expression_substitutes_dollar_here(binja_session, base_url):
    """BN's expression language uses `$here` for the address context
    passed via the `here=` query parameter."""
    r = binja_session.get(
        f"{base_url}/parseExpression",
        params={"expr": "$here+8", "here": ENTRY_FN_ADDR_HEX},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["here"] == ENTRY_FN_ADDR_HEX
    assert body["value"] == int(ENTRY_FN_ADDR_HEX, 16) + 8


def test_parse_expression_missing_expr_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/parseExpression", params={"expr": ""}, timeout=5)
    assert r.status_code == 400
    assert "Missing expression" in r.json().get("error", "")


def test_parse_expression_unknown_symbol_returns_400(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/parseExpression",
        params={"expr": "definitely_not_a_symbol_xyz"},
        timeout=5,
    )
    assert r.status_code == 400
    assert "definitely_not_a_symbol_xyz" in r.json().get("error", "")
