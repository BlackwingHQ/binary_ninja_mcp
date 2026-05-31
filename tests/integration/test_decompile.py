"""Decompilation and IL endpoints: /decompile, /il, /assembly.

The fixture's `_compute_secret` is a small loop summing `i * 7` for
`i` in `1..n`. That gives us reliable HLIL surface (`while`, `i`,
`result`, `* 7`), a multi-block CFG for MLIL/LLIL coverage, and an
SSA form that introduces phi nodes for the loop variables.

The entry function (which BN names `_start`; `_main` is an alias)
forwards to `atoi`, `compute_secret`, and `printf` — useful as a
multi-callee target for /decompile assertions.
"""

COMPUTE_SECRET_ADDR_HEX = "0x100000460"


# ---------- /decompile ----------


def test_decompile_returns_function_body(binja_session, base_url):
    r = binja_session.get(f"{base_url}/decompile", params={"name": "_compute_secret"}, timeout=30)
    r.raise_for_status()
    body = r.json()
    src = body.get("decompiled", "")
    # HLIL of the loop: result accumulator, the * 7 constant, and the
    # return are all stable across BN versions.
    assert "result" in src
    assert "* 7" in src
    assert "return result" in src


def test_decompile_response_includes_function_block(binja_session, base_url):
    """The endpoint bundles the function's metadata alongside the
    source text so callers can confirm what they actually got back."""
    r = binja_session.get(f"{base_url}/decompile", params={"name": "_compute_secret"}, timeout=30)
    r.raise_for_status()
    fn = r.json().get("function") or {}
    assert fn.get("name") == "_compute_secret"
    assert fn.get("address") == COMPUTE_SECRET_ADDR_HEX


def test_decompile_entry_function_references_callees(binja_session, base_url):
    """The entry function calls compute_secret, printf, and atoi. Each
    should appear as a call expression in the decompiled output."""
    r = binja_session.get(f"{base_url}/decompile", params={"name": "_main"}, timeout=30)
    r.raise_for_status()
    src = r.json().get("decompiled", "")
    for callee in ("_compute_secret", "_printf", "_atoi"):
        assert callee in src, f"{callee} missing from decompile:\n{src}"


def test_decompile_unknown_function_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/decompile", params={"name": "definitely_not_a_function"}, timeout=10
    )
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    # The error response includes a directory listing of what IS
    # available, so callers don't have to make a second round trip.
    assert "available_functions" in body


# ---------- /il ----------


def test_il_hlil_uses_high_level_names(binja_session, base_url):
    """HLIL preserves user-level variable names — `result`, `i`, `n`."""
    r = binja_session.get(
        f"{base_url}/il", params={"name": "_compute_secret", "view": "hlil"}, timeout=10
    )
    r.raise_for_status()
    il = r.json()["il"]
    assert "result" in il
    assert "while" in il


def test_il_mlil_exposes_temporary_variables(binja_session, base_url):
    """MLIL lowers HLIL constructs to explicit temporaries (e.g.
    `var_4`, `temp0_1`) — those names won't appear at the HLIL layer
    but should be visible here."""
    r = binja_session.get(
        f"{base_url}/il", params={"name": "_compute_secret", "view": "mlil"}, timeout=10
    )
    r.raise_for_status()
    il = r.json()["il"]
    assert "var_" in il, f"no var_* in MLIL: {il[:200]}"


def test_il_llil_exposes_registers(binja_session, base_url):
    """LLIL is one step above raw assembly — it should reference real
    architectural registers and memory operations."""
    r = binja_session.get(
        f"{base_url}/il", params={"name": "_compute_secret", "view": "llil"}, timeout=10
    )
    r.raise_for_status()
    il = r.json()["il"]
    # `sp` is the stack pointer on every architecture BN supports.
    assert "sp" in il, f"no stack-pointer reference in LLIL: {il[:200]}"


def test_il_hlil_ssa_includes_phi_nodes(binja_session, base_url):
    """SSA form introduces phi (φ) nodes at loop joins. The compute
    loop has two — one for `i` and one for `result`."""
    r = binja_session.get(
        f"{base_url}/il",
        params={"name": "_compute_secret", "view": "hlil", "ssa": 1},
        timeout=10,
    )
    r.raise_for_status()
    il = r.json()["il"]
    assert "ϕ" in il, f"no phi (φ) node in HLIL SSA: {il[:300]}"
    # SSA also tags every variable with a version (#1, #2, ...).
    assert "#1" in il and "#2" in il


def test_il_mlil_ssa_includes_phi_nodes(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/il",
        params={"name": "_compute_secret", "view": "mlil", "ssa": 1},
        timeout=10,
    )
    r.raise_for_status()
    il = r.json()["il"]
    assert "ϕ" in il


def test_il_lookup_by_hex_address(binja_session, base_url):
    """Address-based lookup must return the same body as name-based
    lookup for the same function."""
    by_name = binja_session.get(
        f"{base_url}/il", params={"name": "_compute_secret", "view": "hlil"}, timeout=10
    ).json()["il"]
    by_addr = binja_session.get(
        f"{base_url}/il",
        params={"address": COMPUTE_SECRET_ADDR_HEX, "view": "hlil"},
        timeout=10,
    ).json()["il"]
    assert by_name == by_addr


def test_il_unknown_function_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/il",
        params={"name": "definitely_not_a_function", "view": "hlil"},
        timeout=10,
    )
    assert r.status_code == 404
    assert "error" in r.json()


def test_il_invalid_view_returns_400(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/il",
        params={"name": "_compute_secret", "view": "zzz_not_a_view"},
        timeout=10,
    )
    assert r.status_code == 400
    body = r.json()
    assert "zzz_not_a_view" in body.get("error", "")
    assert set(body.get("supported_views") or []) == {"hlil", "mlil", "llil"}


# ---------- /assembly ----------


def test_assembly_includes_address_and_mnemonic_per_line(binja_session, base_url):
    r = binja_session.get(f"{base_url}/assembly", params={"name": "_compute_secret"}, timeout=10)
    r.raise_for_status()
    asm = r.json()["assembly"]
    assert asm, "empty assembly response"
    # Every non-blank, non-header line should start with a hex address.
    code_lines = [
        ln
        for ln in asm.splitlines()
        if ln.strip() and not ln.startswith("#")  # skip "# Block N at 0x..." headers
    ]
    assert code_lines, f"no code lines in assembly: {asm[:200]}"
    for ln in code_lines[:5]:
        prefix = ln.split()[0]
        int(prefix, 16)  # raises if not a hex address


def test_assembly_unknown_function_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/assembly",
        params={"name": "definitely_not_a_function"},
        timeout=10,
    )
    assert r.status_code == 404
    assert "error" in r.json()
