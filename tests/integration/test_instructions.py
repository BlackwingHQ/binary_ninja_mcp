"""Instruction-level read endpoints: /getParameterAt,
/getConstantsReferencedBy, /getRegsReadBy, /getRegsWrittenBy.

Each of these needs an address that lies *inside* a function (the
server resolves the containing function automatically when one
isn't passed in). Addresses come from the `anchors` session fixture
so they survive rebuilds of `constructs`.

The fixture binary is compiled at -O0, so on arm64 every function
starts with `sub sp, sp, #N` (writes/reads `sp`) and stores its
first argument with `str w0, [sp, #...]` (reads `w0` and `sp`).
"""

NON_FUNCTION_ADDR = "0x100000000"


# ---------- /getParameterAt ----------


def test_get_parameter_at_call_site_returns_argument(binja_session, base_url, anchors):
    """At a call site, index=0 should return the lifted first
    argument. The entry function calls `_compute_secret(<x>)` with
    exactly one argument; the response pins the callee, the param
    count, and a non-empty expression. The expression itself comes
    from MLIL, which has already lowered `_atoi(argv[1])` into a
    variable reference — so we check the metadata fields rather than
    matching source text."""
    call_site = anchors["compute_secret_call_site"]
    r = binja_session.get(
        f"{base_url}/getParameterAt",
        params={"address": call_site, "index": 0},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["address"] == call_site
    assert body["index"] == 0
    assert body["callee"] == "_compute_secret"
    assert body["param_count"] == 1
    assert body["expression"], f"empty expression in {body}"


def test_get_parameter_at_out_of_range_errors(binja_session, base_url, anchors):
    """Index past the actual argument count surfaces as a 404 with a
    descriptive error — not as a 500."""
    r = binja_session.get(
        f"{base_url}/getParameterAt",
        params={"address": anchors["compute_secret_call_site"], "index": 99},
        timeout=5,
    )
    assert r.status_code == 404
    assert "out of range" in r.json().get("error", "")


# ---------- /getConstantsReferencedBy ----------


def test_constants_at_multiplier_instruction_includes_7(binja_session, base_url, anchors):
    """The `i * 7` expression compiles into a multiply whose constants
    list includes the literal 7."""
    r = binja_session.get(
        f"{base_url}/getConstantsReferencedBy",
        params={"address": anchors["compute_secret_mul7"]},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["function"] == "_compute_secret"
    values = [c.get("value") for c in body["constants"]]
    assert "0x7" in values, f"expected 0x7 in constants list: {values}"


def test_constants_response_carries_function_context(binja_session, base_url, anchors):
    """Even when there are no constants at the address, the response
    still tells the caller which function the address resolved to."""
    r = binja_session.get(
        f"{base_url}/getConstantsReferencedBy",
        params={"address": anchors["compute_secret_str_w0"]},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["function"] == "_compute_secret"
    assert body["function_address"] == anchors["compute_secret"]
    assert isinstance(body["constants"], list)
    assert body["count"] == len(body["constants"])


def test_constants_explicit_function_param_matches_auto_resolution(
    binja_session, base_url, anchors
):
    """Passing `function=` explicitly should give the same result as
    letting the server auto-resolve from the address."""
    auto = binja_session.get(
        f"{base_url}/getConstantsReferencedBy",
        params={"address": anchors["compute_secret_mul7"]},
        timeout=5,
    ).json()
    explicit = binja_session.get(
        f"{base_url}/getConstantsReferencedBy",
        params={
            "address": anchors["compute_secret_mul7"],
            "function": "_compute_secret",
        },
        timeout=5,
    ).json()
    assert auto == explicit


# ---------- /getRegsReadBy ----------


def test_regs_read_at_prologue_is_just_sp(binja_session, base_url, anchors):
    """`sub sp, sp, #0x10` reads sp (to subtract from it). Nothing
    else."""
    r = binja_session.get(
        f"{base_url}/getRegsReadBy",
        params={"address": anchors["compute_secret"]},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["function"] == "_compute_secret"
    assert body["registers"] == ["sp"]
    assert body["count"] == 1


def test_regs_read_at_str_w0_includes_w0_and_sp(binja_session, base_url, anchors):
    """`str w0, [sp, #0xc]` reads both w0 (the value being stored)
    and sp (the base register for the address calculation)."""
    r = binja_session.get(
        f"{base_url}/getRegsReadBy",
        params={"address": anchors["compute_secret_str_w0"]},
        timeout=5,
    )
    r.raise_for_status()
    regs = set(r.json()["registers"])
    assert {"w0", "sp"} <= regs, f"missing expected registers in {regs}"


def test_regs_read_at_non_function_address_errors(binja_session, base_url):
    """An address outside any function can't resolve to instructions
    without a `function=` hint; the server returns 400 with a help
    message rather than guessing."""
    r = binja_session.get(
        f"{base_url}/getRegsReadBy", params={"address": NON_FUNCTION_ADDR}, timeout=5
    )
    assert r.status_code == 400
    body = r.json()
    assert "Missing function identifier" in body.get("error", "")


# ---------- /getRegsWrittenBy ----------


def test_regs_written_at_str_w0_is_empty(binja_session, base_url, anchors):
    """`str w0, [sp, #0xc]` writes to memory, not to any register —
    so the written-regs set is empty. This pairs with the read-set
    test above to cover both halves of the same instruction."""
    r = binja_session.get(
        f"{base_url}/getRegsWrittenBy",
        params={"address": anchors["compute_secret_str_w0"]},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["function"] == "_compute_secret"
    assert body["registers"] == []


def test_regs_written_at_prologue_writes_sp(binja_session, base_url, anchors):
    """`sub sp, sp, #0x10` updates sp."""
    r = binja_session.get(
        f"{base_url}/getRegsWrittenBy",
        params={"address": anchors["compute_secret"]},
        timeout=5,
    )
    r.raise_for_status()
    assert "sp" in r.json()["registers"]
