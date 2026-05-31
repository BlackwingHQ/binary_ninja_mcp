"""End-to-end smoke test against a live Binary Ninja.

The `binja_session` fixture (see conftest.py) covers reachability,
auth, and confirming the right fixture binary is open — so the body
of each test is just the read-only tool call being exercised.
"""


def _decompile(session, base_url, name: str):
    """Try decompiling `name`; return parsed body on success, None on 404."""
    r = session.get(f"{base_url}/decompile", params={"name": name}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def test_decompile_main_references_callee(binja_session, base_url):
    """`main` in the fixture calls `compute_secret`; that name must
    appear in the decompiled output. This exercises the full chain:
    HTTP routing, auth, BinaryView lookup, function resolution, and
    HLIL serialization.

    Tries both `main` and `_main` so the same test works on Linux/Windows
    (where the symbol stays `main`) and macOS (Mach-O underscore prefix)."""
    body = _decompile(binja_session, base_url, "main") or _decompile(
        binja_session, base_url, "_main"
    )
    assert body is not None, "neither `main` nor `_main` resolved in the fixture binary"

    decompiled = body.get("decompiled", "")
    assert decompiled, f"empty decompile body: {body!r}"
    assert "compute_secret" in decompiled, (
        "expected main() to call compute_secret; got:\n" + decompiled
    )
