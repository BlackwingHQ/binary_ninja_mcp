"""Round-trip mutation tests for comment endpoints.

Each test follows the same pattern:

  1. read baseline state
  2. apply the mutation
  3. assert the new state inside try/
  4. finally: undo so the binary returns to baseline
  5. confirm the post-undo state matches the baseline

The `try/finally` is the safety net — if an assertion fails mid-test,
the `finally` block still issues the undo so the next test starts
clean. The post-finally baseline check is a second-line guarantee
that the undo actually worked.

These tests do not run in parallel — they share the live Binary
Ninja view. pytest runs serially by default; do not enable
pytest-xdist for this suite.
"""

import contextlib


@contextlib.contextmanager
def _undo_after(session, base_url, expected_undos: int = 1):
    """Run the block, then issue `expected_undos` undo calls. The
    caller is responsible for verifying the baseline was restored —
    this just guarantees the cleanup attempt happens even when an
    assertion fails."""
    try:
        yield
    finally:
        for _ in range(expected_undos):
            try:
                session.get(f"{base_url}/undo", timeout=10)
            except Exception:
                # Best-effort cleanup; don't mask the original failure.
                pass


# ---------- /comment (set / get / delete) ----------


def test_set_comment_round_trip(binja_session, base_url, anchors):
    """POST /comment writes a comment that GET /comment then sees;
    a single undo restores the no-comment baseline."""
    addr = anchors["compute_secret"]
    baseline = binja_session.get(f"{base_url}/comment", params={"address": addr}, timeout=5).json()
    assert baseline["comment"] is None, (
        f"test precondition: expected no comment at {addr}, got {baseline}"
    )

    r = binja_session.post(
        f"{base_url}/comment",
        data={"address": addr, "comment": "test set_comment_round_trip"},
        timeout=5,
    )
    r.raise_for_status()
    with _undo_after(binja_session, base_url):
        after_set = binja_session.get(
            f"{base_url}/comment", params={"address": addr}, timeout=5
        ).json()
        assert after_set["comment"] == "test set_comment_round_trip"
        assert after_set["address"] == addr

    restored = binja_session.get(f"{base_url}/comment", params={"address": addr}, timeout=5).json()
    assert restored["comment"] is None, f"baseline not restored after undo: {restored}"


def test_delete_comment_round_trip(binja_session, base_url, anchors):
    """DELETE /comment removes an existing comment; undo restores it.
    This test sets up its own pre-state (a known comment) so it can
    cleanly assert the delete-then-undo cycle."""
    addr = anchors["compute_secret"]
    # Seed a comment we can delete. This counts as the first mutation
    # in the undo stack; the delete is the second.
    binja_session.post(
        f"{base_url}/comment",
        data={"address": addr, "comment": "soon-to-be-deleted"},
        timeout=5,
    ).raise_for_status()
    try:
        deleted = binja_session.delete(f"{base_url}/comment", params={"address": addr}, timeout=5)
        deleted.raise_for_status()
        with _undo_after(binja_session, base_url):
            after_delete = binja_session.get(
                f"{base_url}/comment", params={"address": addr}, timeout=5
            ).json()
            assert after_delete["comment"] is None

        # The undo in _undo_after reversed the DELETE, restoring the
        # comment we seeded. We now confirm that and clean it up.
        after_undo = binja_session.get(
            f"{base_url}/comment", params={"address": addr}, timeout=5
        ).json()
        assert after_undo["comment"] == "soon-to-be-deleted"
    finally:
        # Undo the seed POST so the test leaves the binary as it
        # found it. Run this unconditionally so a failure above
        # doesn't leak a comment into later tests.
        binja_session.get(f"{base_url}/undo", timeout=10)


def test_set_comment_overwrites_previous_text(binja_session, base_url, anchors):
    """Writing a second comment at the same address replaces the
    first; both mutations are independently undoable."""
    addr = anchors["compute_secret"]
    binja_session.post(
        f"{base_url}/comment", data={"address": addr, "comment": "first"}, timeout=5
    ).raise_for_status()
    try:
        binja_session.post(
            f"{base_url}/comment", data={"address": addr, "comment": "second"}, timeout=5
        ).raise_for_status()
        try:
            after_second = binja_session.get(
                f"{base_url}/comment", params={"address": addr}, timeout=5
            ).json()
            assert after_second["comment"] == "second"
        finally:
            binja_session.get(f"{base_url}/undo", timeout=10)
        # The intermediate undo reversed the second POST, exposing
        # the first comment again.
        after_undo1 = binja_session.get(
            f"{base_url}/comment", params={"address": addr}, timeout=5
        ).json()
        assert after_undo1["comment"] == "first"
    finally:
        # Undo the first POST so the binary is clean.
        binja_session.get(f"{base_url}/undo", timeout=10)
    restored = binja_session.get(f"{base_url}/comment", params={"address": addr}, timeout=5).json()
    assert restored["comment"] is None, f"baseline not restored: {restored}"


# ---------- /comment/function (set / get / delete) ----------


def test_set_function_comment_round_trip(binja_session, base_url):
    """The function-level comment is keyed by function name rather
    than address; otherwise the round-trip looks identical."""
    fn = "_compute_secret"
    baseline = binja_session.get(
        f"{base_url}/comment/function", params={"name": fn}, timeout=5
    ).json()
    assert baseline["comment"] is None

    binja_session.post(
        f"{base_url}/comment/function",
        data={"name": fn, "comment": "computes secret via i*7 accumulator"},
        timeout=5,
    ).raise_for_status()
    with _undo_after(binja_session, base_url):
        after = binja_session.get(
            f"{base_url}/comment/function", params={"name": fn}, timeout=5
        ).json()
        assert after["comment"] == "computes secret via i*7 accumulator"
        assert after["function"] == fn

    restored = binja_session.get(
        f"{base_url}/comment/function", params={"name": fn}, timeout=5
    ).json()
    assert restored["comment"] is None


def test_delete_function_comment_round_trip(binja_session, base_url):
    """Same delete-then-undo pattern as the address-level test."""
    fn = "_compute_secret"
    binja_session.post(
        f"{base_url}/comment/function",
        data={"name": fn, "comment": "seed for delete test"},
        timeout=5,
    ).raise_for_status()
    try:
        binja_session.delete(
            f"{base_url}/comment/function", params={"name": fn}, timeout=5
        ).raise_for_status()
        with _undo_after(binja_session, base_url):
            after_delete = binja_session.get(
                f"{base_url}/comment/function", params={"name": fn}, timeout=5
            ).json()
            assert after_delete["comment"] is None

        after_undo = binja_session.get(
            f"{base_url}/comment/function", params={"name": fn}, timeout=5
        ).json()
        assert after_undo["comment"] == "seed for delete test"
    finally:
        binja_session.get(f"{base_url}/undo", timeout=10)
