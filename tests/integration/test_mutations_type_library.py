"""Error-path tests for /addTypeLibrary.

Happy-path coverage is intentionally skipped:

  - A working `.bntl` is either generated at session setup (~30 lines
    of BN-API ceremony for a niche tool the agent rarely uses) or
    borrowed from the BN install (typelib path varies per OS, and
    "which `.bntl` is not already auto-loaded" varies per platform).
    Neither pays back the brittleness.
  - The bridge tool is a thin pass-through to BN's
    `BinaryView.add_type_library`. Real-world load failures show up
    when actual users feed real `.bntl` files; the test suite can't
    do better than what BN itself promises.

What IS worth pinning: the three input-validation failure modes
that an agent might hit when typing a path wrong.
"""


def test_add_type_library_missing_path_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/addTypeLibrary", timeout=5)
    assert r.status_code == 400
    body = r.json()
    assert "Missing path" in body.get("error", "")


def test_add_type_library_nonexistent_path_returns_400(binja_session, base_url):
    """A path that doesn't exist on disk is rejected before BN gets
    involved, with a clear "file not found" message."""
    r = binja_session.get(
        f"{base_url}/addTypeLibrary",
        params={"path": "/definitely/not/a/real/path/foo.bntl"},
        timeout=5,
    )
    assert r.status_code == 400
    body = r.json()
    assert "not found" in body.get("error", "").lower()
    assert "/definitely/not/a/real/path/foo.bntl" in body["error"]


def test_add_type_library_non_bntl_file_returns_400(binja_session, base_url, anchors):
    """Pointing at a real file that isn't a `.bntl` — using the
    fixture binary itself as a guaranteed real-but-wrong path —
    surfaces as a 400 instead of a 500. Pin this so a future
    change that drops the load-result check (and starts returning
    500 for invalid files) is a visible regression."""
    # The fixture binary is a Mach-O, not a type library.
    from pathlib import Path

    binary_path = Path(__file__).parent / "fixtures" / "constructs"
    assert binary_path.exists(), "fixture binary missing — run build.sh"

    r = binja_session.get(
        f"{base_url}/addTypeLibrary",
        params={"path": str(binary_path)},
        timeout=10,
    )
    assert r.status_code == 400
    assert "Type library" in r.json().get("error", "")
