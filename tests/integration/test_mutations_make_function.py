"""Round-trip mutation tests for /makeFunctionAt.

The endpoint creates a user function at an address. Important
distinctions it must enforce, since BN itself doesn't:

  - Address in non-executable segment (data): refuse, with a clear
    message naming the offending segment bounds.
  - Address not in any mapped segment: refuse — no function can ever
    exist there.
  - Address that already starts a function: no-op with
    `status: exists`.
  - Address inside an existing function body: actually creates a
    new function (splits the analysis); undoable.
"""

import contextlib


@contextlib.contextmanager
def _undo_after(session, base_url, count: int = 1):
    """Issue `count` /undo calls in the finally branch so a failed
    assertion doesn't leak state into the next test."""
    try:
        yield
    finally:
        for _ in range(count):
            try:
                session.get(f"{base_url}/undo", timeout=15)
            except Exception:
                pass


def _function_addrs(session, base_url) -> set[str]:
    body = session.get(f"{base_url}/methods", params={"limit": 200}, timeout=5).json()
    return {f["address"] for f in body["functions"]}


# An address inside `_compute_secret`'s body — past the prologue but
# before the next function. The fixture's binary packs functions
# back-to-back in __text, so any mid-function offset works.
MID_FUNCTION_ADDR = "0x100000550"


# ---------- /makeFunctionAt — round trip ----------


def test_make_function_at_mid_function_round_trip(binja_session, base_url):
    """Creating a function at an address inside an existing one
    splits the analysis: a new `sub_<addr>` entry appears alongside
    the original. One undo removes it."""
    addrs_before = _function_addrs(binja_session, base_url)
    assert MID_FUNCTION_ADDR not in addrs_before

    r = binja_session.get(
        f"{base_url}/makeFunctionAt",
        params={"address": MID_FUNCTION_ADDR},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["address"] == MID_FUNCTION_ADDR
    assert body["name"] == "sub_100000550"

    with _undo_after(binja_session, base_url):
        assert MID_FUNCTION_ADDR in _function_addrs(binja_session, base_url)

    assert MID_FUNCTION_ADDR not in _function_addrs(binja_session, base_url)


# ---------- /makeFunctionAt — idempotent at existing function start ----------


def test_make_function_at_existing_function_returns_exists(binja_session, base_url, anchors):
    """Calling /makeFunctionAt at an address where a function already
    starts is a no-op — response carries `status: exists` and the
    existing function's name. No mutation happens, so no undo needed
    (and none would harm anything if applied)."""
    addrs_before = _function_addrs(binja_session, base_url)

    r = binja_session.get(
        f"{base_url}/makeFunctionAt",
        params={"address": anchors["compute_secret"]},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "exists"
    assert body["address"] == anchors["compute_secret"]
    assert body["name"] == "_compute_secret"

    assert _function_addrs(binja_session, base_url) == addrs_before


# ---------- /makeFunctionAt — rejection paths ----------


def test_make_function_at_data_section_rejected(binja_session, base_url, anchors):
    """`default_task` lives in __data, which is non-executable.
    Without this guard, BN would happily overlay a function on top
    of the existing data symbol and the agent would have no signal
    that anything went wrong."""
    addr = anchors["default_task"]
    addrs_before = _function_addrs(binja_session, base_url)

    r = binja_session.get(
        f"{base_url}/makeFunctionAt",
        params={"address": addr},
        timeout=15,
    )
    assert r.status_code == 400
    body = r.json()
    assert "non-executable segment" in body.get("error", "")
    assert addr.removeprefix("0x") in body["error"]

    # No new function should have been created.
    assert _function_addrs(binja_session, base_url) == addrs_before


def test_make_function_at_unmapped_address_rejected(binja_session, base_url):
    """An address that no segment contains can never hold a
    function. The endpoint must refuse the request rather than
    pretend it succeeded (BN's `create_user_function` will silently
    "succeed" without actually creating anything)."""
    r = binja_session.get(
        f"{base_url}/makeFunctionAt",
        params={"address": "0xdeadbeef0"},
        timeout=15,
    )
    assert r.status_code == 400
    body = r.json()
    assert "not in any mapped segment" in body.get("error", "")


def test_make_function_at_missing_address_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/makeFunctionAt", timeout=5)
    assert r.status_code == 400
    assert "Missing address" in r.json().get("error", "")


def test_make_function_at_unknown_platform_returns_400(binja_session, base_url):
    """When an explicit platform is requested, an unknown name is a
    400 (not a silent fallback to the view default)."""
    r = binja_session.get(
        f"{base_url}/makeFunctionAt",
        params={"address": MID_FUNCTION_ADDR, "platform": "zzz_not_a_platform"},
        timeout=10,
    )
    assert r.status_code == 400
    body = r.json()
    assert "zzz_not_a_platform" in body.get("error", "")
    # The error includes the full platform catalog so the caller can
    # pick a real one without consulting docs.
    assert "available_platforms" in body
    assert any("linux" in p for p in body["available_platforms"])
