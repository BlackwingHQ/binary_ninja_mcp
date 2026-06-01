"""Round-trip mutation tests for user data-variable endpoints.

  /defineUserDataVar   — type a global at an address. Accepts any
                         C type string BN's parser understands —
                         primitives, struct/union/enum names already
                         registered in the view, pointer/array
                         compositions.
  /undefineUserDataVar — remove the user data-var at an address.

Both are undoable. The fixture's __data section has plenty of room
past `_default_task`, so a single address well past it serves as a
clean canvas.
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


# Inside __data, well past `_default_task` so the fixture binary
# has nothing defined here at baseline.
SCRATCH_ADDR = "0x100008100"


def _data_var_at(session, base_url, address: str):
    """Returns the parsed /getDataVarAt body, or None if BN reports
    no data variable at that address (404)."""
    r = session.get(f"{base_url}/getDataVarAt", params={"address": address}, timeout=5)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# ---------- /defineUserDataVar ----------


def test_define_user_data_var_primitive_round_trip(binja_session, base_url):
    """Type a fresh address as `uint32_t`; /getDataVarAt reflects the
    type and a default-zero value. One undo restores the unmapped
    baseline."""
    assert _data_var_at(binja_session, base_url, SCRATCH_ADDR) is None

    r = binja_session.get(
        f"{base_url}/defineUserDataVar",
        params={"address": SCRATCH_ADDR, "type": "uint32_t"},
        timeout=10,
    )
    r.raise_for_status()
    assert r.json() == {
        "status": "ok",
        "address": SCRATCH_ADDR,
        "type": "uint32_t",
    }

    with _undo_after(binja_session, base_url):
        info = _data_var_at(binja_session, base_url, SCRATCH_ADDR)
        assert info is not None
        assert info["type"] == "uint32_t"
        assert info["address"] == SCRATCH_ADDR

    assert _data_var_at(binja_session, base_url, SCRATCH_ADDR) is None


def test_define_user_data_var_struct_round_trip(binja_session, base_url):
    """Typing an address as `struct task_t` reuses the DWARF type
    from the fixture; the response renders the struct's fields with
    their default values."""
    r = binja_session.get(
        f"{base_url}/defineUserDataVar",
        params={"address": SCRATCH_ADDR, "type": "struct task_t"},
        timeout=10,
    )
    r.raise_for_status()
    assert r.json()["type"] == "struct task_t"

    with _undo_after(binja_session, base_url):
        info = _data_var_at(binja_session, base_url, SCRATCH_ADDR)
        assert info["type"] == "struct task_t"
        # `value` is a stringified Python dict; check field names so
        # we know the struct's layout actually applied.
        rendered = info["value"]
        assert "'id'" in rendered
        assert "'pri'" in rendered
        assert "'label'" in rendered


def test_define_user_data_var_invalid_type_returns_400(binja_session, base_url):
    """A type string BN's parser can't understand is rejected with a
    400 carrying the parser's error message."""
    r = binja_session.get(
        f"{base_url}/defineUserDataVar",
        params={"address": SCRATCH_ADDR, "type": "zzz_not_a_type_xyz"},
        timeout=10,
    )
    assert r.status_code == 400
    body = r.json()
    assert "Failed to parse type" in body.get("error", "")
    assert "zzz_not_a_type_xyz" in body.get("error", "")


def test_define_user_data_var_missing_params_returns_400(binja_session, base_url):
    """Missing the type argument."""
    r = binja_session.get(
        f"{base_url}/defineUserDataVar",
        params={"address": SCRATCH_ADDR},
        timeout=5,
    )
    assert r.status_code == 400


def test_define_user_data_var_unmapped_address_returns_error(binja_session, base_url):
    """BN's `define_user_data_var` happily registers a phantom data
    variable at an unmapped address — the agent thinks the operation
    succeeded but the binary doesn't actually have any storage there.
    The endpoint must reject unmapped addresses with a 4xx rather
    than returning `status:"ok"`."""
    r = binja_session.get(
        f"{base_url}/defineUserDataVar",
        params={"address": "0xdeadbeef0", "type": "uint32_t"},
        timeout=10,
    )
    assert r.status_code >= 400, (
        f"unmapped address should be rejected, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body.get("status") != "ok"


# ---------- /undefineUserDataVar ----------


def test_undefine_user_data_var_round_trip(binja_session, base_url):
    """Seed a typed var, undefine it, undo restores it. Pins that
    /undefineUserDataVar is itself undoable — the agent can explore
    typing decisions without permanent commitment."""
    binja_session.get(
        f"{base_url}/defineUserDataVar",
        params={"address": SCRATCH_ADDR, "type": "uint32_t"},
        timeout=10,
    ).raise_for_status()
    try:
        assert _data_var_at(binja_session, base_url, SCRATCH_ADDR) is not None

        r = binja_session.get(
            f"{base_url}/undefineUserDataVar",
            params={"address": SCRATCH_ADDR},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        assert body["status"] == "ok"
        assert body["address"] == SCRATCH_ADDR
        assert body["removed_type"] == "uint32_t"
        assert _data_var_at(binja_session, base_url, SCRATCH_ADDR) is None

        # Undo the undefine — the typed var comes back.
        binja_session.get(f"{base_url}/undo", timeout=5)
        info = _data_var_at(binja_session, base_url, SCRATCH_ADDR)
        assert info is not None
        assert info["type"] == "uint32_t"
    finally:
        # Two undos: one for the post-undo state, one to peel the
        # original define so the scratch address is empty again.
        for _ in range(2):
            binja_session.get(f"{base_url}/undo", timeout=5)


def test_undefine_user_data_var_no_var_returns_404(binja_session, base_url):
    """Undefining at an address with no data var returns 404 with a
    descriptive error — not a silent success."""
    r = binja_session.get(
        f"{base_url}/undefineUserDataVar",
        params={"address": "0x100008500"},
        timeout=5,
    )
    assert r.status_code == 404
    assert "No data variable" in r.json().get("error", "")


def test_undefine_user_data_var_missing_address_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/undefineUserDataVar", timeout=5)
    assert r.status_code == 400
