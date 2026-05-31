"""Search and expression endpoints: /findBytes, /findText,
/findConstant, /parseExpression.

These all return `{count, matches}` (find_*) or a resolved address
record (parseExpression). The find_* endpoints accept optional
`start` / `end` bounds (hex or decimal) and a `limit` cap.

Addresses come from the `anchors` session fixture, except the
prologue byte pattern itself — `ff 43 00 d1` is the arm64 encoding
of `sub sp, sp, #0x10` and is independent of where the function
ends up loaded.
"""

PROLOGUE_BYTES = "ff 43 00 d1"


# ---------- /findBytes ----------


def test_find_bytes_locates_prologue(binja_session, base_url, anchors):
    r = binja_session.get(
        f"{base_url}/findBytes",
        params={"pattern": PROLOGUE_BYTES, "limit": 50},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["pattern"] == "ff4300d1"  # spaces are stripped
    assert body["count"] >= 1
    addrs = {m["address"] for m in body["matches"]}
    # `_compute_secret` definitely opens with this prologue; other
    # helpers might too (every arm64 `sub sp, sp, #0x10` encodes the
    # same way), so use `in` rather than equality.
    assert anchors["compute_secret"] in addrs
    hit = next(m for m in body["matches"] if m["address"] == anchors["compute_secret"])
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


def test_find_bytes_respects_end_bound(binja_session, base_url, anchors):
    """end= is exclusive: searching strictly before the prologue
    address must miss it, then including the next 16 bytes after it
    must hit."""
    cs_int = int(anchors["compute_secret"], 16)
    miss = binja_session.get(
        f"{base_url}/findBytes",
        params={
            "pattern": PROLOGUE_BYTES,
            "start": "0x100000000",
            "end": anchors["compute_secret"],
        },
        timeout=10,
    ).json()
    # No `_compute_secret` prologue before its own address. Earlier
    # functions might still hit, so only assert against the specific
    # address we're trying to exclude.
    addrs = {m["address"] for m in miss["matches"]}
    assert anchors["compute_secret"] not in addrs

    hit = binja_session.get(
        f"{base_url}/findBytes",
        params={
            "pattern": PROLOGUE_BYTES,
            "start": "0x100000000",
            "end": f"0x{cs_int + 0x10:x}",
        },
        timeout=10,
    ).json()
    assert anchors["compute_secret"] in {m["address"] for m in hit["matches"]}


# ---------- /findText ----------


def test_find_text_locates_fixture_string(binja_session, base_url, anchors):
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
    # The literal lives in __cstring (the address resolved by the
    # `anchors` fixture).
    assert anchors["usage_string"] in addrs


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


def test_find_text_respects_bounds(binja_session, base_url, anchors):
    """Bounding the search to the __cstring region around the literal
    must still find it; bounding strictly past it must not."""
    usage_int = int(anchors["usage_string"], 16)
    inside = binja_session.get(
        f"{base_url}/findText",
        params={
            "text": "usage",
            "start": f"0x{usage_int - 0x10:x}",
            "end": f"0x{usage_int + 0x40:x}",
            "limit": 10,
        },
        timeout=10,
    ).json()
    assert inside["count"] >= 1

    past = binja_session.get(
        f"{base_url}/findText",
        params={
            "text": "usage",
            "start": f"0x{usage_int + 0x40:x}",
            "limit": 10,
        },
        timeout=10,
    ).json()
    assert past["count"] == 0


# ---------- /findConstant ----------


def test_find_constant_finds_loop_multiplier(binja_session, base_url):
    """The `i * 7` expression in `_compute_secret` references the
    literal 7 — the headline use case for this endpoint. Every
    matching site reports the containing function."""
    r = binja_session.get(f"{base_url}/findConstant", params={"value": 7, "limit": 50}, timeout=15)
    r.raise_for_status()
    body = r.json()
    assert body["value"] == "0x7"
    assert body["count"] >= 1
    fns = {m["function"] for m in body["matches"]}
    assert "_compute_secret" in fns


def test_find_constant_unlikely_value_returns_empty(binja_session, base_url):
    """A value chosen to be absent from every instruction yields zero
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
    same canonical `0x...` echo and the same match set."""
    by_dec = binja_session.get(
        f"{base_url}/findConstant", params={"value": "7", "limit": 50}, timeout=15
    ).json()
    by_hex = binja_session.get(
        f"{base_url}/findConstant", params={"value": "0x7", "limit": 50}, timeout=15
    ).json()
    assert by_dec["value"] == "0x7"
    assert by_hex["value"] == "0x7"
    assert by_dec["matches"] == by_hex["matches"]


def test_find_constant_respects_bounds(binja_session, base_url, anchors):
    """An end= bound that excludes `_compute_secret` entirely must
    produce zero matches for the `i * 7` constant; widening past
    its end must hit at least one."""
    cs_int = int(anchors["compute_secret"], 16)
    excluded = binja_session.get(
        f"{base_url}/findConstant",
        params={
            "value": 7,
            "start": "0x100000000",
            "end": anchors["compute_secret"],
            "limit": 50,
        },
        timeout=15,
    ).json()
    cs_hits = [m for m in excluded["matches"] if m["function"] == "_compute_secret"]
    assert cs_hits == []

    included = binja_session.get(
        f"{base_url}/findConstant",
        params={
            "value": 7,
            "start": "0x100000000",
            "end": f"0x{cs_int + 0x100:x}",
            "limit": 50,
        },
        timeout=15,
    ).json()
    assert any(m["function"] == "_compute_secret" for m in included["matches"])


# ---------- /parseExpression ----------


def test_parse_expression_resolves_symbol(binja_session, base_url, anchors):
    r = binja_session.get(
        f"{base_url}/parseExpression", params={"expr": "_compute_secret"}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["expression"] == "_compute_secret"
    assert body["address"] == anchors["compute_secret"]
    assert body["value"] == int(anchors["compute_secret"], 16)


def test_parse_expression_hex_literal_round_trips(binja_session, base_url, anchors):
    r = binja_session.get(
        f"{base_url}/parseExpression",
        params={"expr": anchors["compute_secret"]},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["address"] == anchors["compute_secret"]


def test_parse_expression_supports_arithmetic(binja_session, base_url, anchors):
    """`_compute_secret + 4` resolves to the second instruction of
    the function."""
    r = binja_session.get(
        f"{base_url}/parseExpression",
        params={"expr": "_compute_secret+4"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["value"] == int(anchors["compute_secret"], 16) + 4


def test_parse_expression_substitutes_dollar_here(binja_session, base_url, anchors):
    """BN's expression language uses `$here` for the address context
    passed via the `here=` query parameter."""
    r = binja_session.get(
        f"{base_url}/parseExpression",
        params={"expr": "$here+8", "here": anchors["main"]},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["here"] == anchors["main"]
    assert body["value"] == int(anchors["main"], 16) + 8


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
