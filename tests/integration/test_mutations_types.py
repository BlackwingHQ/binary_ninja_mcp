"""Round-trip mutation tests for type endpoints.

Three mutation surfaces:

  /defineTypes    — parse a block of C and add every type it declares
                    (BN's "Tools → Define Types..."). Each type defined
                    becomes its own undo entry.
  /declareCType   — single declaration variant, same backing analyzer
                    but with a different response shape
                    ({defined_types, count}).
  /undefineUserType — remove a user-defined type. Itself undoable, so
                    undefine→undo cleanly restores the type. Rejects
                    DWARF-imported types and unknown names with 404.

The fixture binary has no other user-defined types, so each test
starts from a known-empty baseline for the names it touches.
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


def _type_source(session, base_url, name: str) -> str:
    """Convenience: returns the `source` field of /getTypeInfo —
    'local' for user/DWARF types, 'platform' / 'library' for libc
    typedefs, 'unknown' when the name doesn't resolve."""
    return (
        session.get(f"{base_url}/getTypeInfo", params={"name": name}, timeout=5)
        .json()
        .get("source")
    )


# ---------- /defineTypes ----------


def test_define_types_round_trip(binja_session, base_url):
    """Defining a single struct registers it as a view-local type
    with member info; one undo removes it."""
    name = "test_define_round_trip"
    assert _type_source(binja_session, base_url, name) == "unknown"

    r = binja_session.get(
        f"{base_url}/defineTypes",
        params={"cCode": f"struct {name} {{ int a; int b; }};"},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body == {name: "struct"}

    with _undo_after(binja_session, base_url):
        info = binja_session.get(f"{base_url}/getTypeInfo", params={"name": name}, timeout=5).json()
        assert info["source"] == "local"
        assert info["kind"] == "struct"
        members = {m["name"]: m for m in info["members"]}
        assert set(members) == {"a", "b"}
        assert members["a"]["offset"] == 0
        assert members["b"]["offset"] == 4

    assert _type_source(binja_session, base_url, name) == "unknown"


def test_define_types_can_register_an_enum(binja_session, base_url):
    """Enums round-trip the same way as structs — `kind=enum` and
    `enum_members` populated."""
    name = "test_define_enum"
    binja_session.get(
        f"{base_url}/defineTypes",
        params={"cCode": f"enum {name} {{ A = 1, B = 2, C = 10 }};"},
        timeout=10,
    ).raise_for_status()
    with _undo_after(binja_session, base_url):
        info = binja_session.get(f"{base_url}/getTypeInfo", params={"name": name}, timeout=5).json()
        assert info["kind"] == "enum"
        by_name = {m["name"]: m["value"] for m in info["enum_members"]}
        assert by_name == {"A": 1, "B": 2, "C": 10}


def test_define_types_multi_uses_one_undo_entry_per_type(binja_session, base_url):
    """One /defineTypes call declaring N types pushes N separate undo
    entries (LIFO order). The bridge's response is a single dict, but
    revert costs one undo per type. Pinning this so a future BN
    change that merges them into one entry is a visible improvement."""
    code = "struct test_multi_a { int x; };struct test_multi_b { int y; };"
    binja_session.get(
        f"{base_url}/defineTypes", params={"cCode": code}, timeout=10
    ).raise_for_status()
    try:
        assert _type_source(binja_session, base_url, "test_multi_a") == "local"
        assert _type_source(binja_session, base_url, "test_multi_b") == "local"

        binja_session.get(f"{base_url}/undo", timeout=5)
        # The second-declared type reverts first (LIFO).
        assert _type_source(binja_session, base_url, "test_multi_a") == "local"
        assert _type_source(binja_session, base_url, "test_multi_b") == "unknown"

        binja_session.get(f"{base_url}/undo", timeout=5)
        assert _type_source(binja_session, base_url, "test_multi_a") == "unknown"
        assert _type_source(binja_session, base_url, "test_multi_b") == "unknown"
    except Exception:
        # If an assert fires mid-test, drain any remaining undo
        # entries so the next test starts clean.
        for _ in range(2):
            binja_session.get(f"{base_url}/undo", timeout=5)
        raise


# ---------- /declareCType ----------


def test_declare_c_type_round_trip(binja_session, base_url):
    """Single-declaration variant. Response wraps the result as
    `{defined_types: {name: kind}, count: N}`."""
    name = "test_declare_round_trip"
    r = binja_session.get(
        f"{base_url}/declareCType",
        params={"declaration": f"struct {name} {{ char *label; int id; }};"},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    assert body == {"defined_types": {name: "struct"}, "count": 1}

    with _undo_after(binja_session, base_url):
        info = binja_session.get(f"{base_url}/getTypeInfo", params={"name": name}, timeout=5).json()
        assert info["source"] == "local"
        assert info["kind"] == "struct"
        members = {m["name"]: m for m in info["members"]}
        assert "label" in members and "char" in members["label"]["type"]
        assert "id" in members and "int" in members["id"]["type"]

    assert _type_source(binja_session, base_url, name) == "unknown"


# ---------- /undefineUserType ----------


def test_undefine_user_type_round_trip(binja_session, base_url):
    """Seed a user type, undefine it, verify it's gone, undo, verify
    it's back. This pins that /undefineUserType is itself undoable —
    a property that lets an agent treat both define and undefine as
    safe explorations."""
    name = "test_undefine_round_trip"
    # Seed.
    binja_session.get(
        f"{base_url}/defineTypes",
        params={"cCode": f"struct {name} {{ int z; }};"},
        timeout=10,
    ).raise_for_status()
    try:
        assert _type_source(binja_session, base_url, name) == "local"

        r = binja_session.get(f"{base_url}/undefineUserType", params={"name": name}, timeout=10)
        r.raise_for_status()
        body = r.json()
        assert body["status"] == "ok"
        assert body["name"] == name
        assert "removed_declaration" in body
        assert _type_source(binja_session, base_url, name) == "unknown"

        # Undo of the undefine restores the type.
        binja_session.get(f"{base_url}/undo", timeout=5)
        assert _type_source(binja_session, base_url, name) == "local"
    finally:
        # Two undos total: one for the (post-undo) define, one to
        # peel the original define. Run unconditionally so a failed
        # assertion doesn't leak the type into later tests.
        for _ in range(2):
            binja_session.get(f"{base_url}/undo", timeout=5)


def test_undefine_user_type_rejects_dwarf_type(binja_session, base_url):
    """DWARF-imported types (like the fixture's `task_t`) are
    view-local but not user-defined. The endpoint distinguishes the
    two — only user-CREATED types can be removed this way."""
    r = binja_session.get(f"{base_url}/undefineUserType", params={"name": "task_t"}, timeout=10)
    assert r.status_code == 404
    assert "user type" in r.json().get("error", "")


def test_undefine_user_type_unknown_returns_404(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/undefineUserType",
        params={"name": "definitely_not_a_user_type_xyz"},
        timeout=10,
    )
    assert r.status_code == 404
    assert "not defined as a user type" in r.json().get("error", "")
