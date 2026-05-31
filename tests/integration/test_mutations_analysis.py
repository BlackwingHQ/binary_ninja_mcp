"""Tests for the analysis-trigger endpoints.

  /reanalyzeFunction    — queue a single function for reanalysis.
                          Returns as soon as BN accepts the request.
  /updateAnalysisAndWait — drive the full analysis queue to idle
                          and block until it's done.

Neither endpoint changes persistent binary state, so there's no
round-trip to undo. The tests cover response shape + the error
cases an agent might hit.
"""


# ---------- /reanalyzeFunction ----------


def test_reanalyze_function_by_name(binja_session, base_url):
    """Happy path by function name. The response carries the
    canonical name + address and a note pointing at the heavier
    /updateAnalysisAndWait call for callers who need the result
    fully settled before their next query."""
    r = binja_session.get(
        f"{base_url}/reanalyzeFunction",
        params={"function": "_compute_secret"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["function"] == "_compute_secret"
    assert body["address"].startswith("0x")
    assert "update_analysis" in body.get("note", "")


def test_reanalyze_function_by_address(binja_session, base_url, anchors):
    """Address-form lookup. The response normalises to the same
    canonical name as the by-name call."""
    by_name = binja_session.get(
        f"{base_url}/reanalyzeFunction",
        params={"function": "_compute_secret"},
        timeout=30,
    ).json()
    by_addr = binja_session.get(
        f"{base_url}/reanalyzeFunction",
        params={"function": anchors["compute_secret"]},
        timeout=30,
    ).json()
    assert by_name["function"] == by_addr["function"]
    assert by_name["address"] == by_addr["address"]


def test_reanalyze_function_unknown_returns_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/reanalyzeFunction",
        params={"function": "definitely_not_a_function"},
        timeout=5,
    )
    assert r.status_code == 404
    assert "definitely_not_a_function" in r.json().get("error", "")


def test_reanalyze_function_missing_identifier_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/reanalyzeFunction", timeout=5)
    assert r.status_code == 400
    body = r.json()
    assert "Missing function identifier" in body.get("error", "")


# ---------- /updateAnalysisAndWait ----------


def test_update_analysis_and_wait_returns_idle_state(binja_session, base_url):
    """The blocking variant runs to completion before returning. The
    response carries a duration in ms and a snapshot of BN's
    `analysis_info` so the caller can confirm the queue is idle."""
    r = binja_session.get(f"{base_url}/updateAnalysisAndWait", timeout=60)
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["duration_ms"], int)
    assert body["duration_ms"] >= 0
    info = body["analysis_info"]
    # Shape pinned so a future BN that renames these fields surfaces
    # as a deliberate test change.
    assert "state" in info
    assert "analysis_time" in info
    assert "active_info" in info


def test_update_analysis_and_wait_is_idempotent(binja_session, base_url):
    """Two calls in a row are both fast (≈0 ms after the first)
    because the second call finds an already-idle queue. Pin that
    so a future change that re-queues work on every call surfaces
    as a regression."""
    binja_session.get(f"{base_url}/updateAnalysisAndWait", timeout=60).raise_for_status()
    second = binja_session.get(f"{base_url}/updateAnalysisAndWait", timeout=10).json()
    # Second call should be fast — the queue is already idle. 100ms
    # is a generous ceiling for a fixture this small.
    assert second["duration_ms"] < 100
