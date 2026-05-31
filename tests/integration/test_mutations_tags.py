"""Round-trip mutation tests for tag endpoints.

Three layers of state to round-trip:

  - tag *types* (categories like "Bug", "Crypto") created via
    /createTagType; revertible with /undo.
  - tags (instances of a tag type at a location), one per kind:
        data     — attached to a data address via BinaryView.add_tag
        address  — attached to a code address inside a function
        function — attached to the whole function (no specific addr)
    Each addTag call is revertible with /undo.

The fixture has no user tags or custom tag types, so every test
starts from a known-empty baseline.
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


def _tag_type_names(session, base_url) -> set[str]:
    body = session.get(f"{base_url}/tagTypes", timeout=5).json()
    return {t["name"] for t in body.get("tag_types", [])}


def _tags_at(session, base_url, address: str) -> dict:
    return session.get(f"{base_url}/getTagsAt", params={"address": address}, timeout=5).json()


# ---------- /createTagType ----------


def test_create_tag_type_round_trip(binja_session, base_url):
    """A new tag-type name shows up in /tagTypes; a single undo
    removes it. `created: true` distinguishes a fresh creation from
    a no-op when the name already exists."""
    name = "RoundTripTagType"
    assert name not in _tag_type_names(binja_session, base_url)

    r = binja_session.get(
        f"{base_url}/createTagType",
        params={"name": name, "icon": "🔬"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["name"] == name
    assert body["created"] is True

    with _undo_after(binja_session, base_url):
        assert name in _tag_type_names(binja_session, base_url)

    assert name not in _tag_type_names(binja_session, base_url)


def test_create_tag_type_idempotent_when_already_exists(binja_session, base_url):
    """Asking to create a name that's already registered (e.g. one
    of BN's built-ins like 'Bugs') returns `created: false`. No
    undo is needed since no mutation occurred."""
    r = binja_session.get(
        f"{base_url}/createTagType",
        params={"name": "Bugs", "icon": "🐛"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    assert body["status"] == "ok"
    assert body["created"] is False


def test_create_tag_type_missing_name_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/createTagType", timeout=5)
    assert r.status_code == 400
    assert "Missing name" in r.json().get("error", "")


# ---------- /addTag (data kind) ----------


def test_add_data_tag_round_trip(binja_session, base_url, anchors):
    """Data tag on the `default_task` global. /getTagsAt at the
    same address sees it under `data_tags`; one undo removes it."""
    addr = anchors["default_task"]
    tag_type = "DataRoundTripTag"

    # Auto-creates the tag type and the data tag in one call.
    r = binja_session.get(
        f"{base_url}/addTag",
        params={
            "address": addr,
            "tagType": tag_type,
            "data": "marking default_task",
            "kind": "data",
        },
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body["kind"] == "data"
    assert body["address"] == addr
    assert body["tag_type"] == tag_type

    # Data-tag /addTag pushes three undo entries (vs two for the
    # function/address kinds): BN's `BinaryView.add_tag` records an
    # extra entry beyond the tag itself. Empirically determined.
    with _undo_after(binja_session, base_url, count=3):
        tags = _tags_at(binja_session, base_url, addr)
        match = next((t for t in tags["data_tags"] if t["type"] == tag_type), None)
        assert match is not None, f"data tag missing: {tags}"
        assert match["data"] == "marking default_task"
        assert match["address"] == addr
        assert match["kind"] == "data"

    after = _tags_at(binja_session, base_url, addr)
    assert all(t["type"] != tag_type for t in after["data_tags"])
    assert tag_type not in _tag_type_names(binja_session, base_url)


# ---------- /addTag (address kind) ----------


def test_add_address_tag_round_trip(binja_session, base_url, anchors):
    """Address tag attached to a specific instruction inside
    `_compute_secret`. It shows up under `address_tags` at that
    exact address; two undos revert both the tag and its type."""
    addr = anchors["compute_secret_mul7"]  # `mov w?, #0x7` in the loop
    tag_type = "AddressRoundTripTag"

    binja_session.get(
        f"{base_url}/addTag",
        params={
            "address": addr,
            "tagType": tag_type,
            "data": "i*7 site",
            "kind": "address",
        },
        timeout=10,
    ).raise_for_status()

    with _undo_after(binja_session, base_url, count=2):
        tags = _tags_at(binja_session, base_url, addr)
        match = next((t for t in tags["address_tags"] if t["type"] == tag_type), None)
        assert match is not None, f"address tag missing at {addr}: {tags}"
        assert match["data"] == "i*7 site"
        assert match["kind"] == "address"
        assert match["address"] == addr

    after = _tags_at(binja_session, base_url, addr)
    assert all(t["type"] != tag_type for t in after["address_tags"])
    assert tag_type not in _tag_type_names(binja_session, base_url)


# ---------- /addTag (function kind) ----------


def test_add_function_tag_round_trip(binja_session, base_url, anchors):
    """Function tag applies to the whole function — the response
    has no `address`, and /getTagsAt sees it under `function_tags`
    at every address in that function (including its start)."""
    fn_start = anchors["compute_secret"]
    inside = anchors["compute_secret_mul7"]
    tag_type = "FunctionRoundTripTag"

    binja_session.get(
        f"{base_url}/addTag",
        params={
            "address": fn_start,
            "tagType": tag_type,
            "data": "needs review",
            "kind": "function",
        },
        timeout=10,
    ).raise_for_status()

    with _undo_after(binja_session, base_url, count=2):
        # Visible at function start.
        at_start = _tags_at(binja_session, base_url, fn_start)
        m1 = next((t for t in at_start["function_tags"] if t["type"] == tag_type), None)
        assert m1 is not None, f"function tag missing at start: {at_start}"
        assert m1["address"] is None  # function tags have no specific addr
        assert m1["data"] == "needs review"

        # Also visible at any address inside the function.
        at_inside = _tags_at(binja_session, base_url, inside)
        m2 = next((t for t in at_inside["function_tags"] if t["type"] == tag_type), None)
        assert m2 is not None, f"function tag missing at mid-function addr: {at_inside}"

    after = _tags_at(binja_session, base_url, fn_start)
    assert all(t["type"] != tag_type for t in after["function_tags"])
    assert tag_type not in _tag_type_names(binja_session, base_url)


# ---------- /addTag (auto kind picks the right scope) ----------


def test_add_tag_auto_picks_function_for_function_start(binja_session, base_url, anchors):
    """`kind=auto` at a function-start address resolves to `function`."""
    addr = anchors["compute_secret"]
    tag_type = "AutoPicksFunctionTag"

    r = binja_session.get(
        f"{base_url}/addTag",
        params={"address": addr, "tagType": tag_type, "kind": "auto"},
        timeout=10,
    )
    r.raise_for_status()
    with _undo_after(binja_session, base_url, count=2):
        assert r.json()["kind"] == "function"


def test_add_tag_auto_picks_address_for_in_function_addr(binja_session, base_url, anchors):
    """`kind=auto` inside a function (not the start) resolves to `address`."""
    addr = anchors["compute_secret_mul7"]
    tag_type = "AutoPicksAddressTag"

    r = binja_session.get(
        f"{base_url}/addTag",
        params={"address": addr, "tagType": tag_type, "kind": "auto"},
        timeout=10,
    )
    r.raise_for_status()
    with _undo_after(binja_session, base_url, count=2):
        assert r.json()["kind"] == "address"


def test_add_tag_auto_picks_data_for_data_address(binja_session, base_url, anchors):
    """`kind=auto` outside any function resolves to `data`."""
    addr = anchors["default_task"]
    tag_type = "AutoPicksDataTag"

    r = binja_session.get(
        f"{base_url}/addTag",
        params={"address": addr, "tagType": tag_type, "kind": "auto"},
        timeout=10,
    )
    r.raise_for_status()
    with _undo_after(binja_session, base_url, count=2):
        assert r.json()["kind"] == "data"


# ---------- /addTag error paths ----------


def test_add_tag_missing_params_returns_400(binja_session, base_url):
    r = binja_session.get(f"{base_url}/addTag", timeout=5)
    assert r.status_code == 400
    body = r.json()
    assert "Missing parameters" in body.get("error", "")


def test_add_tag_unknown_kind_returns_400(binja_session, base_url, anchors):
    r = binja_session.get(
        f"{base_url}/addTag",
        params={
            "address": anchors["compute_secret"],
            "tagType": "Bugs",  # any existing type
            "kind": "zzz_not_a_kind",
        },
        timeout=5,
    )
    assert r.status_code == 400
    assert "Unknown tag kind" in r.json().get("error", "")
