"""Status and binary-management endpoints: GET /status, /binaries,
/entryPoints. These are the foundation endpoints — if any of them
regress, almost every other integration test fails in confusing
ways, so pinning their shape isolates the root cause to one signal.
"""


def test_status_reports_fixture_loaded(binja_session, base_url):
    r = binja_session.get(f"{base_url}/status", timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body.get("loaded") is True
    assert "constructs" in (body.get("filename") or "")


def test_binaries_lists_active_fixture(binja_session, base_url):
    r = binja_session.get(f"{base_url}/binaries", timeout=5)
    r.raise_for_status()
    entries = r.json().get("binaries", [])
    fixture = next((e for e in entries if "constructs" in (e.get("basename") or "")), None)
    assert fixture is not None, f"constructs missing from /binaries: {entries}"
    assert fixture.get("active") is True


def test_binaries_provides_useful_selectors(binja_session, base_url):
    """The 'selectors' list is what /selectBinary accepts — must include
    both the basename and the full path so callers can pick either."""
    r = binja_session.get(f"{base_url}/binaries", timeout=5)
    r.raise_for_status()
    fixture = next(e for e in r.json()["binaries"] if "constructs" in (e.get("basename") or ""))
    selectors = fixture.get("selectors") or []
    assert "constructs" in selectors, f"basename selector missing: {selectors}"
    assert any(s.startswith("/") and s.endswith("constructs") for s in selectors), (
        f"full-path selector missing: {selectors}"
    )
    # The numeric ordinal id should also be a valid selector.
    assert fixture.get("id") in selectors


def test_entry_points_non_empty(binja_session, base_url):
    r = binja_session.get(f"{base_url}/entryPoints", timeout=5)
    r.raise_for_status()
    eps = r.json().get("entry_points", [])
    assert eps, "no entry points reported"


def test_entry_point_address_is_hex_parseable(binja_session, base_url):
    """The address field must be something `int(_, 16)` can parse — that
    contract is what every address-taking tool downstream relies on."""
    r = binja_session.get(f"{base_url}/entryPoints", timeout=5)
    r.raise_for_status()
    ep = r.json()["entry_points"][0]
    addr = ep.get("address") or ""
    assert addr.lower().startswith("0x"), f"address not 0x-prefixed: {addr!r}"
    int(addr, 16)  # raises if malformed


def test_entry_point_name_matches_main_or_start(binja_session, base_url):
    """macOS lifts the entry to `_start`; Linux/Windows usually keep
    `main` or `_main`. Accept any of those — the point is "BN found an
    entry function", not which alias it picked."""
    r = binja_session.get(f"{base_url}/entryPoints", timeout=5)
    r.raise_for_status()
    name = (r.json()["entry_points"][0].get("name") or "").lstrip("_")
    assert name in {"main", "start"}, f"unexpected entry point name: {name!r}"
