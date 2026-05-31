"""Comment read endpoints: /comment (address) and /comment/function.

Both endpoints follow the same pattern: success=True with comment=null
when there is no comment, success=True with the comment text when one
exists. They don't distinguish "no comment at this address/function"
from "this address/function doesn't exist at all" — every query
returns 200. That design is pinned here; if it changes to use 404 for
unknown targets, these tests will fail and force a deliberate update.

The fixture binary is loaded fresh with no user comments, so all
queries are exercising the empty-comment path. Set/get round-trips
belong in the mutation suite, not here.
"""

COMPUTE_SECRET_ADDR_HEX = "0x100000460"
COMPUTE_SECRET_ADDR_DEC = "4294968416"
UNMAPPED_ADDR = "0xdeadbeef0"


# ---------- /comment ----------


def test_comment_at_address_with_no_comment(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/comment", params={"address": COMPUTE_SECRET_ADDR_HEX}, timeout=5
    )
    r.raise_for_status()
    assert r.json() == {
        "success": True,
        "address": COMPUTE_SECRET_ADDR_HEX,
        "comment": None,
        "message": "No comment found at this address",
    }


def test_comment_accepts_decimal_address(binja_session, base_url):
    """The address parser is shared with every other endpoint; pin
    decimal-form acceptance here too so a regression surfaces."""
    r = binja_session.get(
        f"{base_url}/comment", params={"address": COMPUTE_SECRET_ADDR_DEC}, timeout=5
    )
    r.raise_for_status()
    # The server normalises the address to hex in its echo, so both
    # forms produce the same response body.
    assert r.json()["address"] == COMPUTE_SECRET_ADDR_HEX


def test_comment_for_unmapped_address_returns_null(binja_session, base_url):
    """Querying a comment at an unmapped address is not an error —
    it just means "no comment exists there"."""
    r = binja_session.get(f"{base_url}/comment", params={"address": UNMAPPED_ADDR}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["success"] is True
    assert body["comment"] is None


# ---------- /comment/function ----------


def test_function_comment_for_function_with_no_comment(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/comment/function", params={"name": "_compute_secret"}, timeout=5
    )
    r.raise_for_status()
    assert r.json() == {
        "success": True,
        "function": "_compute_secret",
        "comment": None,
        "message": "No comment found for this function",
    }


def test_function_comment_for_unknown_function_returns_null(binja_session, base_url):
    """The endpoint doesn't distinguish "function exists but has no
    comment" from "function doesn't exist" — both produce the same
    null-comment response. Pinning this so a future change to return
    404 for unknown functions is a deliberate, test-visible decision."""
    r = binja_session.get(
        f"{base_url}/comment/function",
        params={"name": "definitely_not_a_function"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["success"] is True
    assert body["comment"] is None
