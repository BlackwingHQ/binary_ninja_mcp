"""Round-trip and validation tests for /load.

Setup requirement: the load endpoint is gated behind BN's approval
prompt. For the happy-path tests to run non-interactively, the
fixture binary's absolute path must be in BN's `mcp.loadAllowList`
setting (Settings → MCP Server → Load Allow List). The module-
scoped `load_approved` fixture probes for this by attempting to
load the fixture — if it can't complete in 10 seconds (BN is
showing a dialog) or returns 403 (user denied), the tests gated
on it are skipped with a setup message that includes the exact
path to add.

Loading the same file twice: BN's `open_view` may return the
existing view or open a duplicate. To avoid leaking a stale active
view into the rest of the suite, the fixture captures the active
view id up front and re-selects it after each test in this module.
"""

import pytest
import requests

from .conftest import FIXTURE_PATH


def _setup_hint(reason: str) -> str:
    return (
        f"{reason}\n\n"
        "To run load tests without per-call dialogs:\n"
        "  1. In Binary Ninja: Settings (Cmd+,) → MCP Server → Load Allow List\n"
        f"  2. Add this exact path: {FIXTURE_PATH}\n"
        "  3. Save settings and re-run the suite.\n"
    )


@pytest.fixture(scope="module")
def load_approved(binja_session, base_url):
    """Skip load tests when /load isn't pre-approved.

    Attempts to load the fixture path. If the request returns 200
    within 10 seconds, the path is in `mcp.loadAllowList`. Timeout
    or 403 means the developer needs to configure the allow-list.

    On teardown, re-select the fixture by basename so subsequent
    test files see a loaded binary. Selecting by view_id is unsafe
    here: `bn.load` creates a fresh headless view that may be
    weakref-pruned, causing the server to mint new view_ids and
    invalidate any id captured before the load.
    """
    try:
        r = binja_session.post(
            f"{base_url}/load",
            json={"filepath": str(FIXTURE_PATH)},
            timeout=10,
        )
    except requests.Timeout:
        pytest.skip(_setup_hint("load request timed out — BN is showing an approval dialog."))
    if r.status_code == 403:
        pytest.skip(_setup_hint("load approval was denied."))
    r.raise_for_status()

    yield

    # Restore by basename — survives view_id churn from headless loads.
    binja_session.get(f"{base_url}/selectBinary", params={"view": FIXTURE_PATH.name}, timeout=5)


# ---------- happy-path ----------


def test_load_happy_path(binja_session, base_url, load_approved):
    """Load the fixture binary; verify success message and that
    /status reports the fixture as loaded afterwards."""
    r = binja_session.post(
        f"{base_url}/load",
        json={"filepath": str(FIXTURE_PATH)},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body.get("success") is True
    assert str(FIXTURE_PATH) in body.get("message", "")

    status = binja_session.get(f"{base_url}/status", timeout=5).json()
    assert FIXTURE_PATH.name in (status.get("filename") or "")


# ---------- validation errors (run unconditionally — no approval needed) ----------


def test_load_missing_filepath_returns_400(binja_session, base_url):
    """Missing `filepath` is rejected before the approval gate, so
    this test runs without any allow-list setup."""
    r = binja_session.post(f"{base_url}/load", json={}, timeout=5)
    assert r.status_code == 400
    assert "filepath" in r.json().get("error", "").lower()


def test_load_get_method_returns_405(binja_session, base_url):
    """/load is POST-only — GET returns 405 before any approval or
    load logic runs."""
    r = binja_session.get(f"{base_url}/load", params={"filepath": str(FIXTURE_PATH)}, timeout=5)
    assert r.status_code == 405
    assert "POST" in r.json().get("error", "")
