"""Round-trip mutation tests for the rename endpoints.

Function and data renames cleanly undo, so those tests use the
standard `read baseline → mutate → assert → undo → assert restored`
pattern. Variable renames are NOT reliably undoable on every BN
version — the C++-layer `Variable.name` setter doesn't always
register with the undo stack — so variable-rename tests manually
rename back to the original to guarantee cleanup.

Note: /renameVariables records ONE undo entry per variable renamed
(response.undo_entries reflects this); batch undo isn't merged
server-side.
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


# ---------- /renameFunction ----------


def test_rename_function_round_trip(binja_session, base_url):
    """POST /renameFunction stores the new name verbatim; a single
    undo restores the original symbol."""
    methods_url = f"{base_url}/methods"
    new_name = "test_renamed_compute_secret"

    baseline_names = {
        f["name"] for f in binja_session.get(methods_url, timeout=5).json()["functions"]
    }
    assert "_compute_secret" in baseline_names
    assert new_name not in baseline_names

    binja_session.post(
        f"{base_url}/renameFunction",
        data={"oldName": "_compute_secret", "newName": new_name},
        timeout=10,
    ).raise_for_status()
    with _undo_after(binja_session, base_url):
        after_names = {
            f["name"] for f in binja_session.get(methods_url, timeout=5).json()["functions"]
        }
        assert new_name in after_names
        assert "_compute_secret" not in after_names

    restored = {f["name"] for f in binja_session.get(methods_url, timeout=5).json()["functions"]}
    assert "_compute_secret" in restored
    assert new_name not in restored


def test_rename_function_stores_name_verbatim(binja_session, base_url):
    """The server stores whatever name the caller provides — no
    prefix added, no transformation. This pins the contract so a
    future server change that introduces auto-prefixing is a
    deliberate, test-visible decision."""
    requested = "verbatim_no_prefix_target"
    r = binja_session.post(
        f"{base_url}/renameFunction",
        data={"oldName": "_compute_secret", "newName": requested},
        timeout=10,
    )
    r.raise_for_status()
    try:
        names = {
            f["name"]
            for f in binja_session.get(f"{base_url}/methods", timeout=5).json()["functions"]
        }
        assert requested in names, f"expected {requested!r} in {names}"
    finally:
        binja_session.get(f"{base_url}/undo", timeout=10)


def test_rename_function_unknown_old_name_does_not_disturb_baseline(binja_session, base_url):
    """Naming a function that doesn't exist must not modify any
    other function. We capture the full name set, ask for a bogus
    rename, and check the set is unchanged. No undo needed since
    nothing should have changed."""
    methods_url = f"{base_url}/methods"
    before = {f["name"] for f in binja_session.get(methods_url, timeout=5).json()["functions"]}

    binja_session.post(
        f"{base_url}/renameFunction",
        data={"oldName": "definitely_not_a_function_xyz", "newName": "should_not_appear"},
        timeout=10,
    )
    after = {f["name"] for f in binja_session.get(methods_url, timeout=5).json()["functions"]}
    assert after == before
    assert "should_not_appear" not in after


# ---------- /renameData ----------


def test_rename_data_round_trip(binja_session, base_url, anchors):
    """The data-rename endpoint does NOT prepend the rename prefix —
    only function names get the `mcp_` treatment. Pin that asymmetry
    so a future server change that adds the prefix to data renames is
    a deliberate, test-visible decision."""
    data_url = f"{base_url}/data"
    addr = anchors["default_task"]
    new_name = "renamed_default_task_test"

    baseline = {
        d["name"]
        for d in binja_session.get(data_url, params={"limit": 500}, timeout=10).json()["data"]
    }
    assert "default_task" in baseline
    assert new_name not in baseline

    binja_session.post(
        f"{base_url}/renameData",
        data={"address": addr, "newName": new_name},
        timeout=10,
    ).raise_for_status()
    with _undo_after(binja_session, base_url):
        after = {
            d["name"]
            for d in binja_session.get(data_url, params={"limit": 500}, timeout=10).json()["data"]
        }
        # No prefix added; the name we passed is the name stored.
        assert new_name in after
        assert "default_task" not in after

    restored = {
        d["name"]
        for d in binja_session.get(data_url, params={"limit": 500}, timeout=10).json()["data"]
    }
    assert "default_task" in restored
    assert new_name not in restored


# ---------- /renameVariable (single) ----------


def test_rename_single_variable_round_trip(binja_session, base_url):
    """`/renameVariable` succeeds at modifying the variable's name as
    seen by /getStackFrameVars and /il, but BN's undo stack doesn't
    reliably revert `Variable.name` mutations on every install. We
    therefore confirm the rename took effect and then manually rename
    back to the original — the only deterministic cleanup."""
    fn = "_compute_secret"
    original = "result"
    new_name = "test_acc_round_trip"
    stack_url = f"{base_url}/getStackFrameVars"

    baseline = {
        v["name"]
        for v in binja_session.get(stack_url, params={"function": fn}, timeout=5).json()[
            "stack_frame_vars"
        ][0]["vars"]
    }
    assert original in baseline, f"test precondition: {original!r} should be in stack frame"

    binja_session.get(
        f"{base_url}/renameVariable",
        params={"functionName": fn, "variableName": original, "newName": new_name},
        timeout=10,
    ).raise_for_status()
    try:
        after = {
            v["name"]
            for v in binja_session.get(stack_url, params={"function": fn}, timeout=5).json()[
                "stack_frame_vars"
            ][0]["vars"]
        }
        assert new_name in after
        assert original not in after
    finally:
        # Manual rollback — undo isn't reliable for variable renames.
        binja_session.get(
            f"{base_url}/renameVariable",
            params={"functionName": fn, "variableName": new_name, "newName": original},
            timeout=10,
        )

    restored = {
        v["name"]
        for v in binja_session.get(stack_url, params={"function": fn}, timeout=5).json()[
            "stack_frame_vars"
        ][0]["vars"]
    }
    assert original in restored


# ---------- /renameVariables (batch) ----------


def test_rename_multi_variables_pairs_syntax(binja_session, base_url):
    """`pairs=old1:new1,old2:new2` is the agent-friendly batch format.
    Confirms both renames take effect and the response reports the
    correct undo_entries count. Cleanup is a reverse-batch rename
    rather than undo, for the same reason as the single-rename test."""
    fn = "_compute_secret"
    stack_url = f"{base_url}/getStackFrameVars"

    baseline = {
        v["name"]
        for v in binja_session.get(stack_url, params={"function": fn}, timeout=5).json()[
            "stack_frame_vars"
        ][0]["vars"]
    }
    assert {"i", "result"} <= baseline, (
        f"test precondition: i and result should be in baseline, got {baseline}"
    )

    body = binja_session.get(
        f"{base_url}/renameVariables",
        params={"functionName": fn, "pairs": "i:test_loopvar,result:test_accum"},
        timeout=10,
    ).json()
    try:
        assert body["status"] == "ok"
        assert body["renamed"] == 2
        # Server is honest about how many undo entries this created
        # — agents should issue that many /undo calls to revert.
        assert body["undo_entries"] == 2

        after = {
            v["name"]
            for v in binja_session.get(stack_url, params={"function": fn}, timeout=5).json()[
                "stack_frame_vars"
            ][0]["vars"]
        }
        assert {"test_loopvar", "test_accum"} <= after
        assert "i" not in after and "result" not in after
    finally:
        # Reverse batch rename rather than relying on undo.
        binja_session.get(
            f"{base_url}/renameVariables",
            params={
                "functionName": fn,
                "pairs": "test_loopvar:i,test_accum:result",
            },
            timeout=10,
        )

    restored = {
        v["name"]
        for v in binja_session.get(stack_url, params={"function": fn}, timeout=5).json()[
            "stack_frame_vars"
        ][0]["vars"]
    }
    assert {"i", "result"} <= restored


def test_rename_multi_variables_reports_unknown_per_item(binja_session, base_url):
    """Mixing a real variable with a fictional one: the real one
    succeeds, the fictional one fails with a per-item error, and the
    overall response status stays 'ok'. This is the partial-success
    contract the agent relies on."""
    fn = "_compute_secret"
    body = binja_session.get(
        f"{base_url}/renameVariables",
        params={"functionName": fn, "pairs": "i:test_real_rename,nope_xyz:test_bogus"},
        timeout=10,
    ).json()
    try:
        assert body["status"] == "ok"
        assert body["renamed"] == 1
        results_by_old = {r["old"]: r for r in body["results"]}
        assert results_by_old["i"]["success"] is True
        assert results_by_old["nope_xyz"]["success"] is False
        assert "not found" in results_by_old["nope_xyz"]["error"].lower()
    finally:
        # Roll back only the real rename.
        binja_session.get(
            f"{base_url}/renameVariable",
            params={"functionName": fn, "variableName": "test_real_rename", "newName": "i"},
            timeout=10,
        )
