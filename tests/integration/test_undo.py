"""Tests for the /undo endpoint.

Round-trip coverage lives in the per-mutation test files; this
file pins behaviour that's orthogonal to any specific mutation:
the response shape when the undo stack is empty or shorter than
the requested count.
"""


def _drain_undo_stack(session, base_url):
    """Pop entries until can_undo is False so a subsequent /undo
    call starts from a known-empty stack."""
    for _ in range(200):  # safety cap
        body = session.get(f"{base_url}/undo", params={"count": 1}, timeout=10).json()
        if body.get("can_undo") is False:
            return
    raise RuntimeError("undo stack didn't drain after 200 iterations")


def test_undo_on_empty_stack_reports_zero_performed(binja_session, base_url):
    """When there's nothing to undo, /undo must report performed=0
    rather than counting BN's `bv.undo() -> False` no-ops as if
    they were real reverts."""
    _drain_undo_stack(binja_session, base_url)

    body = binja_session.get(f"{base_url}/undo", params={"count": 5}, timeout=10).json()
    assert body["status"] == "ok"
    assert body["performed"] == 0, (
        f"undo on empty stack should perform 0 steps, got {body['performed']}"
    )
    assert body["can_undo"] is False
