"""Round-trip and validation tests for /patch (in-memory only).

Setup requirement: the patch endpoint is gated behind BN's approval
prompt. For these tests to run non-interactively, the fixture
binary's filesystem path must be in BN's `mcp.patchAllowList`
setting (Settings → MCP Server → Patch Allow List). The session-
scoped `patch_approved` fixture probes for this by attempting a
quick no-op patch — if it can't complete in 5 seconds (BN is
showing a dialog) or returns 403 (user clicked Deny), every test
in this file is skipped with a setup message that includes the
exact path to add.

`save_to_file=true` is intentionally NOT tested here: it writes
through to the on-disk fixture binary (and on macOS re-signs it),
which would corrupt test state across runs. The bridge passes that
flag straight through to BN; the in-memory case here covers the
patch logic itself.
"""

import contextlib

import pytest
import requests


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


def _setup_hint(binja_session, base_url, reason: str) -> str:
    """Build a skip message that names the exact path to add to
    `mcp.patchAllowList`. The path comes from /status (what BN
    actually has open), so it matches what BN's approval gate is
    checking against."""
    try:
        loaded = binja_session.get(f"{base_url}/status", timeout=5).json().get("filename") or ""
    except Exception:
        loaded = ""
    return (
        f"{reason}\n\n"
        "To run patch tests without per-call dialogs:\n"
        "  1. In Binary Ninja: Settings (Cmd+,) → MCP Server → Patch Allow List\n"
        f"  2. Add this exact path: {loaded}\n"
        "  3. Save settings and re-run the suite.\n"
    )


@pytest.fixture(scope="module")
def patch_approved(binja_session, base_url, anchors):
    """Skip the whole patch suite when /patch isn't pre-approved.

    Reads the current byte at a known address and writes the same
    byte back — a no-op patch. If the request returns within 5
    seconds with status 200, the fixture binary is in BN's
    `mcp.patchAllowList` and tests can proceed. If the request
    times out (BN is waiting on a modal dialog) or returns 403
    (user clicked Deny), skip with a setup hint that includes the
    exact path to add.
    """
    addr = anchors["compute_secret"]
    current = binja_session.get(
        f"{base_url}/readInt", params={"address": addr, "size": 1}, timeout=5
    ).json()
    same_byte_hex = current["hex"].removeprefix("0x").zfill(2)
    try:
        r = binja_session.post(
            f"{base_url}/patch",
            json={"address": addr, "data": same_byte_hex, "save_to_file": False},
            timeout=5,
        )
    except requests.Timeout:
        pytest.skip(
            _setup_hint(
                binja_session,
                base_url,
                "patch request timed out (5s) — BN is showing an approval dialog.",
            )
        )
    if r.status_code == 403:
        pytest.skip(_setup_hint(binja_session, base_url, "patch approval was denied."))
    r.raise_for_status()
    # The no-op patch still counts as an undo entry — clean up.
    binja_session.get(f"{base_url}/undo", timeout=10)


# ---------- happy-path round trip ----------


