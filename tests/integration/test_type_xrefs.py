"""Type-graph xref endpoints: /getXrefsToStruct, /getXrefsToField,
/getXrefsToEnum, /getXrefsToUnion, /getXrefsToType.

These answer "where in the code is this type / field / enum
constant used?". The fixture wires up a minimal but exhaustive
example so each endpoint has a real reference to find:

  _task_priority(const task_t *t):
      compares  t->pri  against PRIORITY_CRITICAL
      returns   t->pri

  _value_low_byte(const value_view_t *v):
      returns   v->bytes[0]

That gives us:
  - struct task_t      → referenced by `_task_priority` (via its
                         parameter type) and by `default_task`
                         (the global instance)
  - task_t.pri         → read twice inside `_task_priority`
  - enum priority_t    → constant `PRIORITY_CRITICAL` referenced
                         in the comparison
  - union value_view_t → referenced by `_value_low_byte`
"""


# ---------- /getXrefsToStruct ----------


def test_xrefs_to_struct_lists_typed_function(binja_session, base_url):
    r = binja_session.get(f"{base_url}/getXrefsToStruct", params={"name": "task_t"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["struct"] == "task_t"
    assert "_task_priority" in body["functions_with_type"]


def test_xrefs_to_struct_lists_typed_variables(binja_session, base_url):
    """The typed-vars list pairs each var with its containing
    function and the declared type — useful for "where is this
    struct being passed around?" queries."""
    r = binja_session.get(f"{base_url}/getXrefsToStruct", params={"name": "task_t"}, timeout=5)
    r.raise_for_status()
    vars_with_type = r.json()["vars_with_type"]
    assert any(
        v["function"] == "_task_priority" and "task_t" in v["type"] for v in vars_with_type
    ), f"_task_priority's task_t* parameter missing: {vars_with_type}"


def test_xrefs_to_struct_unknown_name_returns_empty_arrays(binja_session, base_url):
    """Unknown types return a 200 with every array empty. The
    contract is "no xrefs", not 404 — that lets the agent ask the
    question without first checking type existence."""
    r = binja_session.get(
        f"{base_url}/getXrefsToStruct", params={"name": "does_not_exist_xyz"}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    assert body["functions_with_type"] == []
    assert body["vars_with_type"] == []
    assert body["code_references"] == []


# ---------- /getXrefsToField ----------


def test_xrefs_to_field_finds_member_reads(binja_session, base_url):
    """`_task_priority` reads `t->pri` twice: once in the
    `if (t->pri != PRIORITY_CRITICAL)` check and once in the
    `return t->pri`. Both should surface with addresses and HLIL
    snippets pointing at the right function."""
    r = binja_session.get(
        f"{base_url}/getXrefsToField",
        params={"struct": "task_t", "field": "pri"},
        timeout=5,
    )
    r.raise_for_status()
    body = r.json()
    refs = body["references"]
    assert len(refs) >= 2
    for ref in refs:
        assert ref["function"] == "_task_priority"
        assert ref["address"].startswith("0x")
        assert "t->pri" in ref["text"]


def test_xrefs_to_field_unknown_field_returns_empty(binja_session, base_url):
    """Asking about a field that doesn't exist on a real struct
    returns a 200 with empty references — same contract as the
    other type-xref endpoints."""
    r = binja_session.get(
        f"{base_url}/getXrefsToField",
        params={"struct": "task_t", "field": "definitely_not_a_field"},
        timeout=5,
    )
    r.raise_for_status()
    assert r.json()["references"] == []


# ---------- /getXrefsToEnum ----------


def test_xrefs_to_enum_lists_all_members(binja_session, base_url):
    """Every enumerator from the C source must be reported, with
    its integer value intact."""
    r = binja_session.get(f"{base_url}/getXrefsToEnum", params={"name": "priority_t"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    by_name = {m["name"]: m["value"] for m in body["members"]}
    assert by_name == {
        "PRIORITY_LOW": 0,
        "PRIORITY_NORMAL": 1,
        "PRIORITY_HIGH": 7,
        "PRIORITY_CRITICAL": 42,
    }


def test_xrefs_to_enum_finds_constant_usage(binja_session, base_url):
    """`_task_priority` compares against `PRIORITY_CRITICAL` (42).
    The usages list should surface that site so the agent can
    answer "which functions special-case this enum value?"."""
    r = binja_session.get(f"{base_url}/getXrefsToEnum", params={"name": "priority_t"}, timeout=5)
    r.raise_for_status()
    usages = r.json()["usages"]
    # At minimum: the PRIORITY_CRITICAL (=42) comparison must show up.
    critical_hits = [u for u in usages if u.get("value") == 42]
    assert critical_hits, f"no PRIORITY_CRITICAL (=42) usage reported: {usages}"
    for hit in critical_hits:
        assert hit.get("function") == "_task_priority"


def test_xrefs_to_enum_unknown_name_returns_empty(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getXrefsToEnum", params={"name": "does_not_exist_xyz"}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    assert body["members"] == []
    assert body["usages"] == []


# ---------- /getXrefsToUnion ----------


def test_xrefs_to_union_lists_typed_function(binja_session, base_url):
    r = binja_session.get(f"{base_url}/getXrefsToUnion", params={"name": "value_view_t"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["union"] == "value_view_t"
    assert "_value_low_byte" in body["functions_with_type"]


def test_xrefs_to_union_lists_typed_variables(binja_session, base_url):
    r = binja_session.get(f"{base_url}/getXrefsToUnion", params={"name": "value_view_t"}, timeout=5)
    r.raise_for_status()
    vars_with_type = r.json()["vars_with_type"]
    assert any(
        v["function"] == "_value_low_byte" and "value_view_t" in v["type"] for v in vars_with_type
    ), f"_value_low_byte's value_view_t* parameter missing: {vars_with_type}"


def test_xrefs_to_union_unknown_name_returns_empty(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getXrefsToUnion", params={"name": "does_not_exist_xyz"}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    assert body["functions_with_type"] == []
    assert body["vars_with_type"] == []


# ---------- /getXrefsToType ----------


def test_xrefs_to_type_general_lookup(binja_session, base_url):
    """The umbrella endpoint covers any named type (struct, enum,
    union, typedef) and reports functions that reference it. For
    `task_t` that's just `_task_priority`."""
    r = binja_session.get(f"{base_url}/getXrefsToType", params={"name": "task_t"}, timeout=5)
    r.raise_for_status()
    body = r.json()
    assert body["type"] == "task_t"
    assert "_task_priority" in body["functions_with_type"]


def test_xrefs_to_type_unknown_name_returns_empty(binja_session, base_url):
    r = binja_session.get(
        f"{base_url}/getXrefsToType", params={"name": "does_not_exist_xyz"}, timeout=5
    )
    r.raise_for_status()
    body = r.json()
    assert body["functions_with_type"] == []
    assert body["code_references"] == []
