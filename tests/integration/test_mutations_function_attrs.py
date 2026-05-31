"""Round-trip mutation tests for the function-attribute setters.

  /setFunctionCanReturn  — override BN's no-return inference.
  /setFunctionReturnType — set just the return type.
  /setFunctionInline     — toggle inline-during-analysis.
  /setFunctionPrototype  — replace the full prototype.
  /setLocalVariableType  — type a local var inside a function.

Both the setters and /undo call `update_analysis_and_wait()` after
their mutation, so reads of `getFunctionMetadata` / `decompile` see
the new state the moment those calls return — no manual reanalysis
prodding required.
"""


def _metadata(session, base_url, fn: str) -> dict:
    return session.get(f"{base_url}/getFunctionMetadata", params={"function": fn}, timeout=5).json()


FN = "_compute_secret"


# ---------- /setFunctionCanReturn ----------


def test_set_function_can_return_round_trip(binja_session, base_url):
    """Default `can_return` for our fixture is True. Set False, read
    back, undo, read back — both the setter and undo run a
    synchronous reanalysis so the metadata is fresh after each."""
    baseline = _metadata(binja_session, base_url, FN)
    assert baseline["can_return"] is True

    r = binja_session.get(
        f"{base_url}/setFunctionCanReturn",
        params={"function": FN, "canReturn": "false"},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["can_return"] is False

    try:
        assert _metadata(binja_session, base_url, FN)["can_return"] is False
    finally:
        binja_session.get(f"{base_url}/undo", timeout=15)
    assert _metadata(binja_session, base_url, FN)["can_return"] is True


# ---------- /setFunctionReturnType ----------


def test_set_function_return_type_round_trip(binja_session, base_url, anchors):
    """Change the return type of `_compute_secret` from int to void
    and confirm the change in the decompiled signature; one undo
    restores the original return type."""
    addr = anchors["compute_secret"]

    r = binja_session.get(
        f"{base_url}/setFunctionReturnType",
        params={"function": FN, "type": "void"},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["address"] == addr
    assert body["return_type"] == "void"

    try:
        # Decompile and check the signature reflects the void return —
        # the original `return result` statement disappears.
        decomp = binja_session.get(f"{base_url}/decompile", params={"name": FN}, timeout=30).json()[
            "decompiled"
        ]
        assert "return result" not in decomp
    finally:
        binja_session.get(f"{base_url}/undo", timeout=15)

    restored = binja_session.get(f"{base_url}/decompile", params={"name": FN}, timeout=30).json()[
        "decompiled"
    ]
    assert "return result" in restored


def test_set_function_return_type_invalid_type_returns_400(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/setFunctionReturnType",
        params={"function": FN, "type": "zzz_not_a_type_xyz"},
        timeout=5,
    )
    assert r.status_code == 400
    assert "Failed to parse type" in r.json().get("error", "")


# ---------- /setFunctionInline ----------


def test_set_function_inline_round_trip(binja_session, base_url):
    """The setter returns the new value; reading back via
    /getFunctionMetadata isn't useful because BN doesn't surface the
    inline flag there — so the response is what we pin. Undo is
    still required to leave the binary clean for later tests."""
    r = binja_session.get(
        f"{base_url}/setFunctionInline",
        params={"function": FN, "inline": "true"},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["inline_during_analysis"] is True

    binja_session.get(f"{base_url}/undo", timeout=10)


# ---------- /setFunctionPrototype ----------


def test_set_function_prototype_round_trip(binja_session, base_url, anchors):
    """Replace the full prototype. The server normalises C-types
    (e.g. `short` → `int16_t`) and echoes the canonical form in
    `applied_type`."""
    addr = anchors["compute_secret"]

    r = binja_session.get(
        f"{base_url}/setFunctionPrototype",
        params={
            "name": FN,
            "prototype": "char compute_secret(short n)",
        },
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["address"] == addr
    # `short` normalises to `int16_t`; function name in the proto
    # gets stripped (it's redundant when applied to a known function).
    assert body["applied_type"] == "char(int16_t n)"

    binja_session.get(f"{base_url}/undo", timeout=15)
    # Parameter count is back to 1 (the original `int n`).
    assert _metadata(binja_session, base_url, FN)["parameter_count"] == 1


def test_set_function_prototype_invalid_prototype_returns_400(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/setFunctionPrototype",
        params={"name": FN, "prototype": "this is not a c prototype"},
        timeout=5,
    )
    assert r.status_code == 400


# ---------- /setLocalVariableType ----------


def test_set_local_variable_type_round_trip(binja_session, base_url, anchors):
    """Retype the `result` local from int32_t to uint8_t and back.
    Cleanup is via undo — but variable mutations may not undo
    reliably on every BN version, so we also issue a manual retype
    back to int32_t as a fallback."""
    addr = anchors["compute_secret"]

    r = binja_session.get(
        f"{base_url}/setLocalVariableType",
        params={
            "functionAddress": addr,
            "variableName": "result",
            "newType": "uint8_t",
        },
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["variable"] == "result"
    assert body["applied_type"] == "uint8_t"

    try:
        # Verify the type change reached the stack frame.
        sframe = binja_session.get(
            f"{base_url}/getStackFrameVars",
            params={"function": FN},
            timeout=5,
        ).json()["stack_frame_vars"][0]["vars"]
        result_var = next((v for v in sframe if v["name"] == "result"), None)
        assert result_var is not None
        assert result_var["type"] == "uint8_t"
    finally:
        # Undo first; if it doesn't stick (variable-mutation undo is
        # flaky on some BN versions), retype back manually.
        binja_session.get(f"{base_url}/undo", timeout=10)
        binja_session.get(
            f"{base_url}/setLocalVariableType",
            params={
                "functionAddress": addr,
                "variableName": "result",
                "newType": "int32_t",
            },
            timeout=15,
        )


def test_set_local_variable_type_unknown_variable_returns_404(binja_session, base_url, anchors):
    r = binja_session.get(
        f"{base_url}/setLocalVariableType",
        params={
            "functionAddress": anchors["compute_secret"],
            "variableName": "zzz_no_such_var",
            "newType": "uint8_t",
        },
        timeout=5,
    )
    assert r.status_code in (400, 404)
    assert (
        "zzz_no_such_var" in r.json().get("error", "")
        or "not found" in r.json().get("error", "").lower()
    )


# ---------- shared error path ----------


def test_set_function_can_return_unknown_function_returns_404(binja_session, base_url):
    """The other setters share the same function-lookup; one
    unknown-function test stands in for all of them."""
    r = binja_session.get(
        f"{base_url}/setFunctionCanReturn",
        params={"function": "definitely_not_a_function", "canReturn": "false"},
        timeout=5,
    )
    assert r.status_code == 404
    assert "definitely_not_a_function" in r.json().get("error", "")