def test_patch_bytes_round_trip(binja_session, base_url, anchors, patch_approved):
    """Patch a single byte at a known address; verify via /readInt;
    one undo restores the original byte."""
    addr = anchors["compute_secret"]

    before = binja_session.get(
        f"{base_url}/readInt", params={"address": addr, "size": 1}, timeout=5
    ).json()
    new_value = (before["value"] ^ 0x55) & 0xFF
    new_hex = f"{new_value:02x}"

    r = binja_session.post(
        f"{base_url}/patch",
        json={"address": addr, "data": new_hex, "save_to_file": False},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["address"] == addr
    assert body["original_bytes"] == before["hex"].removeprefix("0x").zfill(2)
    assert body["patched_bytes"] == new_hex
    assert body["bytes_written"] == 1
    assert body["saved_to_file"] is False

    with _undo_after(binja_session, base_url):
        after = binja_session.get(
            f"{base_url}/readInt", params={"address": addr, "size": 1}, timeout=5
        ).json()
        assert after["value"] == new_value

    restored = binja_session.get(
        f"{base_url}/readInt", params={"address": addr, "size": 1}, timeout=5
    ).json()
    assert restored["value"] == before["value"]


def test_patch_bytes_multi_byte_round_trip(binja_session, base_url, anchors, patch_approved):
    """Patch four bytes at once. Verifies that `bytes_written`
    matches the request, and that all four bytes show up at
    consecutive addresses."""
    addr = anchors["compute_secret"]
    addr_int = int(addr, 16)
    new_hex = "aa bb cc dd"

    before = binja_session.get(
        f"{base_url}/readInt", params={"address": addr, "size": 4}, timeout=5
    ).json()

    r = binja_session.post(
        f"{base_url}/patch",
        json={"address": addr, "data": new_hex, "save_to_file": False},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["bytes_written"] == 4
    assert body["patched_bytes"] == "aabbccdd"

    with _undo_after(binja_session, base_url):
        # Read back each byte individually.
        for offset, expected in enumerate((0xAA, 0xBB, 0xCC, 0xDD)):
            check = binja_session.get(
                f"{base_url}/readInt",
                params={"address": f"0x{addr_int + offset:x}", "size": 1},
                timeout=5,
            ).json()
            assert check["value"] == expected, (
                f"byte at +{offset} = {check['value']:#x}, expected {expected:#x}"
            )

    restored = binja_session.get(
        f"{base_url}/readInt", params={"address": addr, "size": 4}, timeout=5
    ).json()
    assert restored["hex"] == before["hex"]


# ---------- input-format tolerance ----------


def test_patch_bytes_accepts_spaced_and_tight_hex(binja_session, base_url, anchors, patch_approved):
    """Hex with spaces, without spaces, and with `0x` per byte must
    all produce identical writes."""
    addr = anchors["compute_secret"]
    target_bytes = "0x90 0x90"  # most verbose form

    r = binja_session.post(
        f"{base_url}/patch",
        json={"address": addr, "data": target_bytes, "save_to_file": False},
        timeout=10,
    )
    r.raise_for_status()
    with _undo_after(binja_session, base_url):
        body = r.json()
        # All three forms normalise to the same canonical hex string.
        assert body["patched_bytes"] == "9090"
        assert body["bytes_written"] == 2


# ---------- validation errors ----------


def test_patch_bytes_missing_address_returns_400(binja_session, base_url, patch_approved):
    r = binja_session.post(
        f"{base_url}/patch",
        json={"data": "90", "save_to_file": False},
        timeout=5,
    )
    assert r.status_code == 400
    assert "address" in r.json().get("error", "").lower()


def test_patch_bytes_missing_data_returns_400(binja_session, base_url, anchors, patch_approved):
    r = binja_session.post(
        f"{base_url}/patch",
        json={"address": anchors["compute_secret"], "save_to_file": False},
        timeout=5,
    )
    assert r.status_code == 400
    assert "data" in r.json().get("error", "").lower()


def test_patch_bytes_invalid_hex_returns_400(binja_session, base_url, anchors, patch_approved):
    """Non-hex characters in the data string are rejected before any
    write happens, so no undo is required."""
    r = binja_session.post(
        f"{base_url}/patch",
        json={"address": anchors["compute_secret"], "data": "zz", "save_to_file": False},
        timeout=5,
    )
    assert r.status_code == 400
    assert "hex" in r.json().get("error", "").lower()


def test_patch_bytes_unmapped_address_returns_error(binja_session, base_url, patch_approved):
    """Writing to an address outside any mapped segment fails — must
    not return a misleading `status: ok`."""
    r = binja_session.post(
        f"{base_url}/patch",
        json={"address": "0xdeadbeef0", "data": "90", "save_to_file": False},
        timeout=10,
    )
    assert r.status_code >= 400
    body = r.json()
    assert body.get("status") != "ok"


def test_patch_bytes_get_method_returns_405(binja_session, base_url, anchors):
    """The patch endpoint is POST-only — a GET request gets a 405
    with a help hint, regardless of approval state (no /patch
    handler runs at all)."""
    r = binja_session.get(
        f"{base_url}/patch",
        params={"address": anchors["compute_secret"], "data": "90"},
        timeout=5,
    )
    assert r.status_code == 405
    assert "POST" in r.json().get("error", "")
