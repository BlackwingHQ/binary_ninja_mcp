"""Round-trip mutation tests for user-symbol endpoints.

  /defineUserSymbol   — attach a name to an address (kind=data or
                        kind=function).
  /undefineUserSymbol — remove the symbol at an address. Works on any
                        non-auto symbol; BN's truly auto-generated
                        symbols (Mach-O header constructs etc.) are
                        rejected with a clear "auto-generated" error.

Both endpoints are undoable. The fixture's `_default_task` global,
the `_compute_secret` function, and BN's `__macho_header` symbol
each represent a distinct symbol provenance and let us pin the
distinct behaviors.
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
                session.get(f"{base_url}/undo", timeout=10)
            except Exception:
                pass


def _data_symbol_names(session, base_url) -> set[str]:
    body = session.get(
        f"{base_url}/getSymbolsByType",
        params={"type": "data", "limit": 1000},
        timeout=10,
    ).json()
    return {s["name"] for s in body.get("symbols", [])}


# ---------- /defineUserSymbol ----------

# An address inside __data, far enough past _default_task that BN
# hasn't auto-labelled anything there.
UNLABELLED_DATA_ADDR = "0x100008100"


def test_define_user_symbol_round_trip(binja_session, base_url):
    """Attach a label to a previously-unlabelled data address; the
    name shows up under DataSymbol and a single undo removes it."""
    name = "test_define_user_symbol"
    assert name not in _data_symbol_names(binja_session, base_url)

    r = binja_session.get(
        f"{base_url}/defineUserSymbol",
        params={"address": UNLABELLED_DATA_ADDR, "name": name, "kind": "data"},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body == {
        "status": "ok",
        "address": UNLABELLED_DATA_ADDR,
        "name": name,
        "kind": "data",
    }

    with _undo_after(binja_session, base_url):
        assert name in _data_symbol_names(binja_session, base_url)

    assert name not in _data_symbol_names(binja_session, base_url)


def test_define_user_symbol_missing_params_returns_400(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/defineUserSymbol",
        params={"address": UNLABELLED_DATA_ADDR},  # no name
        timeout=5,
    )
    assert r.status_code == 400


# ---------- /undefineUserSymbol ----------


def test_undefine_user_symbol_round_trip(binja_session, base_url):
    """Seed a user label, undefine it, verify it's gone, undo,
    verify it's back. The /undefineUserSymbol itself is undoable —
    a property the agent can rely on when exploring."""
    name = "test_undefine_user_symbol"
    binja_session.get(
        f"{base_url}/defineUserSymbol",
        params={"address": UNLABELLED_DATA_ADDR, "name": name, "kind": "data"},
        timeout=10,
    ).raise_for_status()
    try:
        assert name in _data_symbol_names(binja_session, base_url)

        r = binja_session.get(
            f"{base_url}/undefineUserSymbol",
            params={"address": UNLABELLED_DATA_ADDR},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        assert body["status"] == "ok"
        assert body["address"] == UNLABELLED_DATA_ADDR
        assert body["removed"] == name
        assert name not in _data_symbol_names(binja_session, base_url)

        # Undo the undefine — symbol comes back.
        binja_session.get(f"{base_url}/undo", timeout=5)
        assert name in _data_symbol_names(binja_session, base_url)
    finally:
        # Two undos total: one for the post-undo state, one to peel
        # the original define so we leave the binary clean.
        for _ in range(2):
            binja_session.get(f"{base_url}/undo", timeout=5)


def test_undefine_user_symbol_removes_dwarf_symbol(binja_session, base_url, anchors):
    """DWARF-imported globals like `_default_task` are explicit (not
    BN-auto-synthesised) so the endpoint treats them as removable.
    The undo path restores them — important so the agent's
    exploration is recoverable."""
    name = "default_task"
    assert name in _data_symbol_names(binja_session, base_url)

    r = binja_session.get(
        f"{base_url}/undefineUserSymbol",
        params={"address": anchors["default_task"]},
        timeout=10,
    )
    r.raise_for_status()
    with _undo_after(binja_session, base_url):
        assert name not in _data_symbol_names(binja_session, base_url)

    assert name in _data_symbol_names(binja_session, base_url)


def test_undefine_user_symbol_rejects_truly_auto_symbol(binja_session, base_url):
    """`__macho_header` is BN-auto-generated while parsing the Mach-O
    header — the endpoint must refuse to remove it and tell the
    caller why."""
    r = binja_session.get(
        f"{base_url}/undefineUserSymbol",
        params={"address": "0x100000000"},
        timeout=5,
    )
    assert r.status_code == 404
    body = r.json()
    assert "auto-generated" in body.get("error", "")
    assert "__macho_header" in body.get("error", "")


def test_undefine_user_symbol_no_symbol_at_address_returns_404(binja_session, base_url):
    """Address with no defined symbol at all. 0x100000704 was empty
    in our probe; pick any address known not to have a symbol."""
    r = binja_session.get(
        f"{base_url}/undefineUserSymbol",
        params={"address": "0x100000704"},
        timeout=5,
    )
    assert r.status_code == 404
    assert "No symbol found" in r.json().get("error", "")


def test_undefine_user_symbol_missing_address_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/undefineUserSymbol", timeout=5)
    assert r.status_code == 400
