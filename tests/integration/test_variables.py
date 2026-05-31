"""Variable-level read endpoints: /getStackFrameVars, /getVarUses,
/getVarDefinitions, /getSsaVarUses, /getSsaVarDefinition.

Anchor variables inside `_compute_secret`:
  - `n`      — the parameter (first SSA version 0/1)
  - `result` — the accumulator (`result = 0`, then `result += i * 7`)
  - `i`      — the loop counter (`i = 1`, then `i += 1`)

Each one has both a defining instruction (the assignment) and one or
more uses (the reads inside the loop body and the final return), so
the same fixture covers def / use queries in both non-SSA and SSA
forms.
"""

# ---------- /getStackFrameVars ----------


def test_stack_frame_vars_by_function_name(binja_session, base_url):
    """The fixture's `_compute_secret` has its parameter `n` and local
    accumulators `result` / `i` on the stack frame."""
    r = binja_session.get(
        f"{base_url}/getStackFrameVars",
        params={"function": "_compute_secret"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    payload = body["stack_frame_vars"]
    # Whatever shape the endpoint serialises into, the variable names
    # we care about must appear *somewhere* in its stringified body.
    blob = str(payload)
    for name in ("n", "result", "i"):
        assert name in blob, f"expected {name!r} in stack-frame payload: {payload}"


def test_stack_frame_vars_by_address(binja_session, base_url):
    """`function=` accepts both names and hex addresses."""
    r = binja_session.get(
        f"{base_url}/getStackFrameVars",
        params={"function": "0x100000460"},
        timeout=5,
    )
    r.raise_for_status()
    assert r.json()["stack_frame_vars"]


# ---------- /getVarUses ----------


def test_var_uses_finds_result_in_loop_body(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getVarUses",
        params={
            "function": "_compute_secret",
            "variable": "result",
            "ilLevel": "hlil",
        },
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["function"] == "_compute_secret"
    assert body["variable"] == "result"
    assert body["count"] == len(body["uses"])
    assert body["uses"], f"no uses returned: {body}"
    # Every use is filtered to HLIL when ilLevel=hlil.
    for use in body["uses"]:
        assert use["address"].startswith("0x")
        if use.get("il_type"):
            assert "hlil" in use["il_type"].lower()


def test_var_uses_il_level_all_includes_both_layers(binja_session, base_url):
    """The default `all` filter returns uses from both HLIL and MLIL —
    so the total count must be at least as large as a single-level
    query."""
    hlil = binja_session.get(
        f"{base_url}/getVarUses",
        params={"function": "_compute_secret", "variable": "result", "ilLevel": "hlil"},
        timeout=5,
    ).json()
    all_levels = binja_session.get(
        f"{base_url}/getVarUses",
        params={"function": "_compute_secret", "variable": "result", "ilLevel": "all"},
        timeout=5,
    ).json()
    assert all_levels["count"] >= hlil["count"]
    types = {u.get("il_type", "").lower() for u in all_levels["uses"] if u.get("il_type")}
    assert any("hlil" in t for t in types)
    assert any("mlil" in t for t in types)


def test_var_uses_llil_rejected_with_clear_message(binja_session, base_url):
    """LLIL does not model named variables, so the endpoint must
    refuse this rather than silently returning nothing."""
    r = binja_session.get(
        f"{base_url}/getVarUses",
        params={
            "function": "_compute_secret",
            "variable": "result",
            "ilLevel": "llil",
        },
        timeout=5,
    )
    assert r.status_code == 404
    assert "LLIL" in r.json().get("error", "")


def test_var_uses_unknown_variable_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getVarUses",
        params={
            "function": "_compute_secret",
            "variable": "definitely_not_a_var",
            "ilLevel": "all",
        },
        timeout=5,
    )
    assert r.status_code == 404
    assert "definitely_not_a_var" in r.json().get("error", "")


# ---------- /getVarDefinitions ----------


def test_var_definitions_finds_initial_assignment(binja_session, base_url):
    """`result = 0` is the (only) definition site at the function
    entry — HLIL records exactly one definition for it."""
    r = binja_session.get(
        f"{base_url}/getVarDefinitions",
        params={
            "function": "_compute_secret",
            "variable": "result",
            "ilLevel": "hlil",
        },
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["function"] == "_compute_secret"
    assert body["variable"] == "result"
    assert body["count"] == len(body["definitions"])
    assert body["definitions"], f"no definitions returned: {body}"
    for defn in body["definitions"]:
        assert defn["address"].startswith("0x")


# ---------- /getSsaVarUses ----------


def test_ssa_var_uses_version_1_finds_loop_read(binja_session, base_url):
    """SSA version 1 of `result` is the initial `result = 0`; its only
    use site is the loop body where it gets read into the next phi."""
    r = binja_session.get(
        f"{base_url}/getSsaVarUses",
        params={
            "function": "_compute_secret",
            "variable": "result",
            "version": 1,
            "ilLevel": "hlil",
        },
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["variable"] == "result"
    assert body["version"] == 1
    assert body["count"] == len(body["uses"])
    assert body["count"] >= 1
    for use in body["uses"]:
        assert use["address"].startswith("0x")
        assert use["kind"] == "use"
        assert use["expression"]


def test_ssa_var_uses_version_2_has_multiple_sites(binja_session, base_url):
    """Version 2 (the phi-merged value) is read both in the loop body
    update and in the final return — so it has at least two uses."""
    r = binja_session.get(
        f"{base_url}/getSsaVarUses",
        params={
            "function": "_compute_secret",
            "variable": "result",
            "version": 2,
            "ilLevel": "hlil",
        },
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["count"] >= 2


# ---------- /getSsaVarDefinition ----------


def test_ssa_var_definition_version_1_is_initial_assignment(binja_session, base_url):
    """`result#1` is defined by `int32_t result = 0` at the top of
    the function."""
    r = binja_session.get(
        f"{base_url}/getSsaVarDefinition",
        params={
            "function": "_compute_secret",
            "variable": "result",
            "version": 1,
            "ilLevel": "hlil",
        },
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    defn = body["definition"]
    assert defn["kind"] == "definition"
    assert defn["address"].startswith("0x")
    assert "result = 0" in defn["expression"]
